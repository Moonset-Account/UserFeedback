"""见证构造与独立验算测试。"""

import json
import unittest

from quantile_merger import (
    Bucket,
    Observation,
    VersionedStore,
    Witness,
    verify_witness,
)
from quantile_merger.witness import (
    _bucket_overlap_count,
    _kth_of_allocation,
)


def obs(series, rev, buckets, start=0, length=10, lo=1, hi=10, source="s"):
    return Observation(source, start, length, lo, hi, tuple(buckets), series, rev)


class WitnessVerificationTest(unittest.TestCase):
    def setUp(self):
        self.store = VersionedStore()
        self.store.ingest(
            obs("a", 1, [Bucket(1, 5, 3), Bucket(6, 10, 2)])
        )
        self.store.ingest(
            obs("b", 1, [Bucket(1, 3, 1), Bucket(4, 10, 4)])
        )
        self.result = self.store.query_window(
            "s", 0, 10, 3, segment_starts=[0]
        )

    def test_both_witnesses_verify(self):
        for w in (self.result.min_witness, self.result.max_witness):
            report = verify_witness(w)
            self.assertTrue(report.ok, report.errors)
            self.assertTrue(report.checks_passed)

    def test_witness_achieves_declared_bound(self):
        w_min = self.result.min_witness
        w_max = self.result.max_witness
        self.assertEqual(w_min.kth_value, self.result.min_kth)
        self.assertEqual(w_max.kth_value, self.result.max_kth)
        # 直接从 RLE 分配计算第 k 小
        for w in (w_min, w_max):
            alloc = w.allocations[0]
            self.assertEqual(_kth_of_allocation(alloc, w.k), w.kth_value)

    def test_witness_independent_of_store(self):
        # 存储继续接收迟到更正，旧见证仅凭内嵌快照仍可验算
        self.store.ingest(
            obs("a", 2, [Bucket(1, 5, 0), Bucket(6, 10, 5)])
        )
        old_witness = self.result.min_witness
        report = verify_witness(old_witness)
        self.assertTrue(report.ok, report.errors)
        # 用新存储版本验算旧见证时，版本元数据仍可显式比对
        report2 = verify_witness(
            old_witness, expected_store_version=old_witness.store_version
        )
        self.assertTrue(report2.ok)

    def test_tampered_witness_fails_verification(self):
        w = self.result.min_witness
        # 篡改分配：把第一条 RLE 的计数加一
        alloc = w.allocations[0]
        bad_rle = list(alloc.rle)
        first = bad_rle[0]
        bad_rle[0] = type(first)(
            first.cell_lo, first.cell_hi, first.count + 1
        )
        bad_alloc = type(alloc)(
            alloc.source,
            alloc.segment_start,
            alloc.segment_length,
            tuple(bad_rle),
        )
        tampered = Witness(
            source=w.source,
            window_start=w.window_start,
            window_end=w.window_end,
            k=w.k,
            bound=w.bound,
            kth_value=w.kth_value,
            allocations=(bad_alloc,),
            observations=w.observations,
            store_version=w.store_version,
        )
        report = verify_witness(tampered)
        self.assertFalse(report.ok)
        self.assertTrue(report.errors)

    def test_wrong_bound_claim_fails(self):
        w = self.result.min_witness
        forged = Witness(
            source=w.source,
            window_start=w.window_start,
            window_end=w.window_end,
            k=w.k,
            bound="min",
            kth_value=w.kth_value + 100,
            allocations=w.allocations,
            observations=w.observations,
            store_version=w.store_version,
        )
        report = verify_witness(forged)
        self.assertFalse(report.ok)

    def test_json_roundtrip_preserves_witness(self):
        w = self.result.max_witness
        blob = json.dumps(w.to_dict())
        restored = Witness.from_dict(json.loads(blob))
        self.assertEqual(restored, w)
        report = verify_witness(restored)
        self.assertTrue(report.ok, report.errors)

    def test_bucket_overlap_helper(self):
        from quantile_merger import Allocation, RLE

        alloc = Allocation(
            "s", 0, 10, (RLE(1, 3, 2), RLE(4, 5, 0), RLE(6, 10, 1))
        )
        self.assertEqual(_bucket_overlap_count(alloc, 1, 5), 6)
        self.assertEqual(_bucket_overlap_count(alloc, 2, 4), 4)
        self.assertEqual(alloc.total(), 11)


if __name__ == "__main__":
    unittest.main()
