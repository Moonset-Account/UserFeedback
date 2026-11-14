"""CLI 端到端测试（文件、标准输入、退出码）。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from tests.scenarios import SCENARIOS  # noqa: E402


class CliTest(unittest.TestCase):
    def _run_cli(self, args: list[str], stdin_text: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "app.py"), *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=30,
        )

    def test_file_input_output(self) -> None:
        scenario = SCENARIOS[0]
        with tempfile.TemporaryDirectory() as tmp:
            in_path = Path(tmp) / "in.json"
            out_path = Path(tmp) / "out.json"
            in_path.write_text(json.dumps(scenario["input"]), encoding="utf-8")
            proc = self._run_cli([str(in_path), "--output", str(out_path), "--pretty"])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(len(result["deployments"]), 4)
        self.assertEqual(result["schema_version"], "1.0")

    def test_stdin_stdout(self) -> None:
        scenario = SCENARIOS[0]
        proc = self._run_cli([], stdin_text=json.dumps(scenario["input"]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        statuses = {d["build_id"]: d["status"] for d in result["deployments"]}
        self.assertEqual(statuses["B-reverted"], "reaffected")

    def test_invalid_input_exit_code(self) -> None:
        proc = self._run_cli([], stdin_text="{not json")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("input_error", proc.stderr)

    def test_missing_file_exit_code(self) -> None:
        proc = self._run_cli(["/nonexistent/path/input.json"])
        self.assertEqual(proc.returncode, 3)


if __name__ == "__main__":
    unittest.main()
