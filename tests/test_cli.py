"""CLI 入口验收：退出码、报告文件、阻断摘要均执行真实断言。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compat_audit.cli import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(ROOT, "fixtures", "sample_config.json")
RUNNER = os.path.join(ROOT, "run_audit.py")


class CliInProcessTest(unittest.TestCase):
    def test_blocking_fixture_returns_exit_code_2(self):
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            code = main([FIXTURE])
        self.assertEqual(code, 2)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["summary"]["blocked"], 3)

    def test_report_file_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.json")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main([FIXTURE, "--report", out])
            self.assertEqual(code, 2)
            with open(out, encoding="utf-8") as fh:
                payload = json.load(fh)
        pair_ids = [p["pair"] for p in payload["pairs"]]
        self.assertEqual(len(pair_ids), 5)
        self.assertIn("producer-old -> consumer-future", pair_ids)

    def test_bad_config_returns_exit_code_1(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            fh.write("{not valid json")
            bad_path = fh.name
        try:
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main([bad_path])
            self.assertEqual(code, 1)
            self.assertIn("配置错误", err.getvalue())
        finally:
            os.unlink(bad_path)

    def test_missing_file_returns_exit_code_1(self):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            code = main(["/nonexistent/no-such-config.json"])
        self.assertEqual(code, 1)


class CliSubprocessTest(unittest.TestCase):
    """通过隔离模式子进程验证真实命令行入口（-I 下不依赖 cwd 注入）。"""

    def test_runner_under_isolated_python(self):
        proc = subprocess.run(
            [sys.executable, "-I", RUNNER, FIXTURE],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["summary"]["total"], 5)
        # stderr 上必须输出每组生产者/消费者的结论行
        self.assertIn("producer-old -> consumer-old", proc.stderr)
        self.assertIn("status=blocked", proc.stderr)
        self.assertIn("status=no_overlap", proc.stderr)

    def test_module_entry_without_isolation_also_works(self):
        # 非隔离模式下 -m 入口同样可用
        proc = subprocess.run(
            [sys.executable, "-m", "compat_audit", FIXTURE],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["summary"]["compatible"], 1)


if __name__ == "__main__":
    unittest.main()
