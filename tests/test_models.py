"""输入模型字段校验的单元测试。"""

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
)


def _route():
    return (
        RouteNode("relay-a", 1, NodeKind.RELAY),
        RouteNode("region-b", 2, NodeKind.REGION),
    )


def _receipts():
    return (
        HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 100),)),
        HopReceipt("region-b", NodeKind.REGION, (ByteRange(0, 100),)),
    )


class ByteRangeTest(unittest.TestCase):
    def test_valid(self):
        item = ByteRange(0, 1)
        self.assertEqual((item.start, item.end), (0, 1))

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            ByteRange(-1, 10)

    def test_end_must_exceed_start(self):
        with self.assertRaises(ValueError):
            ByteRange(5, 5)
        with self.assertRaises(ValueError):
            ByteRange(10, 5)

    def test_bool_is_not_int_here(self):
        with self.assertRaises(ValueError):
            ByteRange(True, 10)  # type: ignore[arg-type]


class RouteNodeAndReceiptTest(unittest.TestCase):
    def test_blank_node_rejected(self):
        with self.assertRaises(ValueError):
            RouteNode("  ", 1, NodeKind.RELAY)

    def test_sequence_must_be_positive(self):
        with self.assertRaises(ValueError):
            RouteNode("x", 0, NodeKind.RELAY)

    def test_wrong_kind_rejected(self):
        with self.assertRaises(ValueError):
            RouteNode("x", 1, "relay")  # type: ignore[arg-type]

    def test_receipt_requires_ranges(self):
        with self.assertRaises(ValueError):
            HopReceipt("x", NodeKind.RELAY, ())

    def test_receipt_normalizes_list_to_tuple(self):
        receipt = HopReceipt("x", NodeKind.RELAY, [ByteRange(0, 10)])
        self.assertIsInstance(receipt.ranges, tuple)


class AttemptTest(unittest.TestCase):
    def test_valid(self):
        attempt = Attempt(1, _route(), _receipts(), DeliveryResult.DELIVERED)
        self.assertEqual(attempt.attempt_no, 1)

    def test_attempt_no_must_be_positive(self):
        with self.assertRaises(ValueError):
            Attempt(0, _route(), _receipts(), DeliveryResult.FAILED)

    def test_route_sequences_must_be_dense(self):
        bad_route = (
            RouteNode("a", 1, NodeKind.RELAY),
            RouteNode("b", 3, NodeKind.REGION),
        )
        with self.assertRaises(ValueError):
            Attempt(1, bad_route, (), DeliveryResult.FAILED)

    def test_route_must_be_non_empty(self):
        with self.assertRaises(ValueError):
            Attempt(1, (), (), DeliveryResult.FAILED)

    def test_duplicate_receipt_node_rejected(self):
        bad_receipts = (
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 10),)),
            HopReceipt("relay-a", NodeKind.RELAY, (ByteRange(0, 10),)),
        )
        with self.assertRaises(ValueError):
            Attempt(1, _route(), bad_receipts, DeliveryResult.DELIVERED)

    def test_wrong_result_rejected(self):
        with self.assertRaises(ValueError):
            Attempt(1, _route(), _receipts(), "delivered")  # type: ignore[arg-type]


class SegmentAndBatchTest(unittest.TestCase):
    def test_segment_requires_id(self):
        with self.assertRaises(ValueError):
            Segment("", ByteRange(0, 10))

    def _segment(self):
        return Segment("seg-1", ByteRange(0, 100))

    def test_batch_duplicate_segment_rejected(self):
        with self.assertRaises(ValueError):
            Batch((self._segment(), self._segment()), {})

    def test_batch_unknown_attempt_key_rejected(self):
        batch_attempts = {"seg-other": (Attempt(1, _route(), (), DeliveryResult.FAILED),)}
        with self.assertRaises(ValueError):
            Batch((self._segment(),), batch_attempts)

    def test_batch_duplicate_attempt_no_rejected(self):
        attempts = {
            "seg-1": (
                Attempt(1, _route(), (), DeliveryResult.FAILED),
                Attempt(1, _route(), (), DeliveryResult.FAILED),
            )
        }
        with self.assertRaises(ValueError):
            Batch((self._segment(),), attempts)

    def test_batch_accepts_empty_attempt_list_for_segment(self):
        batch = Batch((self._segment(),), {"seg-1": []})
        self.assertEqual(batch.attempts["seg-1"], ())


if __name__ == "__main__":
    unittest.main()
