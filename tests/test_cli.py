"""CLI 入口验收：以子进程执行真实命令并断言 JSON 输出。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class CliSmokeTests(unittest.TestCase):
    def test_payload_mode_returns_decision_json(self):
        payload = ROOT / "examples" / "payload_forward.json"
        proc = subprocess.run(
            [sys.executable, "-m", "mail_security", "--payload", str(payload)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        decision = json.loads(proc.stdout)
        self.assertEqual(decision["disposition"], "deliver")
        self.assertEqual(decision["dmarc_verdict"], "pass")
        self.assertEqual(decision["evidence_used"]["source"], "upstream")
        self.assertEqual(decision["evidence_used"]["message_id"], "msg-fwd-002")
        self.assertIn("upstream_result_adopted", decision["reasons"])

    def test_config_and_message_mode_direct_mail(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mail_security",
                "--config",
                str(ROOT / "examples" / "config.json"),
                "--message",
                str(ROOT / "examples" / "message_direct.json"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        decision = json.loads(proc.stdout)
        self.assertEqual(decision["disposition"], "deliver")
        self.assertEqual(decision["evidence_used"]["method"], "dkim")

    def test_bad_input_file_exits_nonzero(self):
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mail_security",
                "--payload",
                str(ROOT / "examples" / "config.json"),  # 缺少 message 键
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("error", json.loads(proc.stderr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
