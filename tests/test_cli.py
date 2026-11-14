"""CLI 端到端测试：app.py --demo 与 --error 文件输入。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
PY = sys.executable


def run_cli(args, stdin=None):
    return subprocess.run(
        [PY, APP] + args, input=stdin, capture_output=True, text=True,
        cwd=ROOT, timeout=30)


class TestCliDemo(unittest.TestCase):
    def test_demo_outputs_both_views_with_same_trace_id(self):
        proc = run_cli(["--demo"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["client"]["error"]["trace_id"], "req-demo-0001")
        self.assertEqual(out["internal"]["trace_id"], "req-demo-0001")
        self.assertEqual(out["client"]["error"]["message"],
                         "支付未成功，请更换支付方式后重试")
        self.assertNotIn("s3cret-db-pass", proc.stdout)
        self.assertNotIn("tok-live-9f3c", proc.stdout)


class TestCliErrorFile(unittest.TestCase):
    def test_unknown_code_from_file_gets_generic_message(self):
        err = {"code": "UNLISTED_BUG", "message": "npe at line 9",
               "env": {"API_TOKEN": "tok-x"}}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(err, fh)
            path = fh.name
        try:
            proc = run_cli(["--interface", "orders.submit",
                            "--request-id", "req-file-1",
                            "--error", path])
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["client"]["error"]["code"], "INTERNAL_ERROR")
        self.assertEqual(out["client"]["error"]["message"], "系统繁忙，请稍后重试")
        self.assertEqual(out["internal"]["interface"], "orders.submit")
        self.assertEqual(out["internal"]["error"]["env"]["API_TOKEN"],
                         "***REDACTED***")
        self.assertNotIn("tok-x", proc.stdout)


if __name__ == "__main__":
    unittest.main()
