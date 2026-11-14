"""Demonstration scenarios must actually pass, and stay stable across repeats."""

import unittest

from quota.demo import SCENARIOS, run_scenarios


class DemoScenarioTests(unittest.TestCase):
    def test_every_scenario_passes_and_declares_itself(self) -> None:
        report = run_scenarios()
        self.assertEqual(set(report), {case_id for case_id, _ in SCENARIOS})
        for case_id, body in report.items():
            self.assertEqual(body["case_id"], case_id)
            self.assertTrue(body["passed"], case_id)
            self.assertGreaterEqual(len(body["checks"]), 3, case_id)
            for check in body["checks"]:
                self.assertIn("expected", check)
                self.assertIn("actual", check)
                self.assertTrue(check["ok"], f"{case_id}: {check['name']}")

    def test_scenarios_are_deterministic_over_repeats(self) -> None:
        # The last-unit race scenario uses real threads, so repeat it: a data
        # race that only sometimes admits both callers must not hide here.
        for run in range(10):
            report = run_scenarios()
            failed = [case_id for case_id, body in report.items() if not body["passed"]]
            self.assertEqual(failed, [], f"run {run} failed: {failed}")

    def test_window_boundary_scenario_has_the_edge_checks(self) -> None:
        body = run_scenarios()["demo-window-boundary"]
        names = {check["name"] for check in body["checks"]}
        self.assertIn("t=9.5 denied (window not yet released)", names)
        self.assertIn("t=10.0 admitted (slot freed at the boundary)", names)

    def test_race_scenario_asserts_exactly_one_winner(self) -> None:
        body = run_scenarios()["demo-last-unit-race"]
        names = {check["name"]: check for check in body["checks"]}
        self.assertEqual(names["exactly one admission"]["actual"], 1)


if __name__ == "__main__":
    unittest.main()
