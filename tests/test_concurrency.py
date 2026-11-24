"""真实线程并发下的提交安全性测试。"""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from temporal_fk import (  # noqa: E402
    TemporalForeignKeyError,
    TemporalStore,
)
from temporal_fk.coverage import evaluate_coverage  # noqa: E402


def assert_fully_covered(store, test_case):
    parents = {}
    for row in store.list_parents():
        parents.setdefault(row.key, []).append(row.interval)
    children = [(row.row_id, row.key, row.interval) for row in store.list_children()]
    test_case.assertEqual(evaluate_coverage(parents, children), [])


class TestThreadedCommitSafety(unittest.TestCase):
    def test_concurrent_valid_transactions_never_leave_uncovered_state(self):
        """N 个线程各自插入成对的父 / 子区间（同键、同区间）。

        每笔事务自身合法；并发提交后无论成功多少笔，已确认状态都必须
        持续满足时态外键（提交在锁内原子核对 + 生效）。
        """
        store = TemporalStore()
        n_threads = 20
        barrier = threading.Barrier(n_threads)
        failures = []

        def worker(idx):
            try:
                barrier.wait()
                txn = store.begin()
                txn.insert_parent("shared", 0, 100)
                txn.insert_child("shared", 0, 100)
                txn.commit()
            except TemporalForeignKeyError as exc:  # 本原型下不应发生
                failures.append(exc)
            except Exception as exc:  # pragma: no cover - 防御性
                failures.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(failures, [])
        self.assertEqual(len(store.list_parents()), n_threads)
        self.assertEqual(len(store.list_children()), n_threads)
        assert_fully_covered(store, self)

    def test_concurrent_shrink_attempts_are_serialized_and_safe(self):
        """多线程并发尝试缩减同一父区间，任何成功提交都不得留下缺口。"""
        store = TemporalStore()
        setup = store.begin()
        parent_id = setup.insert_parent("k", 0, 10)
        child_id = setup.insert_child("k", 0, 10)
        setup.commit()

        n_threads = 10
        barrier = threading.Barrier(n_threads)
        rejected = []
        accepted = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            txn = store.begin()
            txn.update_parent(parent_id, end=5)
            try:
                txn.commit()
                with lock:
                    accepted.append(1)
            except TemporalForeignKeyError:
                with lock:
                    rejected.append(1)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 没有任何事务能在子区间 [0,10) 仍存在时把父区间缩到 [0,5)
        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), n_threads)
        self.assertEqual(store.get_parent(parent_id).interval, (0, 10))
        self.assertEqual(store.get_child(child_id).interval, (0, 10))
        assert_fully_covered(store, self)


if __name__ == "__main__":
    unittest.main()
