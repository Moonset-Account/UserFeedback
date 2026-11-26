from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from incremental_build import BuildCoordinator, DependencyCycleError

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


class DynamicCycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.root = Path(self._temp.name)
        self.coordinator = BuildCoordinator(root=self.root)
        self.coordinator.write_file("old.h", "old dependency")

        def compile_v1(ctx):
            ctx.read("old.h")
            ctx.write_output("app.o", "original-app-object")

        def generate(ctx):
            ctx.read("app.o")  # static edge declared below as well
            ctx.write_output("gen.h", "generated-header-v1")

        self.coordinator.add_step("compile", compile_v1, outputs=["app.o"])
        self.coordinator.add_step(
            "generate",
            generate,
            outputs=["gen.h"],
            static_dependencies=["app.o"],
        )
        initial = self.coordinator.run()
        initial.raise_for_status()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_new_dynamic_edge_reports_closed_loop_and_retains_artifacts(self) -> None:
        def compile_with_new_generated_read(ctx):
            ctx.read("gen.h")
            ctx.write_output("app.o", "stale-or-cyclic-new-object")

        self.coordinator.specs["compile"].action = compile_with_new_generated_read

        with self.assertRaises(DependencyCycleError) as caught:
            self.coordinator.run("compile", force=True)

        error = caught.exception
        self.assertEqual(error.cycle, ["compile", "generate", "compile"])
        self.assertEqual(len(error.edges), 2)

        new_edge = next(
            edge
            for edge in error.edges
            if edge.source == "compile" and edge.target == "generate"
        )
        self.assertEqual(new_edge.path, "gen.h")
        self.assertEqual(new_edge.kind, "dynamic")
        self.assertTrue(new_edge.newly_added)

        return_edge = next(
            edge
            for edge in error.edges
            if edge.source == "generate" and edge.target == "compile"
        )
        self.assertEqual(return_edge.path, "app.o")
        self.assertEqual(return_edge.kind, "static")

        # Original artifacts and old dynamic manifest remain on disk/state.
        self.assertEqual(
            (self.root / "app.o").read_text(), "original-app-object"
        )
        self.assertEqual(
            (self.root / "gen.h").read_text(), "generated-header-v1"
        )
        self.assertEqual(
            set(self.coordinator.dynamic_dependencies("compile")), {"old.h"}
        )

        non_raising = self.coordinator.run(
            "compile", force=True, raise_on_cycle=False
        )
        self.assertFalse(non_raising.ok)
        self.assertIsInstance(non_raising.cycle_error, DependencyCycleError)
        self.assertEqual(non_raising.failed_steps, ["compile"])

    def test_self_dependency_is_a_one_edge_cycle(self) -> None:
        coordinator = BuildCoordinator()
        coordinator.write_file("self.out", "old")

        def reads_own_output(ctx):
            ctx.read("self.out")
            ctx.write_output("self.out", "new")

        coordinator.add_step("self_step", reads_own_output, outputs=["self.out"])
        with self.assertRaises(DependencyCycleError) as caught:
            coordinator.run("self_step", force=True)
        self.assertEqual(caught.exception.cycle, ["self_step", "self_step"])
        self.assertEqual(coordinator.read_bytes("self.out")[0], b"old")


if __name__ == "__main__":
    unittest.main()
