"""区间引擎单元测试：传播、可行性、极值与比例极值。"""

from __future__ import annotations

import unittest
from fractions import Fraction

from chain_audit.intervals import (
    Equation,
    extremize,
    initial_domains,
    propagate,
    ratio_extremize,
    solve,
)


def eq(coeffs, rhs, origin=None):
    return Equation(tuple(coeffs), rhs, origin)


class PropagationTests(unittest.TestCase):
    def test_sum_equation_tightens_domains(self):
        # x + y = 10, x in [0,10], y in [0,10]
        dom = initial_domains(["x", "y"])
        out = propagate(dom, [eq([("x", 1), ("y", 1)], 10)])
        self.assertIsNone(out.failed)
        self.assertEqual(out.domains["x"], (0, 10))
        self.assertEqual(out.domains["y"], (0, 10))

        # 加入 x >= 7 后，y 被收窄到 [0,3]
        dom2 = {"x": (7, 10), "y": (0, 10)}
        out2 = propagate(dom2, [eq([("x", 1), ("y", 1)], 10)])
        self.assertEqual(out2.domains["y"], (0, 3))
        self.assertEqual(out2.domains["x"], (7, 10))

    def test_negative_coefficient_equation(self):
        # x - y = 2, x in [0,10], y in [0,10]  => x in [2,10], y in [0,8]
        dom = {"x": (0, 10), "y": (0, 10)}
        out = propagate(dom, [eq([("x", 1), ("y", -1)], 2)])
        self.assertEqual(out.domains["x"], (2, 10))
        self.assertEqual(out.domains["y"], (0, 8))

    def test_inconsistent_bounds_detected(self):
        dom = {"x": (5, 5), "y": (0, 3)}
        out = propagate(dom, [eq([("x", 1), ("y", 1)], 10)])
        self.assertIsNotNone(out.failed)

    def test_multistep_conservation(self):
        # a = b + c; b = d + e；a 固定 10，d 固定 4，e 固定 1 => b=5, c=5
        dom = initial_domains(["a", "b", "c", "d", "e"])
        dom["a"] = (10, 10)
        dom["d"] = (4, 4)
        dom["e"] = (1, 1)
        eqs = [
            eq([("a", -1), ("b", 1), ("c", 1)], 0),
            eq([("b", -1), ("d", 1), ("e", 1)], 0),
        ]
        out = propagate(dom, eqs)
        self.assertEqual(out.domains["b"], (5, 5))
        self.assertEqual(out.domains["c"], (5, 5))

    def test_input_domains_not_mutated(self):
        dom = initial_domains(["x", "y"])
        before = dict(dom)
        propagate(dom, [eq([("x", 1), ("y", 1)], 3)])
        self.assertEqual(dom, before)


class SolveTests(unittest.TestCase):
    def test_sat_returns_integer_witness(self):
        dom = {"x": (0, 6), "y": (0, 6)}
        status, witness = solve(dom, [eq([("x", 1), ("y", 1)], 7)])
        self.assertEqual(status, "sat")
        self.assertEqual(witness["x"] + witness["y"], 7)

    def test_unsat(self):
        dom = {"x": (0, 2), "y": (0, 2)}
        status, witness = solve(dom, [eq([("x", 1), ("y", 1)], 9)])
        self.assertEqual(status, "unsat")
        self.assertIsNone(witness)

    def test_sat_after_propagation_only(self):
        dom = {"x": (3, 3), "y": (4, 4)}
        status, witness = solve(dom, [eq([("x", 1), ("y", 1)], 7)])
        self.assertEqual(status, "sat")
        self.assertEqual(witness, {"x": 3, "y": 4})


class ExtremizeTests(unittest.TestCase):
    def setUp(self):
        # x + y = 10，x,y 非负
        self.dom = {"x": (0, 10), "y": (0, 10)}
        self.eqs = [eq([("x", 1), ("y", 1)], 10)]

    def test_min_max(self):
        mn, ok1 = extremize(self.dom, self.eqs, "x", maximize=False)
        mx, ok2 = extremize(self.dom, self.eqs, "x", maximize=True)
        self.assertTrue(ok1 and ok2)
        self.assertEqual((mn, mx), (0, 10))

    def test_min_max_with_constraint(self):
        dom = {"x": (3, 8), "y": (0, 10)}
        mn, _ = extremize(dom, self.eqs, "x", maximize=False)
        mx, _ = extremize(dom, self.eqs, "x", maximize=True)
        self.assertEqual((mn, mx), (3, 8))

    def test_ratio_extremes(self):
        # x + y = 10，x in [2,8] => x/(x+y)=x/10 in [1/5, 4/5]
        dom = {"x": (2, 8), "y": (0, 10)}
        lo, ok_lo = ratio_extremize(dom, self.eqs, "x", "y", maximize=False)
        # x/y 比例：x 最小 2 时 y=8 => 1/4；x 最大 8 时 y=2 => 4
        hi, ok_hi = ratio_extremize(dom, self.eqs, "x", "y", maximize=True)
        self.assertTrue(ok_lo and ok_hi)
        self.assertEqual(lo, Fraction(1, 4))
        self.assertEqual(hi, Fraction(4, 1))

    def test_ratio_zero_denominator_skipped(self):
        # x + y = 5，x 固定 5、y 固定 0：以 y 为分母无正分母赋值
        dom = {"x": (5, 5), "y": (0, 0)}
        value, exact = ratio_extremize(
            dom, self.eqs, "x", "y", maximize=True
        )
        self.assertIsNone(value)
        self.assertTrue(exact)


if __name__ == "__main__":
    unittest.main()
