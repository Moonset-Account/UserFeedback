"""Tests for specification validation, loading and dependency resolution."""

import unittest

from window_planning import (
    Direction,
    DependencyError,
    Nulls,
    OrderKey,
    ROWNO_COLUMN,
    SpecError,
    TiePolicy,
    WindowFunction,
    WindowSpec,
    load_rows,
    topo_order,
    validate_specs,
)


def _ok(name: str = "w", **overrides: object) -> WindowSpec:
    params = {
        "name": name,
        "partition_by": ("dept",),
        "order_by": (OrderKey("salary", Direction.ASC, Nulls.LAST),),
        "tie_policy": TiePolicy.ROW_NUMBER,
        "function": WindowFunction.ROW_NUMBER,
    }
    params.update(overrides)
    return WindowSpec(**params)  # type: ignore[arg-type]


class LoadRowsTest(unittest.TestCase):
    def test_injects_original_row_number(self):
        rows = load_rows([{"a": 1}, {"a": 2}])
        self.assertEqual([row[ROWNO_COLUMN] for row in rows], [0, 1])

    def test_rejects_reserved_column_name(self):
        with self.assertRaises(SpecError):
            load_rows([{ROWNO_COLUMN: 1}])

    def test_rejects_inconsistent_schemas(self):
        with self.assertRaises(SpecError):
            load_rows([{"a": 1}, {"a": 2, "b": 3}])

    def test_empty_table_is_supported(self):
        self.assertEqual(load_rows([]), [])

    def test_rejects_non_dict_rows(self):
        with self.assertRaises(SpecError):
            load_rows([(1, 2)])  # type: ignore[list-item]


class SpecValidationTest(unittest.TestCase):
    def setUp(self):
        self.base = frozenset({"dept", "salary", "bonus"})

    def test_row_number_requires_row_number_policy(self):
        spec = _ok(
            tie_policy=TiePolicy.GROUP,
            function=WindowFunction.ROW_NUMBER,
        )
        with self.assertRaises(SpecError):
            validate_specs([spec], self.base)

    def test_dense_rank_requires_group_policy(self):
        spec = _ok(
            tie_policy=TiePolicy.ROW_NUMBER,
            function=WindowFunction.DENSE_RANK,
        )
        with self.assertRaises(SpecError):
            validate_specs([spec], self.base)

    def test_cumulative_count_requires_group_policy(self):
        spec = _ok(
            tie_policy=TiePolicy.ROW_NUMBER,
            function=WindowFunction.CUMULATIVE_COUNT,
        )
        with self.assertRaises(SpecError):
            validate_specs([spec], self.base)

    def test_rejects_duplicate_window_names(self):
        with self.assertRaises(SpecError):
            validate_specs([_ok("w"), _ok("w")], self.base)

    def test_rejects_repeated_partition_and_order_columns(self):
        bad_partition = _ok(partition_by=("dept", "dept"))
        with self.assertRaises(SpecError):
            validate_specs([bad_partition], self.base)
        bad_order = _ok(
            order_by=(
                OrderKey("salary", Direction.ASC, Nulls.LAST),
                OrderKey("salary", Direction.DESC, Nulls.FIRST),
            )
        )
        with self.assertRaises(SpecError):
            validate_specs([bad_order], self.base)

    def test_rejects_column_used_both_as_partition_and_order(self):
        spec = _ok(
            partition_by=("dept",),
            order_by=(OrderKey("dept", Direction.ASC, Nulls.LAST),),
        )
        with self.assertRaises(SpecError):
            validate_specs([spec], self.base)

    def test_rejects_more_than_eight_windows(self):
        specs = [_ok(f"w{i}") for i in range(9)]
        with self.assertRaises(SpecError):
            validate_specs(specs, self.base)

    def test_rejects_unknown_column_reference(self):
        spec = _ok(partition_by=("ghost",))
        with self.assertRaises(DependencyError):
            validate_specs([spec], self.base)

    def test_dependency_edges_are_derived_from_generated_columns(self):
        first = _ok("w1")
        second = _ok(
            "w2",
            partition_by=("dept",),
            order_by=(OrderKey("w1", Direction.ASC, Nulls.LAST),),
            tie_policy=TiePolicy.GROUP,
            function=WindowFunction.DENSE_RANK,
        )
        deps = validate_specs([first, second], self.base)
        self.assertEqual(deps["w1"], [])
        self.assertEqual(deps["w2"], ["w1"])

    def test_self_dependency_is_rejected(self):
        spec = _ok(
            "w1",
            order_by=(OrderKey("w1", Direction.ASC, Nulls.LAST),),
        )
        with self.assertRaises(DependencyError):
            validate_specs([spec], self.base)

    def test_dependency_cycle_is_rejected(self):
        w1 = _ok(
            "w1",
            order_by=(OrderKey("w2", Direction.ASC, Nulls.LAST),),
        )
        w2 = _ok(
            "w2",
            order_by=(OrderKey("w1", Direction.ASC, Nulls.LAST),),
        )
        with self.assertRaises(DependencyError):
            validate_specs([w1, w2], self.base)

    def test_topo_order_respects_edges_and_is_deterministic(self):
        w1 = _ok("w1")
        w2 = _ok(
            "w2",
            order_by=(OrderKey("w1", Direction.ASC, Nulls.LAST),),
            tie_policy=TiePolicy.GROUP,
            function=WindowFunction.DENSE_RANK,
        )
        deps = validate_specs([w2, w1], self.base)
        order = topo_order([w2, w1], deps)
        self.assertEqual([spec.name for spec in order], ["w1", "w2"])


if __name__ == "__main__":
    unittest.main()
