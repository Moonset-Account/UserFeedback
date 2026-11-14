"""Input framing, per-record error isolation, and CLI behaviour."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from importer import core
from importer.errors import (
    DUPLICATE_FIELD,
    INVALID_ENCODING,
    INVALID_JSON,
    RECORD_SHAPE,
    UNSUPPORTED_VALUE,
)
from importer.limits import Limits

WORKSPACE = Path(__file__).resolve().parent.parent
APP = WORKSPACE / "app.py"


def _jsonl(records) -> bytes:
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode()


def _modern(record_id: str = "r1", **overrides) -> dict:
    record = {
        "version": "2",
        "task_type": "text_classification",
        "record_id": record_id,
        "text": "hello",
        "labels": ["a"],
    }
    record.update(overrides)
    return record


class FramingTests(unittest.TestCase):
    def test_json_array_input_is_accepted(self) -> None:
        raw = json.dumps([_modern("a"), _modern("b")]).encode()
        report = core.import_bytes(raw)
        self.assertEqual(report["summary"]["accepted"], 2)
        self.assertEqual([r["record_id"] for r in report["records"]], ["a", "b"])

    def test_json_array_reports_one_based_lines(self) -> None:
        payload = json.dumps([_modern("a"), _modern("b")], indent=2).encode()
        report = core.import_bytes(payload)
        self.assertEqual(report["summary"]["accepted"], 2)
        # The array's opening bracket occupies line 1; elements start on line 2.
        self.assertEqual(report["records"][0]["line"], 2)
        self.assertGreater(report["records"][1]["line"], 2)

    def test_single_line_array_failure_reports_line_one(self) -> None:
        report = core.import_bytes(
            json.dumps([_modern("a"), {"version": "2", "task_type": "nope"}]).encode()
        )
        self.assertEqual(report["failures"][0]["classification"], "unregistered_task_type")
        self.assertEqual(report["failures"][0]["line"], 1)

    def test_blank_lines_are_skipped_and_line_numbers_kept(self) -> None:
        raw = b"\n\n" + json.dumps(_modern("ok")).encode() + b"\n\n"
        report = core.import_bytes(raw)
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(report["records"][0]["line"], 3)

    def test_crlf_line_endings_are_supported(self) -> None:
        raw = json.dumps(_modern("ok")).encode() + b"\r\n"
        report = core.import_bytes(raw)
        self.assertEqual(report["summary"]["accepted"], 1)

    def test_empty_file_yields_no_records_and_no_failures(self) -> None:
        report = core.import_bytes(b"")
        self.assertEqual(
            report["summary"], {"records_seen": 0, "accepted": 0, "rejected": 0}
        )
        self.assertEqual(core.exit_code(report), core.EXIT_OK)

    def test_unicode_line_separator_does_not_split_a_record(self) -> None:
        # U+2028 is legal inside a JSON string and must not be treated as a
        # physical line break by the JSONL splitter.
        record = _modern("u2028", text="before after")
        payload = json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
        report = core.import_bytes(payload)
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(report["records"][0]["data"]["text"], "before after")

    def test_utf8_bom_is_tolerated(self) -> None:
        payload = b"\xef\xbb\xbf" + json.dumps(_modern("bom")).encode() + b"\n"
        report = core.import_bytes(payload)
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(report["records"][0]["record_id"], "bom")

    def test_trailing_content_after_array_is_rejected(self) -> None:
        payload = json.dumps([_modern("a")]).encode() + b'\n{"version":"2"}'
        report = core.import_bytes(payload)
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(report["failures"][0]["classification"], INVALID_JSON)
        self.assertIn("trailing content", report["failures"][0]["detail"])

    def test_empty_json_array_yields_no_records(self) -> None:
        report = core.import_bytes(b"[]\n")
        self.assertEqual(
            report["summary"], {"records_seen": 0, "accepted": 0, "rejected": 0}
        )
        self.assertEqual(core.exit_code(report), core.EXIT_OK)


class PerRecordErrorTests(unittest.TestCase):
    def test_invalid_json_isolated_to_one_line(self) -> None:
        raw = (
            json.dumps(_modern("good1")).encode()
            + b"\n{not json}\n"
            + json.dumps(_modern("good2")).encode()
            + b"\n"
        )
        report = core.import_bytes(raw)
        self.assertEqual(
            report["summary"], {"records_seen": 3, "accepted": 2, "rejected": 1}
        )
        self.assertEqual(
            [r["record_id"] for r in report["records"]], ["good1", "good2"]
        )
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], INVALID_JSON)
        self.assertEqual(failure["field"], "$")
        self.assertEqual(failure["line"], 2)
        self.assertGreaterEqual(failure["column"], 1)

    def test_jsonl_non_object_line_is_shape_error(self) -> None:
        raw = json.dumps(_modern("ok")).encode() + b'\n"just a string"\n'
        report = core.import_bytes(raw)
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(report["failures"][0]["classification"], RECORD_SHAPE)
        self.assertEqual(report["failures"][0]["line"], 2)

    def test_duplicate_key_is_rejected_with_location(self) -> None:
        raw = (
            b'{"version":"2","task_type":"text_classification","record_id":"dup",'
            b'"record_id":"dup2","text":"t","labels":[]}\n'
        )
        report = core.import_bytes(raw)
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], DUPLICATE_FIELD)
        self.assertEqual(failure["field"], "$.record_id")

    def test_duplicate_key_in_nested_record_is_located(self) -> None:
        raw = (
            b'{"version":"2","task_type":"document_analysis","record_id":"d",'
            b'"document":{"title":"t","title":"t2","paragraphs":[]}}\n'
        )
        report = core.import_bytes(raw)
        self.assertEqual(report["failures"][0]["classification"], DUPLICATE_FIELD)
        self.assertEqual(report["failures"][0]["field"], "$.document.title")

    def test_nan_constant_is_rejected(self) -> None:
        raw = (
            b'{"version":"2","task_type":"text_classification","record_id":"n",'
            b'"text":"t","labels":[],"extensions":{"annotations":{"x":NaN}}}\n'
        )
        report = core.import_bytes(raw)
        self.assertEqual(report["failures"][0]["classification"], UNSUPPORTED_VALUE)
        self.assertEqual(report["failures"][0]["field"], "$")
        self.assertEqual(report["failures"][0]["line"], 1)

    def test_invalid_utf8_file_fails_whole_input(self) -> None:
        report = core.import_bytes(b'{"version":"2"}\n\xff\xfe')
        self.assertEqual(report["failures"][0]["classification"], INVALID_ENCODING)
        self.assertEqual(core.exit_code(report), core.EXIT_INPUT_ERROR)


class LimitsConfigTests(unittest.TestCase):
    def test_partial_override_keeps_defaults(self) -> None:
        from importer.limits import load_limits

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "limits.json"
            path.write_text('{"max_list_items": 3}', encoding="utf-8")
            limits = load_limits(str(path))
        self.assertEqual(limits.max_list_items, 3)
        self.assertEqual(limits.max_file_bytes, Limits().max_file_bytes)

    def test_invalid_limits_are_rejected(self) -> None:
        from importer.limits import LimitsConfigError, load_limits

        cases = [
            '{"max_list_items": 0}',
            '{"max_list_items": true}',
            '{"max_list_items": "3"}',
            '{"unknown_key": 1}',
            "[]",
            "{not json}",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "limits.json"
            for case in cases:
                path.write_text(case, encoding="utf-8")
                with self.subTest(case=case):
                    with self.assertRaises(LimitsConfigError):
                        load_limits(str(path))


class CliTests(unittest.TestCase):
    def _run(self, args, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(APP), *args],
            capture_output=True,
            text=True,
            cwd=WORKSPACE,
            timeout=30,
            **kwargs,
        )

    def test_exit_zero_and_report_on_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.jsonl"
            path.write_text(json.dumps(_modern("x")) + "\n", encoding="utf-8")
            result = self._run(["import", str(path)])
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(report["tool"], core.REPORT_TOOL)

    def test_exit_one_when_one_record_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "in.jsonl"
            path.write_text(
                json.dumps(_modern("ok"))
                + "\n"
                + json.dumps(_modern("bad", task_type="x"))
                + "\n",
                encoding="utf-8",
            )
            result = self._run(["import", str(path), "--compact"])
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(
            report["failures"][0]["classification"], "unregistered_task_type"
        )

    def test_missing_input_is_input_error(self) -> None:
        result = self._run(["import", "/definitely/not/here.jsonl"])
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual(report["failures"][0]["classification"], "input_unreadable")

    def test_bad_limits_file_is_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "in.jsonl"
            inp.write_text(json.dumps(_modern("x")) + "\n", encoding="utf-8")
            lim = Path(tmp) / "limits.json"
            lim.write_text('{"max_object_depth": -1}', encoding="utf-8")
            result = self._run(["import", str(inp), "--limits", str(lim)])
        self.assertEqual(result.returncode, 64)
        self.assertIn("limits configuration rejected", result.stderr)

    def test_report_file_and_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "in.jsonl"
            inp.write_text(json.dumps(_modern("x")) + "\n", encoding="utf-8")
            out = Path(tmp) / "out.json"
            result = self._run(["import", str(inp), "--report", str(out), "--quiet"])
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertTrue(out.exists())
            self.assertEqual(
                json.loads(out.read_text(encoding="utf-8"))["summary"]["accepted"], 1
            )

    def test_input_file_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inp = Path(tmp) / "in.jsonl"
            payload = json.dumps(_modern("x")) + "\n"
            inp.write_text(payload, encoding="utf-8")
            before = inp.read_bytes()
            self._run(["import", str(inp)])
            self.assertEqual(inp.read_bytes(), before)


class NoDynamicLoadingTests(unittest.TestCase):
    """The decoder table is data; nothing resolves a task type by name."""

    FORBIDDEN = (
        "importlib",
        "eval(",
        "exec(",
        "__import__",
        "getattr(",
        "globals()",
        "pkgutil",
        "sys.modules",
    )

    def test_importer_sources_contain_no_dynamic_loading(self) -> None:
        offenders = []
        for path in sorted((WORKSPACE / "importer").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for token in self.FORBIDDEN:
                if token in text:
                    offenders.append(f"{path.name}: {token}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
