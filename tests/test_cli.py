import json
import os
import subprocess
import sys
import tempfile
import unittest

import _bootstrap  # noqa: F401  (workspace-root path bootstrap)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNNER = os.path.join(_ROOT, "scripts", "run_cli.py")
_SPEC = os.path.join(_ROOT, "examples", "compile_race.json")
_SNAP = os.path.join(_ROOT, "examples", "breakpoint.json")


class CliEndToEndTests(unittest.TestCase):
    def _run(self, *args):
        env = dict(os.environ)
        # Run the same way the README documents: isolated interpreter.
        proc = subprocess.run(
            [sys.executable, "-I", _RUNNER, *args],
            cwd=_ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
        return proc

    def test_analyze_emits_machine_readable_report(self):
        proc = self._run("analyze", _SPEC)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertFalse(report["deterministic"])
        self.assertEqual(len(report["traces"]), 2)
        self.assertGreater(report["shared_prefix_length"], 0)

    def test_resume_uses_breakpoint_snapshot(self):
        proc = self._run("resume", _SPEC, _SNAP)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["root"], "snapshot")
        # Continuation traces only mention the still-pending/running steps.
        for trace in report["traces"]:
            self.assertNotIn("start(compile-a)", trace)

    def test_replay_reports_workspace_and_statuses(self):
        proc = self._run(
            "replay", _SPEC, "start(compile-a)", "end(compile-a)"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["statuses"], ["ended", "pending", "pending"])
        self.assertEqual(report["workspace"]["artifact"], "a1")

    def test_illegal_trace_returns_nonzero(self):
        proc = self._run("replay", _SPEC, "end(compile-a)")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("TraceError", proc.stderr)

    def test_analyze_accepts_written_json_spec(self):
        spec = {
            "initial_values": {"x": 0},
            "outputs_of_interest": ["y"],
            "steps": [
                {
                    "name": "s",
                    "reads": ["x"],
                    "table": [{"when": [0], "publish": {"y": 7}}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(spec, handle)
            proc = self._run("analyze", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["deterministic"])
        self.assertEqual(report["final_outputs"], [7])


if __name__ == "__main__":
    unittest.main()
