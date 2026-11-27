"""CLI 入口的端到端测试（使用 tests/fixtures 下的合成 JSON）。"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from sigcheck.cli import main

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_document.json")


class CliTest(unittest.TestCase):
    def test_cli_detects_violation_and_returns_exit_1(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main([FIXTURE])
        self.assertEqual(code, 1)
        report = json.loads(buf.getvalue())
        self.assertTrue(report["violated"])
        # r2 是允许字段填值（info），r3 改正文对象才是最早越界。
        self.assertEqual(
            report["earliest_violation"]["revision_id"], "r3"
        )
        self.assertEqual(
            report["earliest_violation"]["rule_id"], "object_modified"
        )
        assessment = report["assessments"][0]
        rule_ids = {item["rule_id"] for item in assessment["findings"]}
        self.assertIn("allowed_field_fill", rule_ids)
        self.assertIn("object_modified", rule_ids)

    def test_cli_writes_output_file_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "report.json")
            with redirect_stdout(io.StringIO()):
                code = main([FIXTURE, "-o", out_path])
            self.assertEqual(code, 1)
            with open(out_path, "r", encoding="utf-8") as handle:
                report = json.load(handle)
            self.assertTrue(report["violated"])

    def test_cli_clean_document_returns_exit_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clean_path = os.path.join(tmp, "clean.json")
            with open(FIXTURE, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            # 去掉越界的 r3 对象替换事件。
            payload["objects"] = [
                item
                for item in payload["objects"]
                if not (
                    item["object_id"] == "C1" and item["revision_id"] == "r3"
                )
            ]
            with open(clean_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = main([clean_path])
            self.assertEqual(code, 0)
            report = json.loads(buf.getvalue())
            self.assertFalse(report["violated"])

    def test_cli_invalid_model_returns_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = os.path.join(tmp, "bad.json")
            with open(bad_path, "w", encoding="utf-8") as handle:
                json.dump({"revisions": []}, handle)
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = main([bad_path])
            self.assertEqual(code, 2)
            self.assertIn("输入模型不合法", err.getvalue())

    def test_cli_missing_file_returns_exit_2(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = main(["/nonexistent/path/uid-17197/document.json"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
