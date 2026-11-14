"""Malformed record handling beyond the registry and limit tests."""

from __future__ import annotations

import json
import unittest

from importer import core
from importer.errors import MISSING_FIELD, TYPE_ERROR
from tests.support import modern_record


def _import(record: dict) -> dict:
    return core.import_bytes(json.dumps(record).encode() + b"\n")


class RecordValidationTests(unittest.TestCase):
    def test_missing_version(self) -> None:
        record = modern_record()
        del record["version"]
        report = _import(record)
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], MISSING_FIELD)
        self.assertEqual(failure["field"], "$.version")

    def test_missing_task_type(self) -> None:
        record = modern_record()
        del record["task_type"]
        report = _import(record)
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], MISSING_FIELD)
        self.assertEqual(failure["field"], "$.task_type")

    def test_extensions_must_be_object(self) -> None:
        report = _import(modern_record(extensions=["not", "an", "object"]))
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], TYPE_ERROR)
        self.assertEqual(failure["field"], "$.extensions")

    def test_empty_record_id_is_invalid_value(self) -> None:
        report = _import(modern_record(record_id=""))
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], "invalid_value")
        self.assertEqual(failure["field"], "$.record_id")

    def test_non_string_label_item_is_located(self) -> None:
        report = _import(modern_record(labels=["ok", 7]))
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], TYPE_ERROR)
        self.assertEqual(failure["field"], "$.labels[1]")

    def test_document_unknown_field_is_located(self) -> None:
        report = _import(
            {
                "version": "2",
                "task_type": "document_analysis",
                "record_id": "d",
                "document": {"title": "t", "paragraphs": [], "extra": 1},
            }
        )
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], "unknown_field")
        self.assertEqual(failure["field"], "$.document.extra")

    def test_document_missing_paragraphs(self) -> None:
        report = _import(
            {
                "version": "2",
                "task_type": "document_analysis",
                "record_id": "d",
                "document": {"title": "t"},
            }
        )
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], MISSING_FIELD)
        self.assertEqual(failure["field"], "$.document.paragraphs")

    def test_document_wrong_type(self) -> None:
        report = _import(
            {
                "version": "2",
                "task_type": "document_analysis",
                "record_id": "d",
                "document": ["t"],
            }
        )
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], TYPE_ERROR)
        self.assertEqual(failure["field"], "$.document")

    def test_v1_record_may_not_use_v2_field_names(self) -> None:
        report = _import(
            {
                "version": "1",
                "task_type": "text_label",
                "record_id": "x",
                "text": "hello",
                "labels": ["a"],
            }
        )
        self.assertEqual(report["summary"]["accepted"], 0)
        classifications = {f["classification"] for f in report["failures"]}
        self.assertEqual(classifications, {"unknown_field"})


if __name__ == "__main__":
    unittest.main()
