"""Structural input validation tests."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from emergency_scheduler import (  # noqa: E402
    Component,
    Problem,
    Service,
    Stage,
    ValidationError,
    solve,
)

from tests import fixtures as fx  # noqa: E402


class ValidationTest(unittest.TestCase):
    def _base(self) -> Problem:
        return fx.sequencing_problem()

    def test_rejects_non_positive_horizon(self) -> None:
        problem = self._base()
        problem.horizon = 0
        with self.assertRaises(ValidationError):
            solve(problem)

    def test_rejects_duplicate_stage_ids(self) -> None:
        problem = self._base()
        problem.components[1].stages[0] = Stage(
            "a1", "B", duration=1, personnel=1
        )
        with self.assertRaises(ValidationError):
            solve(problem)

    def test_rejects_stage_stopping_unknown_component(self) -> None:
        problem = self._base()
        problem.components[0].stages[0] = Stage(
            "a1", "A", duration=1, personnel=1, stop_components=("Z",)
        )
        with self.assertRaises(ValidationError):
            solve(problem)

    def test_rejects_stage_stopping_its_own_component(self) -> None:
        problem = self._base()
        problem.components[0].stages[0] = Stage(
            "a1", "A", duration=1, personnel=1, stop_components=("A",)
        )
        with self.assertRaises(ValidationError):
            solve(problem)

    def test_rejects_non_positive_duration(self) -> None:
        problem = self._base()
        problem.components[0].stages[0] = Stage(
            "a1", "A", duration=0, personnel=1
        )
        with self.assertRaises(ValidationError):
            solve(problem)

    def test_rejects_negative_service_minimum(self) -> None:
        problem = self._base()
        problem.services[0] = Service("s1", demand=5.0, min_capacity=-1.0)
        with self.assertRaises(ValidationError):
            solve(problem)

    def test_rejects_capacity_for_unknown_service(self) -> None:
        problem = self._base()
        problem.components[0].service_capacity["ghost"] = 3.0
        with self.assertRaises(ValidationError):
            solve(problem)

    def test_rejects_instance_without_any_repair_stage(self) -> None:
        problem = Problem(
            components=[Component("X", {"s1": 1.0}, stages=[])],
            services=[Service("s1", demand=1.0)],
            horizon=3,
            personnel_capacity=1,
        )
        with self.assertRaises(ValidationError):
            solve(problem)

    def test_accepts_healthy_redundant_component_without_stages(self) -> None:
        problem = fx.stop_redundant_problem()
        result = solve(problem)
        self.assertTrue(result.feasible)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
