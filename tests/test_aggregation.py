"""跨区交接去重与多观察者局部区间合并测试。"""

import unittest

from sla_accounting.aggregation import (
    DoubleCountingError,
    ObservationReport,
    coverage_gaps,
    merge_reports,
    union_intervals,
)
from sla_accounting.clocks import ClockSource
from sla_accounting.model import Attempt, LogicalRequest, Outcome

CLK_A = ClockSource("a", epsilon=0.2)
CLK_B = ClockSource("b", epsilon=0.5)


def att(clock: ClockSource, region: str, observer: str,
        start: float, finish: float, outcome: Outcome) -> Attempt:
    return Attempt(
        region=region,
        observer=observer,
        start=clock.observe(start),
        finish=clock.observe(finish),
        outcome=outcome,
        deadline=clock.observe(start + 2.0),
    )


def handoff_request(request_id: str, *, mutate_finish: float | None = None) -> LogicalRequest:
    second_finish = 41.6 if mutate_finish is None else mutate_finish
    return LogicalRequest(
        request_id=request_id,
        service="svc",
        origin_region="region-a",
        handed_off=True,
        attempts=(
            att(CLK_A, "region-a", "obs-a", 40.0, 40.8, Outcome.FAILURE),
            att(CLK_B, "region-b", "obs-b", 41.0, second_finish, Outcome.SUCCESS),
        ),
    )


class UnionIntervalsTest(unittest.TestCase):
    def test_disjoint_kept_separate(self) -> None:
        self.assertEqual(
            union_intervals([(1.0, 2.0), (5.0, 6.0)]),
            ((1.0, 2.0), (5.0, 6.0)),
        )

    def test_overlapping_merged(self) -> None:
        self.assertEqual(
            union_intervals([(1.0, 4.0), (3.0, 6.0), (8.0, 9.0)]),
            ((1.0, 6.0), (8.0, 9.0)),
        )

    def test_touching_intervals_merged(self) -> None:
        self.assertEqual(
            union_intervals([(1.0, 2.0), (2.0, 3.0)]),
            ((1.0, 3.0),),
        )

    def test_unsorted_input_normalized(self) -> None:
        self.assertEqual(
            union_intervals([(8.0, 9.0), (1.0, 3.0), (2.0, 4.0)]),
            ((1.0, 4.0), (8.0, 9.0)),
        )


class CoverageGapsTest(unittest.TestCase):
    def test_internal_gap_without_horizon(self) -> None:
        merged = union_intervals([(0.0, 10.0), (20.0, 30.0)])
        self.assertEqual(coverage_gaps(merged), ((10.0, 20.0),))

    def test_gaps_clipped_to_horizon(self) -> None:
        merged = union_intervals([(10.0, 20.0), (40.0, 50.0)])
        self.assertEqual(
            coverage_gaps(merged, horizon=(0.0, 60.0)),
            ((0.0, 10.0), (20.0, 40.0), (50.0, 60.0)),
        )

    def test_full_coverage_no_gap(self) -> None:
        merged = union_intervals([(0.0, 40.0), (30.0, 60.0)])
        self.assertEqual(coverage_gaps(merged, horizon=(0.0, 60.0)), ())


class MergeReportsTest(unittest.TestCase):
    def test_handoff_request_counted_once_at_service_level(self) -> None:
        # 同一条交接请求被两个区域观察者分别上报 -> 服务级只出现一次
        req = handoff_request("h1")
        report_a = ObservationReport(
            observer="obs-a", region="region-a",
            covered_intervals=((0.0, 100.0),), requests=(req,),
        )
        report_b = ObservationReport(
            observer="obs-b", region="region-b",
            covered_intervals=((0.0, 100.0),), requests=(req,),
        )
        agg = merge_reports("svc", [report_a, report_b], horizon=(0.0, 100.0))
        self.assertEqual(len(agg.requests), 1)
        self.assertEqual(agg.duplicate_reports_ignored, 1)
        self.assertEqual(agg.regions, frozenset({"region-a", "region-b"}))
        # 预算事件只有一条
        self.assertEqual(len(agg.verdicts), 1)

    def test_distinct_requests_both_kept(self) -> None:
        r1 = handoff_request("h1")
        r2 = handoff_request("h2")
        report_a = ObservationReport(
            observer="obs-a", region="region-a",
            covered_intervals=((0.0, 100.0),), requests=(r1,),
        )
        report_b = ObservationReport(
            observer="obs-b", region="region-b",
            covered_intervals=((0.0, 100.0),), requests=(r2,),
        )
        agg = merge_reports("svc", [report_a, report_b])
        self.assertEqual({r.request_id for r in agg.requests}, {"h1", "h2"})

    def test_conflicting_duplicate_rejected(self) -> None:
        # 同 id 但内容冲突 -> 拒绝，防止两边都进预算
        r1 = handoff_request("h1")
        r1_conflict = handoff_request("h1", mutate_finish=41.9)
        report_a = ObservationReport(
            observer="obs-a", region="region-a",
            covered_intervals=((0.0, 100.0),), requests=(r1,),
        )
        report_b = ObservationReport(
            observer="obs-b", region="region-b",
            covered_intervals=((0.0, 100.0),), requests=(r1_conflict,),
        )
        with self.assertRaises(DoubleCountingError):
            merge_reports("svc", [report_a, report_b])

    def test_foreign_service_report_rejected(self) -> None:
        req = LogicalRequest(
            request_id="x",
            service="other-svc",
            origin_region="region-a",
            attempts=(att(CLK_A, "region-a", "obs-a", 0.0, 0.5, Outcome.SUCCESS),),
        )
        report = ObservationReport(
            observer="obs-a", region="region-a",
            covered_intervals=((0.0, 10.0),), requests=(req,),
        )
        with self.assertRaises(ValueError):
            merge_reports("svc", [report])

    def test_observer_intervals_merged_and_gaps_flagged(self) -> None:
        report_a = ObservationReport(
            observer="obs-a", region="region-a",
            covered_intervals=((0.0, 30.0), (70.0, 100.0)), requests=(),
        )
        report_b = ObservationReport(
            observer="obs-b", region="region-b",
            covered_intervals=((40.0, 70.0),), requests=(),
        )
        agg = merge_reports("svc", [report_a, report_b], horizon=(0.0, 100.0))
        # 30-40 是唯一缺口；60/70 端点相接被合并
        self.assertEqual(agg.coverage, ((0.0, 30.0), (40.0, 100.0)))
        self.assertEqual(agg.gaps, ((30.0, 40.0),))

    def test_reverse_interval_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ObservationReport(
                observer="obs", region="region-a",
                covered_intervals=((10.0, 5.0),),
            )


if __name__ == "__main__":
    unittest.main()
