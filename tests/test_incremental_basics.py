from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from incremental_build import BuildCoordinator, DuplicateProducerError

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


class LocalIncrementalBasicsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.root = Path(self._temp.name)
        self.coordinator = BuildCoordinator(root=self.root)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_first_build_runs_and_second_unchanged_build_is_cached(self) -> None:
        self.coordinator.write_file("src/input.txt", "version-1")
        calls = []

        def action(ctx):
            calls.append(ctx.attempt)
            data = ctx.read_text("src/input.txt")
            ctx.write_output("out/result.txt", data.upper())

        self.coordinator.add_step(
            "transform",
            action,
            outputs=["out/result.txt"],
            static_dependencies=["src/input.txt"],
        )

        first = self.coordinator.run()
        self.assertTrue(first.ok)
        self.assertEqual(first.ran_steps, ["transform"])
        self.assertEqual((self.root / "out/result.txt").read_text(), "VERSION-1")

        second = self.coordinator.run()
        self.assertTrue(second.ok)
        self.assertEqual(second.cached_steps, ["transform"])
        self.assertEqual(second.ran_steps, [])
        self.assertEqual(len(calls), 1)

        self.coordinator.write_file("src/input.txt", "version-2")
        third = self.coordinator.run()
        self.assertTrue(third.ok)
        self.assertEqual(third.ran_steps, ["transform"])
        self.assertEqual((self.root / "out/result.txt").read_text(), "VERSION-2")
        self.assertEqual(len(calls), 2)

    def test_state_and_manifest_persist_across_coordinator_restart(self) -> None:
        self.coordinator.write_file("input.txt", "one")

        def first_action(ctx):
            ctx.read("input.txt")
            ctx.write_output("output.txt", "one-out")

        self.coordinator.add_step(
            "step", first_action, outputs=["output.txt"], static_deps=["input.txt"]
        )
        result = self.coordinator.run()
        self.assertTrue(result.ok)
        self.assertTrue((self.root / ".incremental-build" / "state.json").exists())

        restarted = BuildCoordinator(root=self.root)

        def same_action(ctx):
            ctx.read("input.txt")
            ctx.write_output("output.txt", "one-out")

        restarted.add_step(
            "step", same_action, outputs=["output.txt"], static_deps=["input.txt"]
        )
        cached = restarted.run()
        self.assertEqual(cached.cached_steps, ["step"])
        self.assertIn("input.txt", restarted.static_input_versions("step"))
        self.assertIn("input.txt", restarted.input_versions("step"))

    def test_late_reported_non_generated_header_change_triggers_rebuild(self) -> None:
        self.coordinator.write_file("main.c", "main v1")
        self.coordinator.write_file("discovered.h", "header v1")
        calls = []

        def compile_action(ctx):
            calls.append(1)
            ctx.read("main.c")
            ctx.read("discovered.h")
            ctx.write_output("main.o", "object")

        self.coordinator.add_step(
            "compile-deps",
            compile_action,
            outputs=["main.o"],
            static_dependencies=["main.c"],
        )
        self.coordinator.run().raise_for_status()
        self.assertEqual(self.coordinator.run().cached_steps, ["compile-deps"])

        self.coordinator.write_file("discovered.h", "header v2")
        changed = self.coordinator.run()
        changed.raise_for_status()
        self.assertEqual(changed.ran_steps, ["compile-deps"])
        self.assertEqual(len(calls), 2)
        self.assertIn(
            "discovered.h",
            self.coordinator.dynamic_dependencies("compile-deps"),
        )

    def test_every_output_requires_one_unique_producer(self) -> None:
        self.coordinator.add_step(
            "a", lambda ctx: ctx.write_output("shared.out", "a"), outputs=["shared.out"]
        )
        with self.assertRaises(DuplicateProducerError):
            self.coordinator.add_step(
                "b",
                lambda ctx: ctx.write_output("shared.out", "b"),
                outputs=["shared.out"],
            )

    def test_static_generated_dependency_orders_unique_producer_first(self) -> None:
        order = []

        def generate(ctx):
            order.append("generate")
            ctx.write_output("generated.dat", "generated")

        def consume(ctx):
            order.append("consume")
            value = ctx.read_text("generated.dat")
            ctx.write_output("consumed.dat", value.upper())

        # Register consumer first to prove ordering comes from the static edge.
        self.coordinator.add_step(
            "consume",
            consume,
            outputs=["consumed.dat"],
            static_dependencies=["generated.dat"],
        )
        self.coordinator.add_step(
            "generate", generate, outputs=["generated.dat"]
        )

        result = self.coordinator.run("consume")
        result.raise_for_status()
        self.assertEqual(order, ["generate", "consume"])
        self.assertEqual(
            (self.root / "consumed.dat").read_text(), "GENERATED"
        )

    def test_unknown_target_is_reported(self) -> None:
        with self.assertRaises(Exception):
            self.coordinator.run("does-not-exist")


if __name__ == "__main__":
    unittest.main()
