from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from incremental_build.demo import run_header_demo

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


class HeaderPruningDemoTest(unittest.TestCase):
    def test_generator_rewrite_and_removed_include_change_future_reruns(self) -> None:
        with tempfile.TemporaryDirectory(dir=TMP_ROOT) as temporary:
            report = run_header_demo(temporary)
            root = Path(temporary)

            self.assertTrue(report.first.ok)
            self.assertEqual(report.first.ran_steps,
                             ["generate_header", "compile"])
            self.assertIn(
                "build/config.h",
                report.first.step("compile").dynamic_dependencies,
            )

            self.assertTrue(report.second.ok)
            self.assertEqual(report.second.ran_steps,
                             ["generate_header", "compile"])
            compile_second = report.second.step("compile")
            self.assertFalse(compile_second.retried)
            self.assertEqual(compile_second.dynamic_dependencies, {})
            self.assertIn(
                "src/main.c",
                report.coordinator.static_input_versions("compile"),
            )
            self.assertEqual(
                report.coordinator.dependency_graph()["compile"],
                [],
                "pruned compile -> generate edge must disappear",
            )

            self.assertTrue(report.third.ok)
            self.assertEqual(report.third.ran_steps, ["generate_header"])
            self.assertEqual(report.third.cached_steps, ["compile"])

            header = (root / "build/config.h").read_text()
            obj = (root / "build/main.o").read_text()
            self.assertIn("FEATURE 3", header)
            self.assertIn("reads=<none>", obj)


if __name__ == "__main__":
    unittest.main()
