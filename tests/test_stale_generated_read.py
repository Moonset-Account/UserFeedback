from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from incremental_build import BuildCoordinator, PauseGate

REPO_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = REPO_ROOT / ".test-tmp"
TMP_ROOT.mkdir(exist_ok=True)


class StaleGeneratedReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.root = Path(self._temp.name)
        self.coordinator = BuildCoordinator(root=self.root)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_paused_read_then_producer_rewrite_discards_stale_result_and_reruns(self):
        coordinator = self.coordinator
        coordinator.write_file("generated/config.h", "header-v1")
        coordinator.write_file("build/compile.o", "previously-published-object")

        def generate(ctx):
            ctx.write_output("generated/config.h", "header-v2-from-producer")

        def compile_step(ctx):
            header = ctx.read_text("generated/config.h")
            ctx.pause("after-header-read")
            ctx.write_output("build/compile.o", f"object-for:{header}")

        coordinator.add_step(
            "generate", generate, outputs=["generated/config.h"]
        )
        coordinator.add_step("compile", compile_step, outputs=["build/compile.o"])

        events = []
        original_commit = coordinator.commit_success
        commits = []

        def commit_spy(step_name, *args, **kwargs):
            commits.append(step_name)
            return original_commit(step_name, *args, **kwargs)

        coordinator.commit_success = commit_spy  # type: ignore[method-assign]

        def pause_hook(event):
            events.append((event.step, event.attempt, event.reason))
            if (
                event.step == "compile"
                and event.attempt == 1
                and event.reason == "after-header-read"
            ):
                run = event.run_step("generate")
                self.assertEqual(run.status, "ran")

        result = coordinator.run("compile", pause_hook=pause_hook)
        result.raise_for_status()

        self.assertEqual(events, [("compile", 1, "after-header-read"),
                                   ("compile", 2, "after-header-read")])
        compile_run = result.step("compile")
        generate_run = result.step("generate")

        self.assertEqual(generate_run.attempt_count, 1)
        self.assertEqual(compile_run.attempt_count, 2)
        self.assertEqual(
            [attempt.status for attempt in compile_run.attempts],
            ["stale", "published"],
        )
        stale_attempt = compile_run.attempts[0]
        self.assertEqual([r.path for r in stale_attempt.stale_reads],
                         ["generated/config.h"])
        self.assertEqual(stale_attempt.stale_reads[0].version.revision, 1)
        self.assertEqual(
            [name for name in commits if name == "compile"],
            ["compile"],
            "stale first compile attempt was published",
        )

        manifest = coordinator.dynamic_dependencies("compile")
        self.assertEqual(set(manifest), {"generated/config.h"})
        self.assertEqual(manifest["generated/config.h"].revision, 2)
        self.assertEqual(
            (self.root / "build/compile.o").read_text(),
            "object-for:header-v2-from-producer",
        )

    def test_producer_scheduled_after_optimistic_reader_is_still_verified_before_publish(self):
        coordinator = BuildCoordinator()  # in-memory also exercises that backend
        coordinator.write_file("generated.h", "old")

        calls = {"compile": 0, "generate": 0}

        def compile_step(ctx):
            calls["compile"] += 1
            value = ctx.read_text("generated.h")
            ctx.write_output("compile.out", f"compiled-{value}")

        def generate_step(ctx):
            calls["generate"] += 1
            ctx.write_output("generated.h", "new")

        # Registration order intentionally puts the optimistic reader first.
        coordinator.add_step("compile", compile_step, outputs=["compile.out"])
        coordinator.add_step("generate", generate_step, outputs=["generated.h"])

        result = coordinator.run()
        result.raise_for_status()

        self.assertEqual(calls, {"compile": 2, "generate": 1})
        self.assertEqual(result.step("compile").attempt_count, 2)
        self.assertEqual(
            [a.status for a in result.step("compile").attempts],
            ["stale", "published"],
        )
        self.assertEqual(coordinator.read_bytes("compile.out")[0], b"compiled-new")

    def test_threaded_pause_gate_supports_actual_callback_suspension(self) -> None:
        coordinator = self.coordinator
        coordinator.write_file("thread-header.h", "old-thread")
        coordinator.add_step(
            "thread-generate",
            lambda ctx: ctx.write_output("thread-header.h", "new-thread"),
            outputs=["thread-header.h"],
        )

        def compile_action(ctx):
            value = ctx.read_text("thread-header.h")
            ctx.pause("after-read")
            ctx.write_output("thread.o", f"object-{value}")

        coordinator.add_step(
            "thread-compile", compile_action, outputs=["thread.o"]
        )

        gate = PauseGate(step="thread-compile", reason="after-read", timeout=5)
        outcome: dict[str, object] = {}
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                outcome["result"] = coordinator.run(
                    "thread-compile", pause_hook=gate
                )
            except BaseException as exc:  # captured for assertion in main thread
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            paused_event = gate.wait_until_paused(timeout=5)
            producer_run = paused_event.run_step("thread-generate")
            self.assertTrue(producer_run.ok)
        finally:
            gate.resume()
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive(), "build worker did not finish")
        self.assertEqual(errors, [])
        result = outcome["result"]
        result.raise_for_status()  # type: ignore[union-attr]
        compile_run = result.step("thread-compile")  # type: ignore[union-attr]
        self.assertEqual(
            [attempt.status for attempt in compile_run.attempts],
            ["stale", "published"],
        )
        self.assertEqual(
            (self.root / "thread.o").read_text(), "object-new-thread"
        )

    def test_repeated_stale_reads_until_retry_limit_preserve_old_manifest(self) -> None:
        coordinator = BuildCoordinator()
        coordinator.write_file("volatile.h", "v0")

        def action(ctx):
            value = ctx.read_text("volatile.h")
            ctx.pause("after-read")
            ctx.write_output("out.o", f"object-{value}")

        coordinator.add_step("volatile-step", action, outputs=["out.o"])
        initial = coordinator.run()
        initial.raise_for_status()
        old_manifest = coordinator.dynamic_dependencies("volatile-step")
        old_output = coordinator.read_bytes("out.o")[0]

        write_count = 0

        def keep_changing(event):
            nonlocal write_count
            if event.step == "volatile-step" and event.reason == "after-read":
                write_count += 1
                event.write_file("volatile.h", f"changed-{write_count}")

        result = coordinator.run(
            "volatile-step",
            force=True,
            max_attempts=2,
            pause_hook=keep_changing,
        )

        self.assertFalse(result.ok)
        run = result.step("volatile-step")
        self.assertEqual(run.status, "failed")
        self.assertEqual(
            [attempt.status for attempt in run.attempts],
            ["stale", "stale"],
        )
        self.assertEqual(
            coordinator.dynamic_dependencies("volatile-step"), old_manifest
        )
        self.assertEqual(coordinator.read_bytes("out.o")[0], old_output)


if __name__ == "__main__":
    unittest.main()
