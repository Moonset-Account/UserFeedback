"""播报选择：最短、预算、等长、同音不可区分。"""

import unittest

from confirm import (
    CandidateBook,
    HomophoneTable,
    Segment,
    plan_confirmation,
)
from tests.helpers import amount_tail_book, book, cand


def _plan(book, target, required, segments, budget, **kw):
    return plan_confirmation(book, target, required, segments, budget, **kw)


class ShortestWithinBudgetTests(unittest.TestCase):
    def test_picks_cheaper_sufficient_segment_over_expensive_all_in_one(self):
        # 单段全字段 800ms vs 分段合计 700ms；700ms 更短，应选分段
        segments = (
            Segment(0, (1,), 300),
            Segment(1, (2,), 400),
            Segment(2, (1, 2), 800),
        )
        plan = _plan(amount_tail_book(), "A", (1, 2), segments, 1000)
        self.assertEqual(plan.total_duration_ms, 700)
        self.assertEqual(plan.field_ids, (1, 2))
        self.assertEqual(plan.segment_indices, (0, 1))

    def test_returns_none_when_budget_too_small(self):
        segments = (Segment(0, (1,), 300), Segment(1, (2,), 400))
        self.assertIsNone(_plan(amount_tail_book(), "A", (1, 2), segments, 500))

    def test_required_field_forces_expensive_segment(self):
        # 必读字段 1+2，只有第 2 段能同时给全，预算刚好够
        segments = (
            Segment(0, (1,), 300),
            Segment(1, (2,), 400),
            Segment(2, (1, 2), 900),
        )
        plan = _plan(amount_tail_book(), "A", (1, 2), segments, 900)
        self.assertEqual(plan.total_duration_ms, 700)
        plan2 = _plan(amount_tail_book(), "A", (1, 2), segments, 600)
        self.assertIsNone(plan2)

    def test_required_subset_lets_single_field_plan(self):
        # 只必读字段 1，且没有其他候选，字段 1 单段即可（但需能区分）
        single = book(cand("A", f1=("一百元",)), )
        segments = (Segment(0, (1,), 300), Segment(1, (1,), 500))
        plan = _plan(single, "A", (1,), segments, 1000)
        self.assertEqual(plan.total_duration_ms, 300)
        self.assertEqual(plan.segment_indices, (0,))


class TieBreakTests(unittest.TestCase):
    def test_equal_length_prefers_smaller_field_id_sequence(self):
        # 两段都是 300ms，都能区分（没有其他候选），必读为空 -> 选编号小的
        single = book(cand("A", f1=("一",), f2=("二",)))
        segments = (Segment(0, (2,), 300), Segment(1, (1,), 300))
        plan = _plan(single, "A", (), segments, 1000)
        self.assertEqual(plan.field_ids, (1,))

    def test_equal_length_prefers_smaller_segment_field_sequence(self):
        # (1,2) 与 (1,3) 等长，编号序列 (1,2) 更小
        single = book(cand("A", f1=("一",), f2=("二",), f3=("三",)))
        segments = (Segment(0, (1, 3), 400), Segment(1, (1, 2), 400))
        plan = _plan(single, "A", (), segments, 1000)
        self.assertEqual(plan.field_ids, (1, 2))

    def test_sequence_lexicographic_not_numeric_value(self):
        # (1, 10) vs (2,)：按序列字典序 (1,10) 更小；等长时仍选它
        single = book(
            cand("A", f1=("一",), f2=("二",), f10=("十",))
        )
        segments = (Segment(0, (2,), 500), Segment(1, (1, 10), 500))
        plan = _plan(single, "A", (), segments, 1000)
        self.assertEqual(plan.field_ids, (1, 10))


class DistinguishabilityTests(unittest.TestCase):
    def test_shared_amount_cannot_distinguish_requires_tail(self):
        book_ = amount_tail_book()
        segments = (Segment(0, (1,), 300), Segment(1, (2,), 400))
        plan = _plan(book_, "A", (1,), segments, 1000)
        # 只播字段 1 无法区分，即便预算足够也必须带上字段 2
        self.assertEqual(plan.field_ids, (1, 2))

    def test_homophone_aliases_treated_as_identical(self):
        # B 的尾号写法与 A 同音（经同音表连成一个等价类），则尾号无法区分
        table = HomophoneTable([("1234", "一二三四", "yi er san si", "5678")])
        book_ = book(
            cand("A", f1=("一百元",), f2=("1234", "一二三四")),
            cand("B", f1=("一百元",), f2=("5678", "五六七八")),
        )
        segments = (Segment(0, (1,), 300), Segment(1, (2,), 400))
        plan = _plan(book_, "A", (1,), segments, 1000, homophones=table)
        # 尾号同音后唯一可区分的也没有了 -> 无解
        self.assertIsNone(plan)

    def test_overlapping_partial_alias_blocks_distinguishing(self):
        # A 尾号别名含 “一二三四”，B 尾号别名也含同一个听感键 -> 不能区分
        book_ = book(
            cand("A", f2=("1234", "一二三四")),
            cand("B", f2=("一二三四")),
        )
        segments = (Segment(0, (2,), 400),)
        self.assertIsNone(_plan(book_, "A", (2,), segments, 1000))

    def test_missing_field_in_other_counts_as_distinct(self):
        # B 没有字段 3，播字段 3 一定能区分
        book_ = book(
            cand("A", f1=("同",), f3=("甲",)),
            cand("B", f1=("同",)),
        )
        segments = (Segment(0, (3,), 200), Segment(1, (1,), 100))
        plan = _plan(book_, "A", (), segments, 1000)
        # 字段 1 无法区分；只有字段 3 能区分
        self.assertEqual(plan.field_ids, (3,))

    def test_multi_candidate_needs_distinguish_all(self):
        book_ = book(
            cand("A", f1=("一",), f2=("x",)),
            cand("B", f1=("一",), f2=("y",)),
            cand("C", f1=("一",), f2=("x",)),  # 与 A 完全相同
        )
        segments = (Segment(0, (1,), 100), Segment(1, (2,), 100))
        # A 与 C 在任何字段上都同音，无法区分
        self.assertIsNone(_plan(book_, "A", (), segments, 1000))


class ValidationTests(unittest.TestCase):
    def test_unknown_target_raises(self):
        with self.assertRaises(KeyError):
            _plan(amount_tail_book(), "Z", (), (Segment(0, (1,), 100),), 1000)

    def test_required_field_not_in_target_raises(self):
        with self.assertRaises(ValueError):
            _plan(amount_tail_book(), "A", (9,), (Segment(0, (1,), 100),), 1000)

    def test_segment_with_unknown_field_raises(self):
        with self.assertRaises(ValueError):
            _plan(amount_tail_book(), "A", (), (Segment(0, (9,), 100),), 1000)

    def test_duplicate_segment_index_raises(self):
        with self.assertRaises(ValueError):
            _plan(
                amount_tail_book(),
                "A",
                (),
                (Segment(0, (1,), 100), Segment(0, (2,), 100)),
                1000,
            )


if __name__ == "__main__":
    unittest.main()
