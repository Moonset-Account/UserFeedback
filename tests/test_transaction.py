"""事务暂存、提交边界约束与存储行为的单元测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from temporal_fk import (  # noqa: E402
    RowNotFoundError,
    TemporalForeignKeyError,
    TemporalStore,
    TransactionClosedError,
)


def build_initial_store():
    """初始关系：父键 'k' 上一条父区间 [0,10)，一条子区间 [0,10)。"""
    store = TemporalStore()
    txn = store.begin()
    parent_id = txn.insert_parent("k", 0, 10)
    child_id = txn.insert_child("k", 0, 10)
    txn.commit()
    return store, parent_id, child_id


class TestCommitBoundaryEnforcement(unittest.TestCase):
    def test_uncovered_child_blocks_commit_and_state_unchanged(self):
        store, parent_id, child_id = build_initial_store()
        txn = store.begin()
        # 把父区间缩减到 [0,5)，子区间仍为 [0,10)
        txn.update_parent(parent_id, start=0, end=5)
        with self.assertRaises(TemporalForeignKeyError) as ctx:
            txn.commit()

        gap = ctx.exception.gaps[0]
        self.assertEqual(gap.child_row_id, child_id)
        self.assertEqual(list(gap.missing), [(5, 10)])

        # 事务被拒绝：已确认状态保持原样
        self.assertEqual(store.get_parent(parent_id).interval, (0, 10))
        self.assertEqual(store.get_child(child_id).interval, (0, 10))

    def test_failed_transaction_cannot_be_reused(self):
        store, parent_id, _ = build_initial_store()
        txn = store.begin()
        txn.update_parent(parent_id, end=5)
        with self.assertRaises(TemporalForeignKeyError):
            txn.commit()
        with self.assertRaises(TransactionClosedError):
            txn.insert_parent("k", 0, 1)
        with self.assertRaises(TransactionClosedError):
            txn.rollback()

    def test_rollback_discards_everything(self):
        store, parent_id, _ = build_initial_store()
        txn = store.begin()
        txn.update_parent(parent_id, end=2)
        txn.insert_child("k", 100, 200)
        txn.rollback()
        self.assertEqual(store.get_parent(parent_id).interval, (0, 10))
        self.assertEqual(len(store.list_children()), 1)
        with self.assertRaises(TransactionClosedError):
            txn.commit()

    def test_context_manager_commits_on_clean_exit(self):
        store = TemporalStore()
        with store.begin() as txn:
            parent_id = txn.insert_parent("k", 0, 5)
            txn.insert_child("k", 0, 5)
        self.assertEqual(store.get_parent(parent_id).interval, (0, 5))

    def test_context_manager_rolls_back_on_exception(self):
        store, _, _ = build_initial_store()
        with self.assertRaises(RuntimeError):
            with store.begin() as txn:
                txn.insert_parent("k", 0, 1)
                raise RuntimeError("boom")
        self.assertEqual(len(store.list_parents()), 1)


class TestSameTransactionReparentAndSplit(unittest.TestCase):
    def test_delete_old_parent_then_insert_adjacent_segments_and_extend_child(self):
        """题面核心场景：同事务删旧父区间、插入相邻小段、同时延长子区间。"""
        store, parent_id, child_id = build_initial_store()

        txn = store.begin()
        # 1) 删掉旧的整条父区间
        txn.delete_parent(parent_id)
        # 2) 插入若干相邻的小段，拼起来覆盖 [0,12)
        seg1 = txn.insert_parent("k", 0, 4)
        seg2 = txn.insert_parent("k", 4, 8)
        seg3 = txn.insert_parent("k", 8, 12)
        # 3) 同时把子记录从 [0,10) 延长到 [0,12)
        txn.update_child(child_id, end=12)
        txn.commit()

        # 旧父行已消失，三条相邻小段全部生效；子记录已延长且完全被覆盖
        with self.assertRaises(RowNotFoundError):
            store.get_parent(parent_id)
        self.assertEqual(
            sorted(row.interval for row in store.list_parents()),
            [(0, 4), (4, 8), (8, 12)],
        )
        self.assertEqual(store.get_child(child_id).interval, (0, 12))
        self.assertEqual(store.parent_intervals("k"), [(0, 4), (4, 8), (8, 12)])

    def test_split_with_internal_gap_is_rejected_with_exact_missing_interval(self):
        store, parent_id, child_id = build_initial_store()
        txn = store.begin()
        txn.delete_parent(parent_id)
        txn.insert_parent("k", 0, 4)
        # 故意留缝：跳过 [4,6)
        txn.insert_parent("k", 6, 10)
        with self.assertRaises(TemporalForeignKeyError) as ctx:
            txn.commit()
        gap = ctx.exception.gaps[0]
        self.assertEqual(gap.child_row_id, child_id)
        self.assertEqual(list(gap.missing), [(4, 6)])
        # 拒绝后已确认状态仍是最初的单条父区间
        self.assertEqual(store.get_parent(parent_id).interval, (0, 10))

    def test_child_delete_then_parent_shrink_is_allowed(self):
        """子记录在同事务被删除后，缩减父区间不再有约束阻碍。"""
        store, parent_id, child_id = build_initial_store()
        txn = store.begin()
        txn.delete_child(child_id)
        txn.update_parent(parent_id, end=3)
        txn.commit()
        self.assertEqual(store.get_parent(parent_id).interval, (0, 3))
        self.assertEqual(store.list_children(), [])

    def test_insert_and_delete_same_new_row_in_one_transaction(self):
        store, parent_id, child_id = build_initial_store()
        txn = store.begin()
        new_id = txn.insert_parent("k", 20, 30)
        txn.delete_parent(new_id)
        txn.update_parent(parent_id, end=10)  # 覆盖不变，提交成功
        txn.commit()
        self.assertEqual(len(store.list_parents()), 1)

    def test_update_child_key_to_uncovered_parent_rejected(self):
        store, parent_id, child_id = build_initial_store()
        txn = store.begin()
        txn.update_child(child_id, key="other")
        with self.assertRaises(TemporalForeignKeyError) as ctx:
            txn.commit()
        gap = ctx.exception.gaps[0]
        self.assertEqual(gap.key, "other")
        self.assertEqual(list(gap.missing), [(0, 10)])

    def test_multiple_children_one_bad_reports_all_gaps(self):
        store, parent_id, _ = build_initial_store()
        txn = store.begin()
        c2 = txn.insert_child("k", 0, 20)
        c3 = txn.insert_child("k", 8, 12)
        with self.assertRaises(TemporalForeignKeyError) as ctx:
            txn.commit()
        reported = {gap.child_row_id: list(gap.missing) for gap in ctx.exception.gaps}
        self.assertEqual(reported[c2], [(10, 20)])
        self.assertEqual(reported[c3], [(10, 12)])


class TestEdgeCasesAndValidation(unittest.TestCase):
    def test_zero_length_and_reversed_intervals_rejected(self):
        store = TemporalStore()
        txn = store.begin()
        with self.assertRaises(ValueError):
            txn.insert_parent("k", 5, 5)
        with self.assertRaises(ValueError):
            txn.insert_child("k", 9, 1)

    def test_update_to_invalid_interval_rejected(self):
        store, parent_id, _ = build_initial_store()
        txn = store.begin()
        with self.assertRaises(ValueError):
            txn.update_parent(parent_id, end=-1)

    def test_half_open_boundary_adjacency_is_valid(self):
        """父区间 [0,5) 与子区间 [0,5)：半开端点处不算缺口。"""
        store = TemporalStore()
        txn = store.begin()
        txn.insert_parent("k", 0, 5)
        txn.insert_child("k", 0, 5)
        txn.commit()
        self.assertEqual(len(store.list_parents()), 1)

    def test_missing_row_operations_raise(self):
        store, _, _ = build_initial_store()
        txn = store.begin()
        with self.assertRaises(RowNotFoundError):
            txn.update_parent(999, end=1)
        with self.assertRaises(RowNotFoundError):
            txn.delete_child(999)

    def test_date_endpoints_work_with_comparable_types(self):
        from datetime import date

        store = TemporalStore()
        txn = store.begin()
        txn.insert_parent("k", date(2026, 1, 1), date(2026, 2, 1))
        txn.insert_child("k", date(2026, 1, 10), date(2026, 1, 20))
        txn.commit()
        self.assertEqual(
            store.get_child(store.list_children()[0].row_id).interval,
            (date(2026, 1, 10), date(2026, 1, 20)),
        )

    def test_separate_keys_do_not_interfere(self):
        store = TemporalStore()
        txn = store.begin()
        txn.insert_parent("a", 0, 10)
        txn.insert_parent("b", 0, 3)
        txn.insert_child("a", 0, 10)
        txn.insert_child("b", 0, 3)
        txn.commit()
        # 缩减 b 的父区间使 b 的子记录失保，a 不受影响
        b_parent = [r.row_id for r in store.list_parents() if r.key == "b"][0]
        txn2 = store.begin()
        txn2.update_parent(b_parent, end=1)
        with self.assertRaises(TemporalForeignKeyError) as ctx:
            txn2.commit()
        self.assertEqual(len(ctx.exception.gaps), 1)
        self.assertEqual(ctx.exception.gaps[0].key, "b")
        self.assertEqual(list(ctx.exception.gaps[0].missing), [(1, 3)])


if __name__ == "__main__":
    unittest.main()
