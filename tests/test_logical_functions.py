"""Hand-computed expectations for the three window functions."""

import unittest

from window_planning import (
    Direction,
    Nulls,
    OrderKey,
    TiePolicy,
    WindowFunction,
    WindowSpec,
    load_rows,
    reference_values,
)


def _key(column: str, direction=Direction.ASC, nulls=Nulls.LAST) -> OrderKey:
    return OrderKey(column, direction, nulls)


class RowNumberTest(unittest.TestCase):
    def test_partitioned_row_numbers_follow_declared_order_and_reset(self):
        rows = load_rows(
            [
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 20},
                {"dept": "B", "salary": 5},
            ]
        )
        spec = WindowSpec(
            "rn",
            ("dept",),
            (_key("salary"),),
            TiePolicy.ROW_NUMBER,
            WindowFunction.ROW_NUMBER,
        )
        values = reference_values(rows, [spec])["rn"]
        # Tied salary rows are distinguished by the physical row-number tail.
        self.assertEqual(values, [1, 2, 3, 1])

    def test_row_number_without_partition_numbers_the_whole_table(self):
        rows = load_rows([{"v": 2}, {"v": 1}, {"v": 2}])
        spec = WindowSpec(
            "rn", (), (_key("v"),), TiePolicy.ROW_NUMBER, WindowFunction.ROW_NUMBER
        )
        values = reference_values(rows, [spec])["rn"]
        self.assertEqual(values, [2, 1, 3])

    def test_row_number_without_ordering_keys_uses_original_order(self):
        rows = load_rows([{"v": 9}, {"v": 1}, {"v": 5}])
        spec = WindowSpec(
            "rn", (), (), TiePolicy.ROW_NUMBER, WindowFunction.ROW_NUMBER
        )
        self.assertEqual(reference_values(rows, [spec])["rn"], [1, 2, 3])


class DenseRankTest(unittest.TestCase):
    def test_dense_rank_compacts_peer_groups_and_resets_per_partition(self):
        rows = load_rows(
            [
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 20},
                {"dept": "A", "salary": 20},
                {"dept": "A", "salary": 30},
                {"dept": "B", "salary": 7},
                {"dept": "B", "salary": 7},
            ]
        )
        spec = WindowSpec(
            "dr",
            ("dept",),
            (_key("salary"),),
            TiePolicy.GROUP,
            WindowFunction.DENSE_RANK,
        )
        self.assertEqual(
            reference_values(rows, [spec])["dr"],
            [1, 1, 2, 2, 3, 1, 1],
        )

    def test_dense_rank_desc_orders_peers_from_high_to_low(self):
        rows = load_rows([{"v": 1}, {"v": 3}, {"v": 3}, {"v": 2}])
        spec = WindowSpec(
            "dr",
            (),
            (_key("v", Direction.DESC, Nulls.LAST),),
            TiePolicy.GROUP,
            WindowFunction.DENSE_RANK,
        )
        self.assertEqual(reference_values(rows, [spec])["dr"], [3, 1, 1, 2])

    def test_nulls_form_their_own_peer_group(self):
        rows = load_rows([{"v": None}, {"v": 1}, {"v": None}, {"v": 1}])
        spec_nulls_first = WindowSpec(
            "dr",
            (),
            (_key("v", Direction.ASC, Nulls.FIRST),),
            TiePolicy.GROUP,
            WindowFunction.DENSE_RANK,
        )
        self.assertEqual(
            reference_values(rows, [spec_nulls_first])["dr"], [1, 2, 1, 2]
        )
        spec_nulls_last = WindowSpec(
            "dr",
            (),
            (_key("v", Direction.ASC, Nulls.LAST),),
            TiePolicy.GROUP,
            WindowFunction.DENSE_RANK,
        )
        self.assertEqual(
            reference_values(rows, [spec_nulls_last])["dr"], [2, 1, 2, 1]
        )

    def test_desc_nulls_last_agrees_between_reference_and_executor(self):
        from window_planning import (
            FeeSchedule,
            execute_plan,
            plan_windows,
            verify,
        )

        fee = FeeSchedule(
            {1: 1, 2: 2, 3: 3, 4: 4, 5: 5},
            {4: 2, 8: 4, 16: 8},
        )
        rows = load_rows([{"v": None}, {"v": 1}, {"v": 3}, {"v": 2}, {"v": None}])
        spec = WindowSpec(
            "rn",
            (),
            (_key("v", Direction.DESC, Nulls.LAST),),
            TiePolicy.ROW_NUMBER,
            WindowFunction.ROW_NUMBER,
        )
        plan = plan_windows(rows, [spec], fee)
        result = execute_plan(rows, [spec], plan)
        verify(rows, [spec], plan, result, fee)
        # DESC order for non-NULLs; NULLs stay last. Original rows map back by
        # row number, so derive the physical order explicitly.
        ordered_values = []
        permutation = result.executed_steps[-1].measurement.permutation
        for index in permutation:
            ordered_values.append(rows[index]["v"])
        self.assertEqual(ordered_values, [3, 2, 1, None, None])


class CumulativeCountTest(unittest.TestCase):
    def test_count_accumulates_peer_group_sizes_in_declared_order(self):
        rows = load_rows(
            [
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 10},
                {"dept": "A", "salary": 20},
                {"dept": "A", "salary": 30},
                {"dept": "A", "salary": 30},
                {"dept": "B", "salary": 1},
            ]
        )
        spec = WindowSpec(
            "cc",
            ("dept",),
            (_key("salary"),),
            TiePolicy.GROUP,
            WindowFunction.CUMULATIVE_COUNT,
        )
        # A: peers (10,10)->2, (20)->3, (30,30)->5; B restarts at 1.
        self.assertEqual(
            reference_values(rows, [spec])["cc"], [2, 2, 3, 5, 5, 1]
        )

    def test_cumulative_count_under_desc_order(self):
        rows = load_rows([{"v": 1}, {"v": 1}, {"v": 2}, {"v": 3}, {"v": 3}])
        spec = WindowSpec(
            "cc",
            (),
            (_key("v", Direction.DESC, Nulls.LAST),),
            TiePolicy.GROUP,
            WindowFunction.CUMULATIVE_COUNT,
        )
        # DESC peers: 3,3 -> 2; 2 -> 3; 1,1 -> 5, mapped back to original rows.
        self.assertEqual(reference_values(rows, [spec])["cc"], [5, 5, 3, 2, 2])

    def test_multiple_ordering_keys_create_compound_peers(self):
        rows = load_rows(
            [
                {"a": 1, "b": 1},
                {"a": 1, "b": 2},
                {"a": 1, "b": 2},
                {"a": 2, "b": 0},
            ]
        )
        spec = WindowSpec(
            "cc",
            (),
            (_key("a"), _key("b")),
            TiePolicy.GROUP,
            WindowFunction.CUMULATIVE_COUNT,
        )
        self.assertEqual(reference_values(rows, [spec])["cc"], [1, 3, 3, 4])


class EmptyAndSingletonTest(unittest.TestCase):
    def test_empty_table_produces_empty_columns(self):
        spec = WindowSpec(
            "rn", (), (_key("v"),), TiePolicy.ROW_NUMBER, WindowFunction.ROW_NUMBER
        )
        self.assertEqual(reference_values([], [spec]), {"rn": []})

    def test_single_row_is_its_only_peer(self):
        rows = load_rows([{"v": 42}])
        rn = WindowSpec(
            "rn", (), (_key("v"),), TiePolicy.ROW_NUMBER, WindowFunction.ROW_NUMBER
        )
        dr = WindowSpec(
            "dr", (), (_key("v"),), TiePolicy.GROUP, WindowFunction.DENSE_RANK
        )
        cc = WindowSpec(
            "cc",
            (),
            (_key("v"),),
            TiePolicy.GROUP,
            WindowFunction.CUMULATIVE_COUNT,
        )
        self.assertEqual(reference_values(rows, [rn, dr, cc])["rn"], [1])
        self.assertEqual(reference_values(rows, [rn, dr, cc])["dr"], [1])
        self.assertEqual(reference_values(rows, [rn, dr, cc])["cc"], [1])


if __name__ == "__main__":
    unittest.main()
