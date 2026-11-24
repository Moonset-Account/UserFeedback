"""并发交错测试：多线程摄入/查询下的版本一致性与故障恢复语义。"""

import threading
import unittest

from quantile_merger import (
    Bucket,
    Observation,
    VersionedStore,
    verify_witness,
)


def obs(series, rev, count, start=0, length=10, lo=1, hi=10, source="s"):
    left = count // 2
    right = count - left
    return Observation(
        source,
        start,
        length,
        lo,
        hi,
        (Bucket(lo, 5, left), Bucket(6, hi, right)),
        series,
        rev,
    )


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_ingests_serialize_and_versions_monotonic(self):
        store = VersionedStore()
        n_threads = 8
        per_thread = 25
        errors = []
        versions = []
        lock = threading.Lock()

        def worker(tid):
            try:
                for i in range(per_thread):
                    ch = store.ingest(
                        obs(f"s{tid}", i + 1, count=(i + 1))
                    )
                    with lock:
                        versions.append(ch.store_version)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,))
                   for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(versions), n_threads * per_thread)
        self.assertEqual(sorted(versions), list(range(1, len(versions) + 1)))
        self.assertEqual(store.version, n_threads * per_thread)

    def test_concurrent_revisions_same_series(self):
        store = VersionedStore()
        # 先放基线修订
        store.ingest(obs("a", 0, count=2))
        errors = []

        def worker(rev):
            try:
                store.ingest(obs("a", rev, count=rev + 1))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(r,)) for r in range(1, 20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        effective = store.effective_observations("s", 0)
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0].revision, 19)

    def test_concurrent_queries_while_ingesting(self):
        store = VersionedStore()
        # 基线：a 与 b 相容，共 4 个样本
        store.ingest(
            Observation(
                "s", 0, 10, 1, 10,
                (Bucket(1, 5, 2), Bucket(6, 10, 2)),
                "a", 1,
            )
        )
        store.ingest(
            Observation(
                "s", 0, 10, 1, 10,
                (Bucket(1, 3, 2), Bucket(4, 10, 2)),
                "b", 1,
            )
        )
        stop = threading.Event()
        errors = []
        verified = []

        def reader():
            try:
                while not stop.is_set():
                    r = store.query_window(
                        "s", 0, 10, 2, segment_starts=[0]
                    )
                    if r.status == "ok":
                        report = verify_witness(r.min_witness)
                        if not report.ok:
                            errors.append(AssertionError(report.errors))
                        verified.append(r.store_version)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()

        for rev in range(2, 40):
            # 保持段总数 4 与系列 b 相容，仅改变桶分布，避免段变不可行
            left = rev % 5
            store.ingest(
                Observation(
                    "s", 0, 10, 1, 10,
                    (Bucket(1, 5, left), Bucket(6, 10, 4 - left)),
                    "a", rev,
                )
            )
        stop.set()
        for t in readers:
            t.join()
        self.assertEqual(errors, [])
        self.assertTrue(verified)
        # 读者至少观察到了多个快照版本（调度极端时可能只见到一版，
        # 此时验证查询本身始终成功即可，不做硬性版本数断言）
        self.assertGreaterEqual(len(set(verified)), 1)

    def test_snapshot_witness_remains_valid_after_many_changes(self):
        store = VersionedStore()
        # a 与 b 描述同一分配：[1,3]=1, [4,5]=2, [6,10]=0（共 3 个样本）
        store.ingest(
            Observation(
                "s", 0, 10, 1, 10,
                (Bucket(1, 5, 3), Bucket(6, 10, 0)),
                "a", 1,
            )
        )
        store.ingest(
            Observation(
                "s", 0, 10, 1, 10,
                (Bucket(1, 3, 1), Bucket(4, 5, 2), Bucket(6, 10, 0)),
                "b", 1,
            )
        )
        original = store.query_window(
            "s", 0, 10, 2, segment_starts=[0]
        )
        self.assertEqual(original.status, "ok")
        witness = original.min_witness
        for rev in range(2, 30):
            # 左桶 [1,5] 固定 3 个（与 b 的 [1,3]=1,[4,5]=2 相容）；
            # 右侧无样本，细分桶只改变边界，段始终可行且产生新版本
            store.ingest(
                Observation(
                    "s", 0, 10, 1, 10,
                    (
                        Bucket(1, 5, 3),
                        Bucket(6, 7 + (rev % 3), 0),
                        Bucket(8 + (rev % 3), 10, 0),
                    ),
                    "a", rev,
                )
            )
        # 旧见证独立验算，不依赖当前存储状态
        report = verify_witness(witness)
        self.assertTrue(report.ok, report.errors)
        # 旧快照仍可在存储中按版本重查
        old = store.query_window(
            "s", 0, 10, 2, segment_starts=[0],
            at_version=witness.store_version,
        )
        self.assertEqual(old.min_kth, witness.kth_value)


if __name__ == "__main__":
    unittest.main()
