from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from incremental_build import BuildCoordinator

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


class DynamicManifestFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.root = Path(self._temp.name)
        self.coordinator = BuildCoordinator(root=self.root)
        self.coordinator.write_file("header.h", "# one\n")
        self.coordinator.write_file("other.h", "other\n")
        self.coordinator.write_file("new-header.h", "maybe\n")

        def successful_compile(ctx):
            ctx.read("header.h")
            ctx.read("other.h")
            ctx.write_output("compile.o", "old-object")

        self.successful_compile = successful_compile
        self.coordinator.add_step(
            "compile", successful_compile, outputs=["compile.o"]
        )
        ok = self.coordinator.run()
        ok.raise_for_status()
        self.old_manifest = self.coordinator.dynamic_dependencies("compile")
        self.assertEqual(set(self.old_manifest), {"header.h", "other.h"})

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_failed_run_keeps_old_dynamic_edges_and_artifact(self) -> None:
        def failing_compile(ctx):
            ctx.read("new-header.h")  # a tentative new edge that must not commit
            raise RuntimeError("synthetic compiler failed")

        self.coordinator.specs["compile"].action = failing_compile
        result = self.coordinator.run(force=True)

        self.assertFalse(result.ok)
        self.assertEqual(result.failed_steps, ["compile"])
        failed_run = result.step("compile")
        self.assertEqual(failed_run.attempts[-1].error_type, "RuntimeError")

        after = self.coordinator.dynamic_dependencies("compile")
        self.assertEqual(after, self.old_manifest)
        self.assertNotIn("new-header.h", after)
        self.assertEqual(
            (self.root / "compile.o").read_text(), "old-object"
        )

        state = json.loads(
            (self.root / ".incremental-build" / "state.json").read_text()
        )
        self.assertEqual(
            set(state["steps"]["compile"]["dynamic_inputs"]),
            {"header.h", "other.h"},
        )
        self.assertEqual(state["steps"]["compile"]["last_status"], "failed")

    def test_successful_valid_run_after_failure_can_replace_manifest(self) -> None:
        def replacement_compile(ctx):
            # Deliberately no longer reads either old header.
            ctx.write_output("compile.o", "new-object")

        self.coordinator.specs["compile"].action = replacement_compile
        result = self.coordinator.run(force=True)
        result.raise_for_status()

        self.assertEqual(self.coordinator.dynamic_dependencies("compile"), {})
        self.assertEqual((self.root / "compile.o").read_text(), "new-object")

    def test_restart_after_failure_still_has_old_edges_and_retries(self) -> None:
        def failing_compile(ctx):
            raise RuntimeError("still broken")

        self.coordinator.specs["compile"].action = failing_compile
        failed = self.coordinator.run(force=True)
        self.assertFalse(failed.ok)

        restarted = BuildCoordinator(root=self.root)

        def fixed_compile(ctx):
            ctx.write_output("compile.o", "restarted-object")

        restarted.add_step("compile", fixed_compile, outputs=["compile.o"])
        result = restarted.run()
        result.raise_for_status()
        self.assertEqual(result.ran_steps, ["compile"])
        self.assertEqual(restarted.dynamic_dependencies("compile"), {})
        self.assertEqual((self.root / "compile.o").read_text(), "restarted-object")


if __name__ == "__main__":
    unittest.main()
