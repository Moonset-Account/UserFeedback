"""CLI 端到端测试：通过入口函数执行真实判定与复核流程。"""

import json
import unittest
from pathlib import Path

from release_gate.cli import main

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "bundle.json"


class TestCli(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(__file__).resolve().parent / "_tmp_cli"
        self.tmp_dir.mkdir(exist_ok=True)
        self.store_path = self.tmp_dir / "decisions.json"
        self.corrected_path = self.tmp_dir / "corrected.json"

    def tearDown(self):
        for child in self.tmp_dir.glob("*"):
            child.unlink()
        self.tmp_dir.rmdir()

    def _write_corrected(self, close_iso="2026-11-05T09:00:00Z"):
        data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        # 补录 EX-3 的提前收回事件，缩短其真实开放区间。
        data["events"].append(
            {
                "event_id": "EV-6",
                "exemption_id": "EX-3",
                "env_id": "prod-eu",
                "control_id": "C-01",
                "action": "CLOSE",
                "occurred_at": close_iso,
            }
        )
        self.corrected_path.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )

    def test_evaluate_returns_blocked_exit_code_and_persists(self):
        exit_code = main(
            ["evaluate", str(EXAMPLE), "--store", str(self.store_path)]
        )
        self.assertEqual(exit_code, 1)  # 存在被阻止的申请
        stored = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["records"]), 2)
        for record in stored["records"]:
            self.assertIn("bundle_snapshot", record)
            self.assertIn("decision", record)

    def test_review_flips_blocked_decision_after_correction(self):
        self.assertEqual(
            main(["evaluate", str(EXAMPLE), "--store", str(self.store_path)]),
            1,
        )
        self._write_corrected()
        # EX-3 由开放 4h(08:00-12:00) 缩短为 1h(08:00-09:00)，已用从 10h 降到 7h，
        # APP-OVER 的 3h 并入后 10h <= 12h，复核应发生翻转（退出码 1 表示有变化）。
        exit_code = main(
            ["review", str(self.corrected_path), "--store", str(self.store_path)]
        )
        self.assertEqual(exit_code, 1)

    def test_bad_input_file_returns_error_code(self):
        exit_code = main(
            ["evaluate", str(self.tmp_dir / "missing.json")]
        )
        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
