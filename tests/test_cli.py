"""CLI 端到端测试：通过 app.main 跑完整核对流程。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


class CliTests(unittest.TestCase):
    def _write(self, directory, name, obj):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = self._write(tmp, "inv.json", {
                "batch": "B1",
                "assets": [{"asset_id": "A1", "volumes": ["C:"]}],
            })
            reps = self._write(tmp, "reps.json", {"reports": [{
                "asset_id": "A1", "batch": "B1", "volume": "C:", "sequence": 1,
                "observed_at": "2026-11-14T10:00:00Z", "encrypted": True,
                "key_id": "K1",
            }]})
            rcpts = self._write(tmp, "rcpts.json", {"receipts": [{
                "asset_id": "A1", "batch": "B1", "volume": "C:", "key_id": "K1",
            }]})
            out = os.path.join(tmp, "out.json")
            code = app.main([
                "--inventory", inv, "--reports", reps, "--receipts", rcpts,
                "--check-time", "2026-11-14T12:00:00Z", "--output", out,
            ])
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as fh:
                report = json.load(fh)
            self.assertEqual(report["devices"][0]["volumes"][0]["status"], "PASS")
            self.assertEqual(report["summary"]["devices_passed"], 1)

    def test_cli_bad_input_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._write(tmp, "bad.json", {"reports": [{"asset_id": "A1"}]})
            inv = self._write(tmp, "inv.json", {
                "batch": "B1",
                "assets": [{"asset_id": "A1", "volumes": ["C:"]}],
            })
            code = app.main([
                "--inventory", inv, "--reports", bad, "--receipts", bad,
                "--check-time", "2026-11-14T12:00:00Z",
            ])
            self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
