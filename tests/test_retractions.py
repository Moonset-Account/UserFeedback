import unittest

from grouping_sets import (
    GroupingSetsEngine,
    GroupingSetsSpec,
    UnmatchedRetractionError,
)
from tests.support import index_rows, row_at


def make_engine(strict=True):
    spec = GroupingSetsSpec(
        grouping_columns=["region", "product"],
        grouping_sets=[["region", "product"], ["region"], []],
        aggregates=[
            ("count", "*", "n"),
            ("sum", "amount", "total"),
            ("min", "amount", "lowest"),
        ],
    )
    return GroupingSetsEngine(spec, strict=strict)


class RetractionTests(unittest.TestCase):
    def test_last_row_removal_drops_the_group_entirely(self):
        engine = make_engine()
        engine.apply(
            adds=[
                {"region": "n", "product": "a", "amount": 5},
                {"region": "n", "product": "b", "amount": 7},
            ]
        )
        result = engine.apply(
            retracts=[{"region": "n", "product": "a", "amount": 5}]
        )
        leaf_keys = {row.grouping_values for row in result.snapshot[0]}
        self.assertNotIn(("n", "a"), leaf_keys)
        self.assertIn(("n", "b"), leaf_keys)
        # region subtotal survives because "n"/"b" remains
        self.assertEqual(
            row_at(result.snapshot, 1, ("n", None)).aggregates, (1, 7, 7)
        )
        # but the leaf row is reported as delete with the old values
        deleted = [row for row in result.deletes if row.set_index == 0]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0].grouping_values, ("n", "a"))
        self.assertEqual(deleted[0].aggregates, (1, 5, 5))

    def test_region_subtotal_disappears_when_last_row_of_region_goes(self):
        engine = make_engine()
        engine.apply(
            adds=[
                {"region": "n", "product": "a", "amount": 5},
                {"region": "s", "product": "b", "amount": 2},
            ]
        )
        result = engine.apply(
            retracts=[{"region": "n", "product": "a", "amount": 5}]
        )
        sub_keys = {row.grouping_values for row in result.snapshot[1]}
        self.assertEqual(sub_keys, {("s", None)})
        deleted_sets = {(row.set_index, row.grouping_values) for row in result.deletes}
        self.assertIn((1, ("n", None)), deleted_sets)
        self.assertNotIn((1, ("s", None)), deleted_sets)

    def test_grand_total_never_disappears(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 5}])
        result = engine.apply(
            retracts=[{"region": "n", "product": "a", "amount": 5}]
        )
        grand = row_at(result.snapshot, 2, (None, None))
        self.assertIsNotNone(grand)
        self.assertEqual(grand.aggregates, (0, None, None))
        self.assertNotIn(
            (2, (None, None)),
            {(row.set_index, row.grouping_values) for row in result.deletes},
        )

    def test_retracting_one_of_several_identical_rows_keeps_the_group(self):
        engine = make_engine()
        engine.apply(
            adds=[
                {"region": "n", "product": "a", "amount": 3},
                {"region": "n", "product": "a", "amount": 3},
            ]
        )
        result = engine.apply(
            retracts=[{"region": "n", "product": "a", "amount": 3}]
        )
        leaf = row_at(result.snapshot, 0, ("n", "a"))
        self.assertEqual(leaf.aggregates, (1, 3, 3))
        self.assertEqual(result.deletes, ())

    def test_unmatched_retraction_raises_and_leaves_state_untouched(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 4}])
        before = engine.current_snapshot()
        before_index = engine.batch_index
        with self.assertRaises(UnmatchedRetractionError):
            engine.apply(
                retracts=[
                    {"region": "n", "product": "a", "amount": 4},
                    {"region": "n", "product": "a", "amount": 999},
                ]
            )
        self.assertEqual(engine.current_snapshot(), before)
        self.assertEqual(engine.batch_index, before_index)
        self.assertEqual(engine.stored_row_counts(), (1, 1, 1))

    def test_retraction_that_would_drive_counts_negative_is_rejected(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 4}])
        with self.assertRaises(UnmatchedRetractionError):
            engine.apply(
                retracts=[
                    {"region": "n", "product": "a", "amount": 4},
                    {"region": "n", "product": "a", "amount": 4},
                ]
            )

    def test_non_strict_mode_ignores_and_counts_unmatched_retractions(self):
        engine = make_engine(strict=False)
        result = engine.apply(
            retracts=[{"region": "n", "product": "a", "amount": 4}]
        )
        self.assertEqual(result.unmatched_retractions, 1)
        self.assertEqual(result.upserts, ())
        self.assertEqual(result.deletes, ())
        self.assertEqual(engine.stored_row_counts(), (0, 0, 0))

    def test_non_strict_mode_still_applies_the_matched_part(self):
        engine = make_engine(strict=False)
        engine.apply(
            adds=[
                {"region": "n", "product": "a", "amount": 4},
                {"region": "n", "product": "a", "amount": 4},
            ]
        )
        result = engine.apply(
            retracts=[
                {"region": "n", "product": "a", "amount": 4},
                {"region": "n", "product": "a", "amount": 4},
                {"region": "n", "product": "a", "amount": 4},
            ]
        )
        self.assertEqual(result.unmatched_retractions, 1)
        self.assertEqual(engine.stored_row_counts(), (0, 0, 0))
        self.assertEqual(
            row_at(result.snapshot, 2, (None, None)).aggregates, (0, None, None)
        )

    def test_add_and_retract_within_one_batch_net_out(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 2}])
        result = engine.apply(
            adds=[{"region": "n", "product": "b", "amount": 1}],
            retracts=[{"region": "n", "product": "a", "amount": 2}],
        )
        leaf_keys = {row.grouping_values for row in result.snapshot[0]}
        self.assertEqual(leaf_keys, {("n", "b")})
        # subtotal for n stays at one row: updated, not deleted+inserted
        subtotal = row_at(result.snapshot, 1, ("n", None))
        self.assertEqual(subtotal.aggregates, (1, 1, 1))
        self.assertNotIn((1, ("n", None)), {(r.set_index, r.grouping_values) for r in result.deletes})

    def test_retracting_after_retracting_to_zero_then_adding_again(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 2}])
        engine.apply(retracts=[{"region": "n", "product": "a", "amount": 2}])
        self.assertEqual(engine.stored_row_counts(), (0, 0, 0))
        result = engine.apply(adds=[{"region": "n", "product": "a", "amount": 9}])
        leaf = row_at(result.snapshot, 0, ("n", "a"))
        self.assertEqual(leaf.aggregates, (1, 9, 9))
        self.assertIn((0, ("n", "a")), {(r.set_index, r.grouping_values) for r in result.upserts})

    def test_retraction_with_different_null_pattern_is_a_different_row(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": None}])
        with self.assertRaises(UnmatchedRetractionError):
            engine.apply(retracts=[{"region": "n", "product": "a", "amount": 0}])
        result = engine.apply(
            retracts=[{"region": "n", "product": "a", "amount": None}]
        )
        self.assertEqual(engine.stored_row_counts(), (0, 0, 0))
        self.assertIn((0, ("n", "a")), {(r.set_index, r.grouping_values) for r in result.deletes})


class RetractionDoesNotDoubleSubtractTests(unittest.TestCase):
    def test_one_retract_per_independent_state(self):
        spec = GroupingSetsSpec(
            grouping_columns=["g"],
            grouping_sets=[["g"], ["g"], []],
            aggregates=[("count", "*", "n"), ("sum", "v", "s")],
        )
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"g": "x", "v": 1}, {"g": "x", "v": 1}])
        self.assertEqual(engine.stored_row_counts(), (2, 2, 2))
        result = engine.apply(retracts=[{"g": "x", "v": 1}])
        # 每个分组集合状态只被扣一次，不因为集合重复而扣两次
        self.assertEqual(engine.stored_row_counts(), (1, 1, 1))
        for block in result.snapshot:
            self.assertEqual(block[0].aggregates, (1, 1))
        self.assertEqual(result.deletes, ())

    def test_retracting_duplicated_set_twice_really_needs_two_rows(self):
        spec = GroupingSetsSpec(
            grouping_columns=["g"],
            grouping_sets=[["g"], ["g"]],
            aggregates=[("count", "*", "n")],
        )
        engine = GroupingSetsEngine(spec)
        engine.apply(adds=[{"g": "x"}])
        with self.assertRaises(UnmatchedRetractionError):
            engine.apply(retracts=[{"g": "x"}, {"g": "x"}])
        self.assertEqual(engine.stored_row_counts(), (1, 1))

    def test_stored_row_counts_track_every_set_independently(self):
        spec = GroupingSetsSpec(
            grouping_columns=["a", "b"],
            grouping_sets=[["a", "b"], ["a"], [], ["a", "b"]],
            aggregates=[("count", "*", "n")],
        )
        engine = GroupingSetsEngine(spec)
        engine.apply(
            adds=[
                {"a": 1, "b": 2},
                {"a": 1, "b": 3},
            ]
        )
        # 4 个分组集合，2 行数据 => 每个状态 2 行
        self.assertEqual(engine.stored_row_counts(), (2, 2, 2, 2))
        self.assertEqual(engine.group_counts(), (2, 1, 1, 2))


if __name__ == "__main__":
    unittest.main()
