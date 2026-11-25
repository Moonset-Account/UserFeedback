import unittest

from grouping_sets import GroupingSetsEngine, GroupingSetsSpec
from tests.support import index_rows, row_at


def make_engine():
    spec = GroupingSetsSpec(
        grouping_columns=["region", "product"],
        grouping_sets=[["region", "product"], ["region"], []],
        aggregates=[
            ("count", "*", "n"),
            ("sum", "amount", "total"),
            ("min", "amount", "lowest"),
        ],
    )
    return GroupingSetsEngine(spec)


class BatchReportingTests(unittest.TestCase):
    def test_first_batch_reports_only_new_groups_and_subtotals(self):
        engine = make_engine()
        result = engine.apply(
            adds=[
                {"region": "n", "product": "a", "amount": 2},
                {"region": "n", "product": "b", "amount": 5},
            ]
        )
        # 2 leaf groups + 1 region subtotal + 1 grand total
        self.assertEqual(len(result.upserts), 4)
        self.assertEqual(result.deletes, ())
        self.assertEqual(result.batch_index, 0)
        self.assertTrue(result.changed)

    def test_grand_total_row_exists_at_construction_as_the_baseline(self):
        engine = make_engine()
        baseline = engine.current_snapshot()
        self.assertEqual(len(baseline), 3)
        row = baseline[2][0]
        self.assertEqual(row.grouping_values, (None, None))
        self.assertEqual(row.aggregates, (0, None, None))
        self.assertEqual(row.grouping_bits, (1, 1))
        # 没有数据的分组集合没有任何行
        self.assertEqual(baseline[0], ())
        self.assertEqual(baseline[1], ())
        self.assertEqual(engine.batch_index, -1)

    def test_empty_first_batch_changes_nothing_because_baseline_already_has_it(self):
        engine = make_engine()
        result = engine.apply()
        self.assertEqual(result.upserts, ())
        self.assertEqual(result.deletes, ())
        self.assertFalse(result.changed)
        self.assertEqual(result.snapshot, engine.current_snapshot())

    def test_second_batch_only_reports_rows_that_changed(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 2}])
        result = engine.apply(
            adds=[{"region": "s", "product": "z", "amount": 1}]
        )
        keys = {(row.set_index, row.grouping_values) for row in result.upserts}
        # new leaf group, new region subtotal, changed grand total
        self.assertEqual(
            keys,
            {(0, ("s", "z")), (1, ("s", None)), (2, (None, None))},
        )
        self.assertEqual(result.deletes, ())

    def test_batch_without_net_change_reports_nothing(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 2}])
        result = engine.apply(
            adds=[{"region": "n", "product": "a", "amount": 2}],
            retracts=[{"region": "n", "product": "a", "amount": 2}],
        )
        self.assertEqual(result.upserts, ())
        self.assertEqual(result.deletes, ())
        self.assertFalse(result.changed)
        self.assertEqual(result.batch_index, 1)

    def test_identical_add_and_retract_across_batches_nets_to_zero(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 2}])
        snapshot_before = engine.current_snapshot()
        result = engine.apply(
            adds=[{"region": "n", "product": "a", "amount": 2}],
            retracts=[{"region": "n", "product": "a", "amount": 2}],
        )
        self.assertEqual(result.snapshot, snapshot_before)

    def test_no_aggregate_change_is_not_reported_as_upsert(self):
        spec = GroupingSetsSpec(["g"], [["g"]], aggregates=[])
        engine = GroupingSetsEngine(spec)
        first = engine.apply(adds=[{"g": "x"}])
        self.assertEqual(len(first.upserts), 1)
        second = engine.apply(adds=[{"g": "x"}])
        self.assertEqual(second.upserts, ())

    def test_deleted_rows_carry_the_values_from_before_the_batch(self):
        engine = make_engine()
        engine.apply(
            adds=[
                {"region": "n", "product": "a", "amount": 4},
                {"region": "n", "product": "a", "amount": 6},
            ]
        )
        result = engine.apply(
            retracts=[
                {"region": "n", "product": "a", "amount": 4},
                {"region": "n", "product": "a", "amount": 6},
            ]
        )
        keys = {(row.set_index, row.grouping_values) for row in result.deletes}
        self.assertIn((0, ("n", "a")), keys)
        self.assertIn((1, ("n", None)), keys)
        leaf = [row for row in result.deletes if row.set_index == 0][0]
        self.assertEqual(leaf.aggregates, (2, 10, 4))
        # 空分组集合不会消失，只是归零
        self.assertNotIn((2, (None, None)), keys)
        grand = row_at(result.snapshot, 2, (None, None))
        self.assertEqual(grand.aggregates, (0, None, None))

    def test_changed_group_is_reported_as_upsert_not_delete_plus_insert(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 5}])
        result = engine.apply(
            adds=[{"region": "n", "product": "a", "amount": 5}]
        )
        keys = {(row.set_index, row.grouping_values) for row in result.upserts}
        self.assertIn((0, ("n", "a")), keys)
        self.assertEqual(result.deletes, ())
        updated = [r for r in result.upserts if r.set_index == 0][0]
        self.assertEqual(updated.aggregates, (2, 10, 5))

    def test_snapshot_is_grouped_by_set_index_and_sorted(self):
        engine = make_engine()
        result = engine.apply(
            adds=[
                {"region": "b", "product": "y", "amount": 1},
                {"region": "a", "product": "z", "amount": 1},
                {"region": "a", "product": "x", "amount": 1},
            ]
        )
        self.assertEqual(len(result.snapshot), 3)
        leaf_keys = [row.grouping_values for row in result.snapshot[0]]
        self.assertEqual(
            leaf_keys,
            [("a", "x"), ("a", "z"), ("b", "y")],
        )

    def test_result_rows_are_ordered_the_same_way_in_upserts_and_snapshot(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 1}])
        result = engine.apply(
            adds=[
                {"region": "n", "product": "b", "amount": 1},
                {"region": "n", "product": "c", "amount": 1},
            ]
        )
        upsert_keys = [(row.set_index, row.grouping_values) for row in result.upserts]
        self.assertEqual(upsert_keys, sorted(upsert_keys))

    def test_batch_index_increments_each_successful_apply(self):
        engine = make_engine()
        self.assertEqual(engine.batch_index, -1)
        engine.apply()
        engine.apply()
        self.assertEqual(engine.batch_index, 1)

    def test_current_snapshot_equals_last_batch_snapshot(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 1}])
        last = engine.apply(adds=[{"region": "n", "product": "b", "amount": 2}])
        self.assertEqual(engine.current_snapshot(), last.snapshot)

    def test_reset_returns_engine_to_an_empty_state(self):
        engine = make_engine()
        engine.apply(adds=[{"region": "n", "product": "a", "amount": 1}])
        engine.reset()
        self.assertEqual(engine.batch_index, -1)
        self.assertEqual(engine.stored_row_counts(), (0, 0, 0))
        # reset 后空分组集合仍保留零行
        grand = row_at(engine.current_snapshot(), 2, (None, None))
        self.assertEqual(grand.aggregates, (0, None, None))

    def test_many_small_batches_match_one_big_batch(self):
        rows = [
            {"region": "n", "product": "a", "amount": 1},
            {"region": "n", "product": "b", "amount": 2},
            {"region": "s", "product": "a", "amount": 3},
            {"region": "s", "product": "b", "amount": 4},
        ]
        batched = make_engine()
        for row in rows:
            batched.apply(adds=[row])
        single = make_engine()
        single.apply(adds=rows)
        self.assertEqual(batched.current_snapshot(), single.current_snapshot())

    def test_rows_and_snapshot_properties_agree(self):
        engine = make_engine()
        result = engine.apply(
            adds=[
                {"region": "n", "product": "a", "amount": 1},
                {"region": "s", "product": "b", "amount": 2},
            ]
        )
        self.assertEqual(result.rows, tuple(r for block in result.snapshot for r in block))
        self.assertEqual(engine.current_rows(), result.rows)

    def test_upsert_keys_are_unique_within_a_batch(self):
        engine = make_engine()
        result = engine.apply(
            adds=[{"region": "n", "product": "a", "amount": 1}] * 5
        )
        keys = [(row.set_index, row.grouping_values) for row in result.upserts]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
