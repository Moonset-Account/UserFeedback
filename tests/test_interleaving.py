"""两笔事务交错提交的集成测试。

题面要求：模拟一笔缩短父区间的事务与一笔延长子区间的事务交错提交，
核对最终关系不会留下未覆盖的子记录。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from temporal_fk import (  # noqa: E402
    TemporalForeignKeyError,
    TemporalStore,
    WriteConflictError,
)
from temporal_fk.coverage import evaluate_coverage  # noqa: E402


def seed_store():
    """初始：父键 'k'，父区间 [0,10)，子区间 [0,10)，完全覆盖。"""
    store = TemporalStore()
    txn = store.begin()
    parent_id = txn.insert_parent("k", 0, 10)
    child_id = txn.insert_child("k", 0, 10)
    txn.commit()
    return store, parent_id, child_id


def assert_fully_covered(store, test_case):
    """对存储的最新已确认状态独立再跑一次覆盖核对。"""
    parents = {}
    for row in store.list_parents():
        parents.setdefault(row.key, []).append(row.interval)
    children = [
        (row.row_id, row.key, row.interval) for row in store.list_children()
    ]
    test_case.assertEqual(evaluate_coverage(parents, children), [])


class TestInterleavedTransactions(unittest.TestCase):
    def test_shrink_parent_first_then_extend_child_must_be_rejected(self):
        """交错顺序一：缩短父区间先提交，延长子区间后提交必须被拒绝。"""
        store, parent_id, child_id = seed_store()

        # 两笔事务各自基于同一个已确认快照开始（交错发生在提交之前）
        shrink_txn = store.begin()
        extend_txn = store.begin()

        # 事务 A：把父区间从 [0,10) 缩短为 [0,6)
        shrink_txn.update_parent(parent_id, end=6)
        # 事务 B（交错、尚未提交）：把子区间从 [0,10) 延长为 [0,12)
        extend_txn.update_child(child_id, end=12)

        # A 先提交：此时已确认子区间仍是 [0,10)，[0,6) 无法覆盖 -> 拒绝
        with self.assertRaises(TemporalForeignKeyError) as ctx:
            shrink_txn.commit()
        self.assertEqual(list(ctx.exception.gaps[0].missing), [(6, 10)])
        self.assertEqual(store.get_parent(parent_id).interval, (0, 10))

        # B 随后提交：父区间未变（仍 [0,10)），子延长到 [0,12) -> 同样拒绝
        with self.assertRaises(TemporalForeignKeyError) as ctx:
            extend_txn.commit()
        self.assertEqual(list(ctx.exception.gaps[0].missing), [(10, 12)])
        self.assertEqual(store.get_child(child_id).interval, (0, 10))

        # 两次拒绝后已确认状态完全不变，最终关系没有未覆盖子记录
        assert_fully_covered(store, self)

    def test_extend_child_first_then_shrink_parent_must_be_rejected(self):
        """交错顺序二：延长子区间先提交，缩短父区间后提交也必须被拒绝。"""
        store, parent_id, child_id = seed_store()

        shrink_txn = store.begin()
        extend_txn = store.begin()

        shrink_txn.update_parent(parent_id, end=6)
        extend_txn.update_child(child_id, end=12)

        # B 先提交：父仍为 [0,10)，子 [0,12) 缺 [10,12) -> 拒绝
        with self.assertRaises(TemporalForeignKeyError) as ctx:
            extend_txn.commit()
        self.assertEqual(list(ctx.exception.gaps[0].missing), [(10, 12)])

        # A 后提交：子仍为 [0,10)，父缩到 [0,6) 缺 [6,10) -> 拒绝
        with self.assertRaises(TemporalForeignKeyError) as ctx:
            shrink_txn.commit()
        self.assertEqual(list(ctx.exception.gaps[0].missing), [(6, 10)])

        assert_fully_covered(store, self)

    def test_interleaved_retry_reaches_valid_final_state(self):
        """交错冲突后重开事务补齐父区间，最终状态合法且无未覆盖子记录。"""
        store, parent_id, child_id = seed_store()

        shrink_txn = store.begin()
        extend_txn = store.begin()
        shrink_txn.update_parent(parent_id, end=6)
        extend_txn.update_child(child_id, end=12)

        with self.assertRaises(TemporalForeignKeyError):
            shrink_txn.commit()
        with self.assertRaises(TemporalForeignKeyError):
            extend_txn.commit()

        # 重试：一笔新事务同时完成“延长子区间 + 用相邻小段补齐父区间”
        retry = store.begin()
        retry.delete_parent(parent_id)
        retry.insert_parent("k", 0, 6)
        retry.insert_parent("k", 6, 10)
        retry.insert_parent("k", 10, 12)
        retry.update_child(child_id, end=12)
        retry.commit()

        self.assertEqual(store.get_child(child_id).interval, (0, 12))
        self.assertEqual(
            sorted(store.parent_intervals("k")),
            [(0, 6), (6, 10), (10, 12)],
        )
        assert_fully_covered(store, self)

    def test_independent_interleaved_transactions_both_commit(self):
        """交错但互不相关的两笔事务应都能提交（按最新已确认状态核对）。"""
        store, _, _ = seed_store()

        txn_a = store.begin()
        txn_b = store.begin()
        # A 操作键 'a'，B 操作键 'b'
        a_parent = txn_a.insert_parent("a", 0, 5)
        a_child = txn_a.insert_child("a", 1, 4)
        b_parent = txn_b.insert_parent("b", 0, 5)
        b_child = txn_b.insert_child("b", 1, 4)

        txn_a.commit()
        txn_b.commit()

        self.assertEqual(store.get_parent(a_parent).key, "a")
        self.assertEqual(store.get_parent(b_parent).key, "b")
        self.assertEqual(store.get_child(a_child).key, "a")
        self.assertEqual(store.get_child(b_child).key, "b")
        assert_fully_covered(store, self)

    def test_write_conflict_when_other_transaction_deleted_the_row(self):
        """一笔事务删除父行并提交后，另一笔仍想改该行的事务提交时报写冲突。"""
        store, parent_id, child_id = seed_store()

        delete_txn = store.begin()
        stale_txn = store.begin()

        # 旧事务在过期快照上计划缩减同一父行
        stale_txn.update_parent(parent_id, end=6)

        # 删除事务同时把孩子迁走，保证删除本身通过外键核对
        delete_txn.delete_child(child_id)
        delete_txn.delete_parent(parent_id)
        delete_txn.commit()

        with self.assertRaises(WriteConflictError):
            stale_txn.commit()

        # 存储里已不存在该父行
        self.assertEqual(store.list_parents(), [])


if __name__ == "__main__":
    unittest.main()
