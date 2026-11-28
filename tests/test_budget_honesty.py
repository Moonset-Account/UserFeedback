"""引擎预算耗尽时的诚实性测试：无定论必须返回 unknown/非精确，禁止伪造。"""

from __future__ import annotations

import unittest

from chain_audit.intervals import (
    Equation,
    extremize,
    propagate,
    ratio_extremize,
    solve,
)


def eq(coeffs, rhs):
    return Equation(tuple(coeffs), rhs)


class BudgetHonestyTests(unittest.TestCase):
    def _build_wide(self):
        # 多个宽度较大的变量满足 x1+...+x6 = 300，制造搜索压力。
        names = [f"x{i}" for i in range(6)]
        dom = {v: (0, 100) for v in names}
        equations = [eq([(v, 1) for v in names], 300)]
        return dom, equations

    def test_solve_unknown_under_tiny_budget(self):
        dom, equations = self._build_wide()
        status, witness = solve(dom, equations, node_budget=1)
        # 预算只够一次传播、无法完成搜索时，必须如实报 unknown，
        # 而不是把未证实的状态当成 sat/unsat。
        self.assertEqual(status, "unknown")
        self.assertIsNone(witness)

    def test_extremize_marks_inexact_when_budget_exhausted(self):
        dom, equations = self._build_wide()
        value, completed = extremize(
            dom, equations, "x0", maximize=True, node_budget=1
        )
        self.assertFalse(completed)

    def test_ratio_extremize_marks_inexact_when_budget_exhausted(self):
        dom, equations = self._build_wide()
        value, completed = ratio_extremize(
            dom, equations, "x0", "x1", maximize=True, node_budget=1
        )
        self.assertFalse(completed)

    def test_full_budget_still_exact(self):
        dom, equations = self._build_wide()
        status, witness = solve(dom, equations)
        self.assertEqual(status, "sat")
        self.assertEqual(sum(witness.values()), 300)
        value, completed = extremize(dom, equations, "x0", maximize=True)
        self.assertTrue(completed)
        self.assertEqual(value, 100)

    def test_propagation_detects_hard_contradiction_without_search(self):
        # 纯传播即可判定的矛盾，不依赖搜索预算。
        dom = {"x": (10, 10), "y": (0, 5)}
        result = propagate(dom, [eq([("x", 1), ("y", 1)], 20)])
        self.assertIsNotNone(result.failed)


if __name__ == "__main__":
    unittest.main()
