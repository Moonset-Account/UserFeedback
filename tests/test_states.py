"""聚合状态机的合并语义测试，重点是去重计数的互斥前提。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sharedscan.states import (  # noqa: E402
    COUNT,
    COUNT_STAR,
    DISTINCT_PARTIAL,
    DISTINCT_SET,
    MAX,
    MIN,
    SUM,
    InvalidDistinctMerge,
    StateSpec,
    avg_from_pair,
    empty_value,
    finalize_state,
    merge_states,
    new_state,
    update_state,
)


class TestStateMerging(unittest.TestCase):
    def _feed(self, spec, values):
        state = new_state(spec)
        for value in values:
            update_state(state, value)
        return state

    def test_count_sum_merge_disjoint_sets(self):
        spec = StateSpec(SUM)
        a = self._feed(spec, [1, 2, None])
        b = self._feed(spec, [3, None])
        merged = merge_states(spec, a, b)
        self.assertEqual(finalize_state(spec, merged), 6)

    def test_sum_all_null_merges_to_null(self):
        spec = StateSpec(SUM)
        a = self._feed(spec, [None])
        b = self._feed(spec, [None, 4])
        merged = merge_states(spec, a, b)
        self.assertEqual(finalize_state(spec, merged), 4)
        all_null = merge_states(spec, self._feed(spec, [None]),
                                self._feed(spec, [None]))
        self.assertIsNone(finalize_state(spec, all_null))

    def test_min_max_merge(self):
        mn = StateSpec(MIN)
        mx = StateSpec(MAX)
        merged_min = merge_states(mn, self._feed(mn, [3, 9]),
                                  self._feed(mn, [None, 1]))
        merged_max = merge_states(mx, self._feed(mx, [3, 9]),
                                  self._feed(mx, [None, 1]))
        self.assertEqual(finalize_state(mn, merged_min), 1)
        self.assertEqual(finalize_state(mx, merged_max), 9)

    def test_count_states_merge(self):
        star = StateSpec(COUNT_STAR)
        cnt = StateSpec(COUNT)
        s1 = merge_states(star, self._feed(star, [None, None]),
                          self._feed(star, [None]))
        c1 = merge_states(cnt, self._feed(cnt, [1, None]),
                          self._feed(cnt, [2, None, 3]))
        self.assertEqual(finalize_state(star, s1), 3)
        self.assertEqual(finalize_state(cnt, c1), 3)

    def test_distinct_set_merge_is_exact_within_scan(self):
        spec = StateSpec(DISTINCT_SET)
        a = self._feed(spec, ["c1", "c2", "c1", None])
        b = self._feed(spec, ["c2", "c3", None])
        merged = merge_states(spec, a, b)
        # 集合并集：c1,c2,c3,NULL -> 4（不是 4+3 的部分计数相加）
        self.assertEqual(finalize_state(spec, merged), 4)

    def test_distinct_partial_refuses_merge_defensively(self):
        spec = StateSpec(DISTINCT_PARTIAL, exclusive=False)
        a = new_state(spec)
        a.value = 3
        b = new_state(spec)
        b.value = 2
        with self.assertRaises(InvalidDistinctMerge):
            merge_states(spec, a, b)

    def test_distinct_null_flag(self):
        incl = StateSpec(DISTINCT_SET, include_null=True)
        excl = StateSpec(DISTINCT_SET, include_null=False)
        s_incl = self._feed(incl, [None, None, "x"])
        s_excl = self._feed(excl, [None, None, "x"])
        self.assertEqual(finalize_state(incl, s_incl), 2)
        self.assertEqual(finalize_state(excl, s_excl), 1)

    def test_empty_values(self):
        self.assertEqual(empty_value(COUNT_STAR), 0)
        self.assertEqual(empty_value(COUNT), 0)
        self.assertEqual(empty_value(DISTINCT_SET), 0)
        self.assertIsNone(empty_value(SUM))
        self.assertIsNone(empty_value(MIN))
        self.assertIsNone(empty_value(MAX))

    def test_avg_pair_projection(self):
        self.assertEqual(avg_from_pair(10, 4), 2.5)
        self.assertIsNone(avg_from_pair(None, 0))
        self.assertIsInstance(avg_from_pair(9, 3), float)


if __name__ == "__main__":
    unittest.main()
