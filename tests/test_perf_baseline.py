import unittest

from tools.perf_baseline import _cases, _diff


class PerformanceBaselineHarnessTests(unittest.TestCase):
    def test_cases_are_bounded_and_cover_required_workloads(self):
        cases = _cases()
        self.assertEqual(
            set(cases),
            {
                "clean_182", "mixed_182", "worst_regex_182",
                "projected_1500_clean", "projected_1500_mixed", "diff_4mib_182",
            },
        )
        for _name, (_rules, lines) in cases.items():
            self.assertLessEqual(len(_diff(lines).encode("utf-8")), 4 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
