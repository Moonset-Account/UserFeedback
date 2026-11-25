import unittest

from grouping_sets import (
    GroupingSetsEngine,
    GroupingSetsSpec,
    UnmatchedRetractionError,
)
from tests.support import index_rows, row_at


class PartialMatchAcrossSetsTests(unittest.TestCase):
    """一行数据对不同分组集合可能有不同粒度，撤回的匹配也随之不同。"""

    def setUp(self):
        self.spec = GroupingSetsSpec(
            grouping_columns=["a", "b"],
            grouping_sets=[["a", "b"], ["a"]],
            aggregates=[("count", "*", "n"), ("sum", "v", "s")],
        )

    def test_retraction_matching_only_the_coarser_set_is_still_rejected(self):
        engine = GroupingSetsEngine(self.spec)
        engine.apply(adds=[{"a": 1, "b": 2, "v": 5}])
        # (1,3,5) 在集合 ["a"] 里是同一组、同一载荷，但在 ["a","b"] 里不存在
        with self.assertRaises(UnmatchedRetractionError):
            engine.apply(retracts=[{"a": 1, "b": 3, "v": 5}])
        self.assertEqual(engine.stored_row_counts(), (1, 1))

    def test_non_strict_counts_it_as_one_ignored_row(self):
        engine = GroupingSetsEngine(self.spec, strict=False)
        engine.apply(adds=[{"a": 1, "b": 2, "v": 5}])
        result = engine.apply(retracts=[{"a": 1, "b": 3, "v": 5}])
        self.assertEqual(result.unmatched_retractions, 1)
        # 该撤回在 ["a"] 里是能匹配的，因此那里确实被扣了一次，
        # 且该组因为最后一行消失而从结果里移除
        self.assertEqual(engine.stored_row_counts(), (1, 0))
        self.assertEqual(result.snapshot[0][0].aggregates, (1, 5))
        self.assertEqual(result.snapshot[1], ())
        self.assertIn((1, (1, None)), {(r.set_index, r.grouping_values) for r in result.deletes})

    def test_non_strict_unmatched_is_not_multiplied_by_set_count(self):
        spec = GroupingSetsSpec(
            grouping_columns=["g"],
            grouping_sets=[["g"], ["g"], ["g"], []],
            aggregates=[("count", "*", "n")],
        )
        engine = GroupingSetsEngine(spec, strict=False)
        result = engine.apply(retracts=[{"g": "missing"}])
        self.assertEqual(result.unmatched_retractions, 1)
        self.assertEqual(result.unmatched_by_set, (1, 1, 1, 1))


class SameBatchNettingTests(unittest.TestCase):
    def test_retracting_a_row_added_in_the_same_batch_is_allowed(self):
        spec = GroupingSetsSpec(
            ["g"], [["g"], []], aggregates=[("count", "*", "n")]
        )
        engine = GroupingSetsEngine(spec)
        row = {"g": "x"}
        result = engine.apply(adds=[row], retracts=[row])
        self.assertEqual(result.upserts, ())
        self.assertEqual(result.deletes, ())
        self.assertEqual(engine.stored_row_counts(), (0, 0))

    def test_same_batch_add_can_cover_a_retraction_of_an_equal_row(self):
        spec = GroupingSetsSpec(
            ["g"], [["g"]], aggregates=[("count", "*", "n")]
        )
        engine = GroupingSetsEngine(spec)
        result = engine.apply(
            adds=[{"g": "x"}], retracts=[{"g": "x"}]
        )
        self.assertEqual(result.upserts, ())
        self.assertEqual(engine.stored_row_counts(), (0,))

    def test_net_negative_beyond_available_is_rejected(self):
        spec = GroupingSetsSpec(
            ["g"], [["g"]], aggregates=[("count", "*", "n")]
        )
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"g": "x"}])
        with self.assertRaises(UnmatchedRetractionError):
            engine.apply(
                adds=[{"g": "x"}],
                retracts=[{"g": "x"}, {"g": "x"}, {"g": "x"}],
            )
        self.assertEqual(engine.stored_row_counts(), (1,))

    def test_zero_net_delta_does_not_touch_state(self):
        spec = GroupingSetsSpec(
            ["g"], [["g"], []], aggregates=[("count", "*", "n")]
        )
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"g": "x"}])
        before = engine.current_snapshot()
        counts_before = engine.stored_row_counts()
        result = engine.apply(
            adds=[{"g": "x"}], retracts=[{"g": "x"}]
        )
        self.assertEqual(engine.stored_row_counts(), counts_before)
        self.assertEqual(result.snapshot, before)


class AggregateOutputNamingTests(unittest.TestCase):
    def test_aggregate_outputs_map_to_dict_keys(self):
        spec = GroupingSetsSpec(
            ["g"],
            [["g"], []],
            aggregates=[
                ("count", "*", "row_count"),
                ("sum", "v", "v_total"),
                ("min", "v", "v_min"),
            ],
        )
        engine = GroupingSetsEngine(spec)
        result = engine.apply(adds=[{"g": "x", "v": 3}])
        mapping = result.rows[0].as_dict(spec.grouping_columns, spec.aggregate_outputs)
        self.assertEqual(
            mapping,
            {
                "g": "x",
                "row_count": 1,
                "v_total": 3,
                "v_min": 3,
                "__grouping__": (0,),
                "__set_index__": 0,
            },
        )

    def test_same_function_on_different_columns(self):
        spec = GroupingSetsSpec(
            ["g"],
            [["g"]],
            aggregates=[("sum", "x", "sx"), ("sum", "y", "sy")],
        )
        engine = GroupingSetsEngine(spec)
        result = engine.apply(adds=[{"g": 1, "x": 2, "y": 10}])
        self.assertEqual(result.snapshot[0][0].aggregates, (2, 10))


class MultiColumnKeyTests(unittest.TestCase):
    def test_keys_with_none_and_mixed_scalars_do_not_crash_sorting(self):
        spec = GroupingSetsSpec(
            ["a", "b"],
            [["a", "b"], []],
            aggregates=[("count", "*", "n")],
        )
        engine = GroupingSetsEngine(spec)
        result = engine.apply(
            adds=[
                {"a": None, "b": 1},
                {"a": 1, "b": None},
                {"a": "x", "b": 1},
                {"a": 1, "b": 1},
            ]
        )
        values = [row.grouping_values for row in result.snapshot[0]]
        # 排序是确定的：None 先于非 None，再按类型名与 repr
        self.assertEqual(len(values), 4)
        self.assertEqual(values, sorted(values, key=_expected_sort_key))

    def test_zero_column_grouping_key(self):
        spec = GroupingSetsSpec(
            [], [[]], aggregates=[("count", "*", "n"), ("sum", "v", "s")]
        )
        engine = GroupingSetsEngine(spec)
        result = engine.apply(adds=[{"v": 1}, {"v": 2}, {"v": None}])
        row = result.snapshot[0][0]
        self.assertEqual(row.grouping_values, ())
        self.assertEqual(row.grouping_bits, ())
        self.assertEqual(row.aggregates, (3, 3))


def _expected_sort_key(values):
    from grouping_sets.model import sort_key_for

    return sort_key_for(values)


if __name__ == "__main__":
    unittest.main()
