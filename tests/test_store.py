"""版本化存储：修订替换、快照重查、迟到更正与历史裁剪。"""

import unittest

from quantile_merger import (
    Bucket,
    HistoryMissingError,
    Observation,
    ValidationError,
    VersionedStore,
)


def obs(series, rev, buckets, start=0, length=10, lo=1, hi=10, source="s"):
    return Observation(source, start, length, lo, hi, tuple(buckets), series, rev)


class RevisionReplacementTest(unittest.TestCase):
    def test_higher_revision_replaces_lower(self):
        store = VersionedStore()
        old = obs("a", 1, [Bucket(1, 5, 3), Bucket(6, 10, 2)])
        new = obs("a", 2, [Bucket(1, 5, 1), Bucket(6, 10, 4)])
        ch1 = store.ingest(old)
        ch2 = store.ingest(new)
        self.assertEqual(ch1.kind, "insert")
        self.assertEqual(ch2.kind, "replace")
        self.assertEqual(ch2.replaced_revision, 1)
        self.assertEqual(store.version, 2)
        effective = store.effective_observations("s", 0)
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0].revision, 2)

    def test_stale_revision_rejected(self):
        store = VersionedStore()
        store.ingest(obs("a", 2, [Bucket(1, 10, 1)]))
        with self.assertRaises(ValidationError):
            store.ingest(obs("a", 1, [Bucket(1, 10, 2)]))

    def test_same_revision_different_content_rejected(self):
        store = VersionedStore()
        store.ingest(obs("a", 1, [Bucket(1, 10, 1)]))
        with self.assertRaises(ValidationError):
            store.ingest(obs("a", 1, [Bucket(1, 10, 2)]))

    def test_idempotent_replay(self):
        store = VersionedStore()
        o = obs("a", 1, [Bucket(1, 10, 1)])
        store.ingest(o)
        ch = store.ingest(o)
        self.assertEqual(store.version, 1)
        self.assertEqual(ch.revision, 1)

    def test_distinct_series_coexist(self):
        store = VersionedStore()
        store.ingest(obs("a", 1, [Bucket(1, 5, 3), Bucket(6, 10, 2)]))
        store.ingest(obs("b", 1, [Bucket(1, 3, 1), Bucket(4, 10, 4)]))
        effective = store.effective_observations("s", 0)
        self.assertEqual({o.series_id for o in effective}, {"a", "b"})


class SnapshotAndCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.store = VersionedStore()
        self.store.ingest(
            obs("a", 1, [Bucket(1, 5, 3), Bucket(6, 10, 2)])
        )
        self.store.ingest(
            obs("b", 1, [Bucket(1, 3, 1), Bucket(4, 10, 4)])
        )

    def test_query_at_historical_version(self):
        r_v2 = self.store.query_window("s", 0, 10, 3, segment_starts=[0])
        self.assertEqual(r_v2.status, "ok")
        self.assertEqual(r_v2.store_version, 2)
        # 迟到更正：系列 a 修订 2 改变桶计数
        self.store.ingest(
            obs("a", 2, [Bucket(1, 5, 2), Bucket(6, 10, 3)])
        )
        r_v3 = self.store.query_window(
            "s", 0, 10, 3, segment_starts=[0]
        )
        self.assertEqual(r_v3.store_version, 3)
        # 旧版本仍可重查，结果与当时一致
        r_old = self.store.query_window(
            "s", 0, 10, 3, segment_starts=[0], at_version=2
        )
        self.assertEqual(r_old.min_kth, r_v2.min_kth)
        self.assertEqual(r_old.max_kth, r_v2.max_kth)
        self.assertEqual(r_old.store_version, 2)
        self.assertNotEqual(r_v3.min_kth, r_old.min_kth)

    def test_unknown_version_raises(self):
        with self.assertRaises(HistoryMissingError):
            self.store.query_window(
                "s", 0, 10, 1, segment_starts=[0], at_version=99
            )

    def test_history_pruning(self):
        store = VersionedStore(retain_history=False)
        store.ingest(obs("a", 1, [Bucket(1, 10, 1)]))
        store.ingest(obs("a", 2, [Bucket(1, 10, 2)]))
        with self.assertRaises(HistoryMissingError):
            store.query_window(
                "s", 0, 10, 1, segment_starts=[0], at_version=1
            )
        # 最新版本仍可用
        r = store.query_window("s", 0, 10, 1, segment_starts=[0])
        self.assertEqual(r.status, "ok")

    def test_affected_segments_helper(self):
        changes = [
            self.store.ingest(
                obs("a", 2, [Bucket(1, 5, 0), Bucket(6, 10, 5)])
            ),
            self.store.ingest(
                obs("c", 1, [Bucket(1, 10, 5)], start=10, length=10
                ),
            ),
        ]
        affected = self.store.affected_segments(changes)
        self.assertEqual(
            sorted(affected),
            sorted([("s", 0, 10), ("s", 10, 20)]),
        )

    def test_infeasible_window_returns_mus(self):
        store = VersionedStore()
        store.ingest(obs("a", 1, [Bucket(1, 5, 3), Bucket(6, 10, 2)]))
        store.ingest(obs("b", 1, [Bucket(1, 5, 4), Bucket(6, 10, 1)]))
        r = store.query_window("s", 0, 10, 1, segment_starts=[0])
        self.assertEqual(r.status, "infeasible")
        self.assertEqual(len(r.mus_reports), 1)
        mus_ids = {o.series_id for o in r.mus_reports[0].mus}
        self.assertEqual(mus_ids, {"a", "b"})

    def test_window_gap_rejected(self):
        store = VersionedStore()
        store.ingest(obs("a", 1, [Bucket(1, 10, 1)], start=0, length=10))
        store.ingest(obs("a", 1, [Bucket(1, 10, 1)], start=20, length=10))
        with self.assertRaises(ValidationError):
            store.query_window("s", 0, 30, 1, segment_starts=[0, 20])


if __name__ == "__main__":
    unittest.main()
