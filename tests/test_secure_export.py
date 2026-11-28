"""secure_export 的单元测试：正常场景、边界与交错情况。"""

import unittest

from secure_export import (
    CellSpec,
    EvaluationError,
    ExportPolicy,
    FormulaError,
    Workbook,
    export_workbook,
    parse_formula,
)


def C(sheet, addr, value=None, formula=None, level="public"):
    return CellSpec(sheet=sheet, address=addr, value=value, formula=formula, level=level)


def make_wb(cells, names=None, hidden=()):
    return Workbook(
        cells=tuple(cells),
        named_ranges=dict(names or {}),
        hidden_sheets=frozenset(hidden),
    )


def policy(publish=(), keep=(), levels=("public",)):
    return ExportPolicy(
        publish_values=frozenset(publish),
        keep_formulas=frozenset(keep),
        allowed_levels=frozenset(levels),
    )


class FreezeConstantTests(unittest.TestCase):
    def test_publish_value_freezes_to_constant(self):
        wb = make_wb([
            C("Sheet1", "A1", 10),
            C("Sheet1", "B1", formula="=A1*2"),
        ])
        res = export_workbook(wb, policy(publish=["Sheet1!B1"]))
        self.assertEqual(res.cells["Sheet1!B1"].value, 20)
        self.assertIsNone(res.cells["Sheet1!B1"].formula)
        # 被引用的 A1 未获准公开，不进入导出物
        self.assertNotIn("Sheet1!A1", res.cells)

    def test_frozen_constant_removes_formula_and_internal_refs(self):
        wb = make_wb([
            C("Sheet1", "Z9", 999999, level="internal"),
            C("Sheet1", "B1", formula="=Z9+1"),
        ])
        res = export_workbook(wb, policy(publish=["Sheet1!B1"]))
        cell = res.cells["Sheet1!B1"]
        self.assertEqual(cell.value, 1000000)
        self.assertIsNone(cell.formula)
        self.assertNotIn("Sheet1!Z9", res.cells)
        self.assertEqual(res.named_ranges, {})

    def test_static_value_cell_freezes_directly(self):
        wb = make_wb([C("Sheet1", "A1", 42)])
        res = export_workbook(wb, policy(publish=["Sheet1!A1"]))
        self.assertEqual(res.cells["Sheet1!A1"].value, 42)


class KeepFormulaTests(unittest.TestCase):
    def test_keep_formula_when_refs_permitted(self):
        wb = make_wb([
            C("Sheet1", "A1", 10),
            C("Sheet1", "B1", formula="=A1*2"),
        ])
        res = export_workbook(
            wb, policy(publish=["Sheet1!A1"], keep=["Sheet1!B1"])
        )
        self.assertEqual(res.cells["Sheet1!B1"].formula, "=A1*2")
        self.assertEqual(res.cells["Sheet1!B1"].value, 20)
        self.assertEqual(res.cells["Sheet1!A1"].value, 10)

    def test_keep_formula_denied_when_ref_not_permitted(self):
        wb = make_wb([
            C("Sheet1", "A1", 10),
            C("Sheet1", "B1", formula="=A1*2"),
        ])
        res = export_workbook(wb, policy(keep=["Sheet1!B1"]))
        self.assertNotIn("Sheet1!B1", res.cells)
        self.assertEqual(res.omitted["Sheet1!B1"], "references-not-permitted")

    def test_keep_denied_falls_back_to_constant_when_publish_allowed(self):
        wb = make_wb([
            C("Sheet1", "A1", 10, level="internal"),
            C("Sheet1", "B1", formula="=A1*2"),
        ])
        res = export_workbook(
            wb, policy(publish=["Sheet1!B1"], keep=["Sheet1!B1"])
        )
        cell = res.cells["Sheet1!B1"]
        self.assertIsNone(cell.formula)
        self.assertEqual(cell.value, 20)

    def test_mixed_chain_formula_on_frozen_constant(self):
        # 交错情形：保留公式的格子引用被固化为常量的格子
        wb = make_wb([
            C("Sheet1", "A1", 5),
            C("Sheet1", "B1", formula="=A1*2"),
            C("Sheet1", "C1", formula="=B1+1"),
        ])
        res = export_workbook(
            wb,
            policy(publish=["Sheet1!A1", "Sheet1!B1"], keep=["Sheet1!C1"]),
        )
        self.assertIsNone(res.cells["Sheet1!B1"].formula)
        self.assertEqual(res.cells["Sheet1!B1"].value, 10)
        self.assertEqual(res.cells["Sheet1!C1"].formula, "=B1+1")
        self.assertEqual(res.cells["Sheet1!C1"].value, 11)

    def test_transitive_keep_chain(self):
        wb = make_wb([
            C("Sheet1", "A1", 2),
            C("Sheet1", "B1", formula="=A1+1"),
            C("Sheet1", "C1", formula="=B1*3"),
        ])
        res = export_workbook(
            wb,
            policy(publish=["Sheet1!A1"], keep=["Sheet1!B1", "Sheet1!C1"]),
        )
        self.assertEqual(res.cells["Sheet1!C1"].formula, "=B1*3")
        self.assertEqual(res.cells["Sheet1!B1"].formula, "=A1+1")

    def test_keep_chain_broken_by_unpermitted_middle_cell(self):
        wb = make_wb([
            C("Sheet1", "A1", 2),
            C("Sheet1", "B1", formula="=A1+1"),  # 未获准
            C("Sheet1", "C1", formula="=B1*3"),
        ])
        res = export_workbook(wb, policy(keep=["Sheet1!C1"]))
        self.assertNotIn("Sheet1!C1", res.cells)
        self.assertEqual(res.omitted["Sheet1!C1"], "references-not-permitted")


class ConditionalDependencyTests(unittest.TestCase):
    def test_if_condition_counts_as_dependency(self):
        wb = make_wb([
            C("Sheet1", "A1", 7, level="internal"),
            C("Sheet1", "B1", formula='=IF(A1>5, "high", "low")'),
        ])
        res = export_workbook(wb, policy(keep=["Sheet1!B1"]))
        # 判定项 A1 未获准 -> 不能保留公式
        self.assertNotIn("Sheet1!B1", res.cells)
        self.assertEqual(res.omitted["Sheet1!B1"], "references-not-permitted")

    def test_if_condition_permitted_keeps_formula(self):
        wb = make_wb([
            C("Sheet1", "A1", 7),
            C("Sheet1", "B1", formula='=IF(A1>5, "high", "low")'),
        ])
        res = export_workbook(
            wb, policy(publish=["Sheet1!A1"], keep=["Sheet1!B1"])
        )
        self.assertEqual(res.cells["Sheet1!B1"].formula, '=IF(A1>5, "high", "low")')
        self.assertEqual(res.cells["Sheet1!B1"].value, "high")

    def test_if_both_branches_count_as_dependencies(self):
        # 即使运行时只走一个分支，静态上两个分支的引用都算依赖
        wb = make_wb([
            C("Sheet1", "A1", 1),
            C("Sheet1", "X1", 100, level="internal"),
            C("Sheet1", "B1", formula="=IF(A1=1, A1, X1)"),
        ])
        res = export_workbook(
            wb, policy(publish=["Sheet1!A1"], keep=["Sheet1!B1"])
        )
        self.assertNotIn("Sheet1!B1", res.cells)

    def test_nested_if_evaluation(self):
        wb = make_wb([
            C("Sheet1", "A1", 15),
            C("Sheet1", "B1",
              formula='=IF(A1>=20, "big", IF(A1>=10, "mid", "small"))'),
        ])
        res = export_workbook(wb, policy(publish=["Sheet1!B1"]))
        self.assertEqual(res.cells["Sheet1!B1"].value, "mid")


class ClassificationAndHiddenTests(unittest.TestCase):
    def test_internal_level_never_exported_even_if_listed(self):
        wb = make_wb([C("Sheet1", "A1", 5, level="internal")])
        res = export_workbook(wb, policy(publish=["Sheet1!A1"]))
        self.assertNotIn("Sheet1!A1", res.cells)
        self.assertEqual(res.omitted["Sheet1!A1"], "level-not-permitted:internal")

    def test_custom_allowed_levels(self):
        wb = make_wb([C("Sheet1", "A1", 5, level="restricted")])
        res = export_workbook(
            wb, policy(publish=["Sheet1!A1"], levels=("public", "restricted"))
        )
        self.assertEqual(res.cells["Sheet1!A1"].value, 5)

    def test_hidden_sheet_cell_never_exported(self):
        wb = make_wb(
            [C("Secret", "A1", 12345)],
            hidden=("Secret",),
        )
        res = export_workbook(wb, policy(publish=["Secret!A1"]))
        self.assertNotIn("Secret!A1", res.cells)
        self.assertEqual(res.omitted["Secret!A1"], "hidden-sheet")

    def test_formula_cannot_be_kept_when_ref_on_hidden_sheet(self):
        wb = make_wb(
            [
                C("Secret", "A1", 5),
                C("Sheet1", "B1", formula="=Secret!A1*2"),
            ],
            hidden=("Secret",),
        )
        res = export_workbook(wb, policy(keep=["Sheet1!B1"]))
        self.assertNotIn("Sheet1!B1", res.cells)


class NamedRangeTests(unittest.TestCase):
    def test_used_named_range_rebuilt_unused_dropped(self):
        wb = make_wb(
            [
                C("Sheet1", "A1", 10),
                C("Sheet1", "Z9", 8888, level="internal"),
                C("Sheet1", "B1", formula="=A1*Rate"),
            ],
            names={"Rate": "Sheet1!A1", "InternalQuote": "Sheet1!Z9"},
        )
        res = export_workbook(
            wb, policy(publish=["Sheet1!A1"], keep=["Sheet1!B1"])
        )
        self.assertEqual(res.named_ranges, {"Rate": "Sheet1!A1"})
        self.assertEqual(res.cells["Sheet1!B1"].value, 100)
        # 未使用的命名范围及其内部数值不出现在导出物中
        self.assertNotIn("InternalQuote", res.named_ranges)
        self.assertNotIn("Sheet1!Z9", res.cells)

    def test_unused_named_range_not_exported_even_if_target_public(self):
        wb = make_wb(
            [C("Sheet1", "A1", 1)],
            names={"Unused": "Sheet1!A1"},
        )
        res = export_workbook(wb, policy(publish=["Sheet1!A1"]))
        self.assertEqual(res.named_ranges, {})

    def test_named_range_to_hidden_sheet_excluded(self):
        wb = make_wb(
            [
                C("Secret", "A1", 7),
                C("Sheet1", "B1", formula="=Hidden1*2"),
            ],
            names={"Hidden1": "Secret!A1"},
            hidden=("Secret",),
        )
        res = export_workbook(wb, policy(keep=["Sheet1!B1"]))
        self.assertEqual(res.named_ranges, {})
        self.assertNotIn("Sheet1!B1", res.cells)

    def test_named_range_to_internal_cell_blocks_keep(self):
        wb = make_wb(
            [
                C("Sheet1", "A1", 7, level="internal"),
                C("Sheet1", "B1", formula="=Rate*2"),
            ],
            names={"Rate": "Sheet1!A1"},
        )
        res = export_workbook(wb, policy(keep=["Sheet1!B1"]))
        self.assertEqual(res.named_ranges, {})
        self.assertNotIn("Sheet1!B1", res.cells)

    def test_range_named_range_expands_all_cells_as_dependencies(self):
        wb = make_wb(
            [
                C("Sheet1", "A1", 1),
                C("Sheet1", "A2", 2, level="internal"),
                C("Sheet1", "B1", formula="=IF(Pair>0, 1, 0)"),
            ],
            names={"Pair": "Sheet1!A1:A2"},
        )
        res = export_workbook(
            wb, policy(publish=["Sheet1!A1"], keep=["Sheet1!B1"])
        )
        # Pair 的 A2 未获准 -> 不能保留公式，名称也不导出
        self.assertNotIn("Sheet1!B1", res.cells)
        self.assertEqual(res.named_ranges, {})

    def test_undefined_name_blocks_keep(self):
        wb = make_wb([
            C("Sheet1", "A1", 2),
            C("Sheet1", "B1", formula="=A1*Unknown"),
        ])
        res = export_workbook(
            wb, policy(publish=["Sheet1!A1"], keep=["Sheet1!B1"])
        )
        self.assertNotIn("Sheet1!B1", res.cells)


class CycleTests(unittest.TestCase):
    def test_cycle_blocks_dependents_others_still_generated(self):
        wb = make_wb([
            C("Sheet1", "A1", formula="=B1+1"),
            C("Sheet1", "B1", formula="=A1+1"),
            C("Sheet1", "C1", formula="=A1*2"),   # 依赖环
            C("Sheet1", "D1", formula="=40+2"),   # 与环无关
        ])
        res = export_workbook(
            wb, policy(publish=["Sheet1!C1", "Sheet1!D1"])
        )
        self.assertEqual(res.cells["Sheet1!D1"].value, 42)
        self.assertNotIn("Sheet1!C1", res.cells)
        self.assertIn(["Sheet1!A1", "Sheet1!B1"], res.cycles)
        self.assertEqual(
            res.cycle_blocked["Sheet1!C1"], [["Sheet1!A1", "Sheet1!B1"]]
        )

    def test_self_loop_detected(self):
        wb = make_wb([C("Sheet1", "A1", formula="=A1+1")])
        res = export_workbook(wb, policy(publish=["Sheet1!A1"]))
        self.assertIn(["Sheet1!A1"], res.cycles)
        self.assertIn("Sheet1!A1", res.cycle_blocked)
        self.assertNotIn("Sheet1!A1", res.cells)

    def test_transitive_cycle_dependency(self):
        wb = make_wb([
            C("Sheet1", "A1", formula="=B1+1"),
            C("Sheet1", "B1", formula="=A1+1"),
            C("Sheet1", "C1", formula="=A1+1"),   # 不在策略中
            C("Sheet1", "D1", formula="=C1+1"),   # 传递依赖环
        ])
        res = export_workbook(wb, policy(publish=["Sheet1!D1"]))
        self.assertIn("Sheet1!D1", res.cycle_blocked)
        self.assertNotIn("Sheet1!D1", res.cells)

    def test_cycle_member_itself_listed_when_requested(self):
        wb = make_wb([
            C("Sheet1", "A1", formula="=B1+1"),
            C("Sheet1", "B1", formula="=A1+1"),
        ])
        res = export_workbook(wb, policy(publish=["Sheet1!A1"]))
        self.assertIn("Sheet1!A1", res.cycle_blocked)
        self.assertEqual(res.cells, {})

    def test_cycle_via_named_range(self):
        wb = make_wb(
            [
                C("Sheet1", "A1", formula="=Loop+1"),
                C("Sheet1", "B1", formula="=A1+1"),
            ],
            names={"Loop": "Sheet1!B1"},
        )
        res = export_workbook(wb, policy(publish=["Sheet1!A1"]))
        self.assertIn("Sheet1!A1", res.cycle_blocked)


class EvaluationTests(unittest.TestCase):
    def _eval(self, formula, extra_cells=(), names=None):
        wb = make_wb(
            [C("Sheet1", "A1", formula=formula), *extra_cells], names=names
        )
        res = export_workbook(wb, policy(publish=["Sheet1!A1"]))
        return res

    def test_arithmetic_precedence(self):
        self.assertEqual(self._eval("=2+3*4").cells["Sheet1!A1"].value, 14)
        self.assertEqual(self._eval("=(2+3)*4").cells["Sheet1!A1"].value, 20)

    def test_unary_minus_and_plus(self):
        self.assertEqual(self._eval("=-3+10").cells["Sheet1!A1"].value, 7)
        self.assertEqual(self._eval("=--5").cells["Sheet1!A1"].value, 5)

    def test_division_and_floats(self):
        self.assertEqual(self._eval("=7/2").cells["Sheet1!A1"].value, 3.5)
        self.assertEqual(self._eval("=1.5*2").cells["Sheet1!A1"].value, 3.0)

    def test_division_by_zero_recorded(self):
        res = self._eval("=1/0")
        self.assertNotIn("Sheet1!A1", res.cells)
        self.assertIn("Sheet1!A1", res.errors)

    def test_string_comparison(self):
        res = self._eval('=IF(B1="x", 1, 0)', extra_cells=[C("Sheet1", "B1", "x")])
        self.assertEqual(res.cells["Sheet1!A1"].value, 1)

    def test_comparison_operators(self):
        for formula, expected in [
            ("=IF(3<>4, 1, 0)", 1),
            ("=IF(3<=3, 1, 0)", 1),
            ("=IF(3>=4, 1, 0)", 0),
            ("=IF(2<3, 1, 0)", 1),
        ]:
            with self.subTest(formula=formula):
                self.assertEqual(self._eval(formula).cells["Sheet1!A1"].value, expected)

    def test_type_mismatch_comparison_errors(self):
        res = self._eval('=IF(1="a", 1, 0)')
        self.assertIn("Sheet1!A1", res.errors)

    def test_missing_reference_errors(self):
        res = self._eval("=Q9+1")
        self.assertIn("Sheet1!A1", res.errors)

    def test_sheet_qualified_reference(self):
        res = self._eval("=Data!B2*3", extra_cells=[C("Data", "B2", 4)])
        self.assertEqual(res.cells["Sheet1!A1"].value, 12)

    def test_absolute_reference_form(self):
        res = self._eval("=$B$1+1", extra_cells=[C("Sheet1", "B1", 9)])
        self.assertEqual(res.cells["Sheet1!A1"].value, 10)


class ParserTests(unittest.TestCase):
    def test_parse_if_ast(self):
        ast = parse_formula("=IF(A1>0, 1, 2)", "Sheet1")
        self.assertEqual(ast[0], "if")

    def test_unsupported_function_rejected(self):
        with self.assertRaises(FormulaError):
            parse_formula("=SUM(A1:A2)", "Sheet1")

    def test_trailing_garbage_rejected(self):
        with self.assertRaises(FormulaError):
            parse_formula("=1 2", "Sheet1")

    def test_empty_formula_rejected(self):
        with self.assertRaises(FormulaError):
            parse_formula("=", "Sheet1")


class PolicyAndShapeTests(unittest.TestCase):
    def test_default_sheet_applies_to_bare_refs(self):
        wb = make_wb([C("Sheet1", "A1", 3)])
        res = export_workbook(wb, policy(publish=["A1"]), default_sheet="Sheet1")
        self.assertEqual(res.cells["Sheet1!A1"].value, 3)

    def test_bare_ref_without_default_sheet_raises(self):
        wb = make_wb([C("Sheet1", "A1", 3)])
        with self.assertRaises(ValueError):
            export_workbook(wb, policy(publish=["A1"]))

    def test_empty_policy_exports_nothing(self):
        wb = make_wb([C("Sheet1", "A1", 3)], names={"N": "Sheet1!A1"})
        res = export_workbook(wb, policy())
        self.assertEqual(res.cells, {})
        self.assertEqual(res.named_ranges, {})
        self.assertEqual(res.cycles, [])

    def test_missing_policy_cell_recorded(self):
        wb = make_wb([C("Sheet1", "A1", 3)])
        res = export_workbook(wb, policy(publish=["Sheet1!B2"]))
        self.assertEqual(res.omitted["Sheet1!B2"], "missing-cell")

    def test_duplicate_cell_rejected(self):
        wb = make_wb([C("Sheet1", "A1", 1), C("Sheet1", "A1", 2)])
        with self.assertRaises(ValueError):
            export_workbook(wb, policy())

    def test_quoted_sheet_name(self):
        wb = make_wb([
            C("My Sheet", "A1", 2),
            C("Sheet1", "B1", formula="='My Sheet'!A1*10"),
        ])
        res = export_workbook(
            wb,
            policy(publish=["'My Sheet'!A1"], keep=["Sheet1!B1"]),
            default_sheet="Sheet1",
        )
        self.assertEqual(res.cells["Sheet1!B1"].value, 20)
        self.assertEqual(res.cells["Sheet1!B1"].formula, "='My Sheet'!A1*10")


if __name__ == "__main__":
    unittest.main()
