"""合规核查主流程的单元测试：正常场景、边界条件与交错情况。"""

import unittest

from transit_audit import (
    Attempt,
    Batch,
    ByteRange,
    DeliveryResult,
    HopReceipt,
    NodeKind,
    RouteNode,
    Segment,
    SegmentStatus,
    ViolationKind,
    verify_batch,
    verify_segment,
)


# ---- 构造助手 -------------------------------------------------------------

def route_three_hops():
    return (
        RouteNode("relay-a", 1, NodeKind.RELAY),
        RouteNode("relay-b", 2, NodeKind.RELAY),
        RouteNode("region-c", 3, NodeKind.REGION),
    )


def receipts_full_cover(segment_end=100):
    return (
        HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, segment_end),)),
        HopReceipt("relay-b", NodeKind.RELAY, (ByteRange(0, segment_end),)),
        HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, segment_end),)),
    )


def delivered_attempt(attempt_no, route, receipts):
    return Attempt(attempt_no, route, receipts, DeliveryResult.DELIVERED)


def failed_attempt(attempt_no, route, receipts=()):
    return Attempt(attempt_no, route, receipts, DeliveryResult.FAILED)


# ---- 正常与边界场景 --------------------------------------------------------

class CompliantScenariosTest(unittest.TestCase):
    def test_single_segment_full_compliance(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        attempt = delivered_attempt(1, route_three_hops(), receipts_full_cover())
        report = verify_segment(segment, (attempt,))

        self.assertEqual(report.status, SegmentStatus.COMPLIANT)
        self.assertTrue(report.is_compliant)
        self.assertEqual(report.selected_attempt_no, 1)
        self.assertEqual(report.violations, ())
        self.assertEqual(report.archived_attempts, ())

    def test_split_and_out_of_order_receipts_form_continuous_route(self):
        # 中继把一段拆成多次传送，回执区间乱序、重叠，但合并后连续覆盖。
        segment = Segment("seg-1", ByteRange(0, 100))
        split_receipts = (
            HopReceipt(
                "relay-a",
                NodeKind.RELAY,
                (ByteRange(60, 100), ByteRange(0, 30), ByteRange(25, 65)),
            ),
            HopReceipt(
                "relay-b",
                NodeKind.RELAY,
                (ByteRange(30, 70), ByteRange(0, 35), ByteRange(65, 100)),
            ),
            HopReceipt(
                "region-c",
                NodeKind.REGION,
                (ByteRange(0, 50), ByteRange(50, 100)),  # 首尾相接
            ),
        )
        report = verify_segment(segment, (delivered_attempt(1, route_three_hops(), split_receipts),))
        self.assertEqual(report.status, SegmentStatus.COMPLIANT)

    def test_adjacent_segments_in_one_batch(self):
        # 两个分段字节范围首尾相接，各自独立核查，互不影响。
        seg1 = Segment("seg-1", ByteRange(0, 100))
        seg2 = Segment("seg-2", ByteRange(100, 200))
        shifted = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(100, 200),)),
            HopReceipt("relay-b", NodeKind.RELAY, (ByteRange(100, 200),)),
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(100, 200),)),
        )
        batch = Batch(
            (seg1, seg2),
            {
                "seg-1": (delivered_attempt(1, route_three_hops(), receipts_full_cover()),),
                "seg-2": (delivered_attempt(1, route_three_hops(), shifted),),
            },
        )
        report = verify_batch(batch)
        self.assertTrue(report.compliant)
        self.assertEqual([item.segment_id for item in report.segment_reports], ["seg-1", "seg-2"])

    def test_single_byte_segment_boundary(self):
        segment = Segment("seg-x", ByteRange(50, 51))
        receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(50, 51),)),
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(50, 51),)),
        )
        route = (
            RouteNode("relay-a", 1, NodeKind.RELAY),
            RouteNode("region-c", 2, NodeKind.REGION),
        )
        report = verify_segment(segment, (delivered_attempt(1, route, receipts),))
        self.assertEqual(report.status, SegmentStatus.COMPLIANT)


# ---- 违规场景：越界、缺口、缺跳、未知节点 -----------------------------------

class ViolationScenariosTest(unittest.TestCase):
    def test_out_of_bounds_reports_exact_subrange_and_node(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 100),)),
            HopReceipt("relay-b", NodeKind.RELAY, (ByteRange(0, 130),)),  # 越界 30 字节
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, 100),)),
        )
        report = verify_segment(
            segment, (delivered_attempt(1, route_three_hops(), receipts),)
        )
        self.assertEqual(report.status, SegmentStatus.NON_COMPLIANT)
        self.assertEqual(len(report.violations), 1)
        violation = report.violations[0]
        self.assertEqual(violation.kind, ViolationKind.OUT_OF_BOUNDS)
        self.assertEqual(violation.node, "relay-b")
        self.assertEqual(violation.node_kind, NodeKind.RELAY)
        self.assertEqual(violation.ranges, (ByteRange(100, 130),))
        self.assertIn("relay-b", violation.message)
        self.assertIn("[100, 130)", violation.message)

    def test_out_of_bounds_on_both_sides(self):
        segment = Segment("seg-1", ByteRange(20, 80))
        receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 100),)),
            HopReceipt("relay-b", NodeKind.RELAY, (ByteRange(20, 80),)),
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(20, 80),)),
        )
        report = verify_segment(
            segment, (delivered_attempt(1, route_three_hops(), receipts),)
        )
        violation = report.violations[0]
        self.assertEqual(violation.kind, ViolationKind.OUT_OF_BOUNDS)
        self.assertEqual(violation.ranges, (ByteRange(0, 20), ByteRange(80, 100)))

    def test_missing_coverage_reports_gap_subrange(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 40), ByteRange(60, 100))),
            HopReceipt("relay-b", NodeKind.RELAY, (ByteRange(0, 100),)),
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, 100),)),
        )
        report = verify_segment(
            segment, (delivered_attempt(1, route_three_hops(), receipts),)
        )
        violation = report.violations[0]
        self.assertEqual(violation.kind, ViolationKind.MISSING_COVERAGE)
        self.assertEqual(violation.node, "relay-a")
        self.assertEqual(violation.ranges, (ByteRange(40, 60),))
        self.assertIn("[40, 60)", violation.message)

    def test_missing_hop_whole_node_absent(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 100),)),
            # relay-b 整条跳缺失
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, 100),)),
        )
        report = verify_segment(
            segment, (delivered_attempt(1, route_three_hops(), receipts),)
        )
        self.assertEqual(report.status, SegmentStatus.NON_COMPLIANT)
        violation = report.violations[0]
        self.assertEqual(violation.kind, ViolationKind.MISSING_HOP)
        self.assertEqual(violation.node, "relay-b")
        self.assertEqual(violation.ranges, (ByteRange(0, 100),))

    def test_unknown_node_receipt_flagged(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        receipts = receipts_full_cover() + (
            HopReceipt("rogue-relay", NodeKind.RELAY, (ByteRange(0, 100),)),
        )
        report = verify_segment(
            segment, (delivered_attempt(1, route_three_hops(), receipts),)
        )
        self.assertEqual(report.status, SegmentStatus.NON_COMPLIANT)
        violation = report.violations[0]
        self.assertEqual(violation.kind, ViolationKind.UNKNOWN_NODE)
        self.assertEqual(violation.node, "rogue-relay")

    def test_multiple_violations_ordered_by_hop_sequence(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 50),)),  # 缺口 50..100
            # relay-b 缺失
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, 110),)),  # 越界
        )
        report = verify_segment(
            segment, (delivered_attempt(1, route_three_hops(), receipts),)
        )
        kinds = [(item.node, item.kind) for item in report.violations]
        self.assertEqual(
            kinds,
            [
                ("relay-a", ViolationKind.MISSING_COVERAGE),
                ("relay-b", ViolationKind.MISSING_HOP),
                ("region-c", ViolationKind.OUT_OF_BOUNDS),
            ],
        )


# ---- 尝试选择与失败留档 ----------------------------------------------------

class AttemptSelectionTest(unittest.TestCase):
    def test_all_failed_marks_undelivered_and_archives(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        attempts = (
            failed_attempt(1, route_three_hops()),
            failed_attempt(2, route_three_hops()),
        )
        report = verify_segment(segment, attempts)
        self.assertEqual(report.status, SegmentStatus.UNDELIVERED)
        self.assertIsNone(report.selected_attempt_no)
        self.assertEqual(report.violations, ())
        self.assertEqual(len(report.archived_attempts), 2)
        self.assertEqual(
            [item.attempt_no for item in report.archived_attempts], [1, 2]
        )
        self.assertTrue(all(item.result is DeliveryResult.FAILED for item in report.archived_attempts))
        self.assertIn("失败", report.archived_attempts[0].reason)

    def test_no_attempts_at_all(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        report = verify_segment(segment, ())
        self.assertEqual(report.status, SegmentStatus.UNDELIVERED)

    def test_latest_delivered_attempt_is_audited(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        # 尝试 1 成功但路线有缺口；尝试 2 成功且完整 -> 只核查尝试 2。
        bad_receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 50),)),
            HopReceipt("relay-b", NodeKind.RELAY, (ByteRange(0, 50),)),
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, 50),)),
        )
        attempts = (
            delivered_attempt(1, route_three_hops(), bad_receipts),
            failed_attempt(2, route_three_hops()),
            delivered_attempt(3, route_three_hops(), receipts_full_cover()),
        )
        report = verify_segment(segment, attempts)
        self.assertEqual(report.status, SegmentStatus.COMPLIANT)
        self.assertEqual(report.selected_attempt_no, 3)
        archived = report.archived_attempts
        self.assertEqual([item.attempt_no for item in archived], [1, 2])
        self.assertEqual(archived[0].result, DeliveryResult.DELIVERED)
        self.assertIn("取代", archived[0].reason)
        self.assertEqual(archived[1].result, DeliveryResult.FAILED)

    def test_earlier_failure_archived_when_later_succeeds(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        report = verify_segment(
            segment,
            (
                failed_attempt(1, route_three_hops()),
                delivered_attempt(2, route_three_hops(), receipts_full_cover()),
            ),
        )
        self.assertEqual(report.selected_attempt_no, 2)
        self.assertEqual(report.status, SegmentStatus.COMPLIANT)
        self.assertEqual(len(report.archived_attempts), 1)
        self.assertEqual(report.archived_attempts[0].attempt_no, 1)


# ---- 交错场景与批次级聚合 --------------------------------------------------

class InterleavingAndBatchTest(unittest.TestCase):
    def test_interleaved_multi_segment_batch(self):
        # 两个分段的尝试编号交错：seg-1 失败后成功，seg-2 成功后又成功一次。
        seg1 = Segment("seg-1", ByteRange(0, 100))
        seg2 = Segment("seg-2", ByteRange(0, 50))

        seg1_attempts = (
            failed_attempt(1, route_three_hops()),
            delivered_attempt(2, route_three_hops(), receipts_full_cover()),
        )
        seg2_receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 50),)),
            HopReceipt("relay-b", NodeKind.RELAY, (ByteRange(0, 50),)),
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, 50),)),
        )
        seg2_attempts = (
            delivered_attempt(1, route_three_hops(), seg2_receipts),
            delivered_attempt(2, route_three_hops(), seg2_receipts),
        )
        batch = Batch(
            (seg1, seg2),
            {"seg-1": seg1_attempts, "seg-2": seg2_attempts},
        )
        report = verify_batch(batch)
        self.assertTrue(report.compliant)
        r1, r2 = report.segment_reports
        self.assertEqual(r1.selected_attempt_no, 2)
        self.assertEqual(len(r1.archived_attempts), 1)
        self.assertEqual(r2.selected_attempt_no, 2)
        self.assertEqual(len(r2.archived_attempts), 1)
        self.assertIn("取代", r2.archived_attempts[0].reason)

    def test_batch_compliant_false_when_any_segment_bad(self):
        good = Segment("seg-ok", ByteRange(0, 10))
        bad = Segment("seg-bad", ByteRange(0, 10))
        good_route = (
            RouteNode("relay-a", 1, NodeKind.RELAY),
            RouteNode("region-c", 2, NodeKind.REGION),
        )
        good_receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 10),)),
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, 10),)),
        )
        bad_receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 5),)),
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, 10),)),
        )
        batch = Batch(
            (good, bad),
            {
                "seg-ok": (delivered_attempt(1, good_route, good_receipts),),
                "seg-bad": (delivered_attempt(1, good_route, bad_receipts),),
            },
        )
        report = verify_batch(batch)
        self.assertFalse(report.compliant)
        self.assertEqual(len(report.violations), 1)
        self.assertEqual(report.violations[0].node, "relay-a")

    def test_to_dict_is_json_ready(self):
        segment = Segment("seg-1", ByteRange(0, 100))
        receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 110),)),
            HopReceipt("relay-b", NodeKind.RELAY, (ByteRange(0, 100),)),
            HopReceipt("region-c", NodeKind.REGION, (ByteRange(0, 100),)),
        )
        report = verify_segment(
            segment, (delivered_attempt(1, route_three_hops(), receipts),)
        )
        payload = report.to_dict()
        self.assertEqual(payload["status"], "non_compliant")
        self.assertEqual(payload["violations"][0]["kind"], "out_of_bounds")
        self.assertEqual(payload["violations"][0]["ranges"], [{"start": 100, "end": 110}])
        # 批次报告也可完整序列化，且只使用原生 JSON 类型。
        import json

        batch = Batch((segment,), {"seg-1": (delivered_attempt(1, route_three_hops(), receipts),)})
        encoded = json.dumps(verify_batch(batch).to_dict(), ensure_ascii=False)
        self.assertIn("non_compliant", encoded)


if __name__ == "__main__":
    unittest.main()
