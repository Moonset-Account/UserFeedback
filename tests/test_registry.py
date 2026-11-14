"""Version / task-type whitelist behaviour."""

from __future__ import annotations

import json
import unittest

from importer import core
from importer.errors import (
    MISSING_FIELD,
    UNKNOWN_EXTENSION,
    UNKNOWN_FIELD,
    UNREGISTERED_COMBINATION,
    UNREGISTERED_TASK_TYPE,
    UNREGISTERED_VERSION,
)
from importer.limits import Limits
from tests.support import document_record, legacy_record, modern_record


def _import(records: list) -> dict:
    raw = ("\n".join(json.dumps(r) for r in records) + "\n").encode()
    return core.import_bytes(raw, limits=Limits())


class LegacyCompatibilityTests(unittest.TestCase):
    def test_v1_text_label_maps_to_v2_fields(self) -> None:
        report = _import([legacy_record()])
        self.assertEqual(report["summary"], {"records_seen": 1, "accepted": 1, "rejected": 0})
        record = report["records"][0]
        self.assertEqual(record["version"], "1")
        self.assertEqual(record["declared_task_type"], "text_label")
        self.assertEqual(record["canonical_task_type"], "text_classification")
        self.assertEqual(
            record["data"],
            {
                "record_id": "legacy-001",
                "text": "Is this review positive?",
                "labels": ["positive", "negative"],
            },
        )
        self.assertEqual(record["extensions"], {"legacy_source": "old-pipeline"})

    def test_v1_document_scan_maps_to_document_analysis(self) -> None:
        report = _import(
            [
                {
                    "version": "1",
                    "task_type": "document_scan",
                    "task_id": "old-doc-1",
                    "document_title": "Legacy memo",
                    "sections": ["First", "Second"],
                }
            ]
        )
        self.assertEqual(report["summary"]["accepted"], 1)
        record = report["records"][0]
        self.assertEqual(record["canonical_task_type"], "document_analysis")
        self.assertEqual(
            record["data"],
            {
                "record_id": "old-doc-1",
                "title": "Legacy memo",
                "paragraphs": ["First", "Second"],
            },
        )

    def test_modern_v2_records_still_accepted(self) -> None:
        report = _import([modern_record(), document_record()])
        self.assertEqual(report["summary"]["accepted"], 2)
        self.assertEqual(
            report["records"][1]["data"],
            {
                "record_id": "doc-001",
                "document": {
                    "title": "Quarterly report",
                    "paragraphs": ["Revenue rose.", "Costs fell."],
                },
            },
        )
        self.assertEqual(
            report["records"][1]["extensions"],
            {"annotations": {"reviewer": "synthetic", "round": 1}},
        )


class WhitelistRejectionTests(unittest.TestCase):
    def test_unknown_task_type_is_rejected_and_others_continue(self) -> None:
        bad = modern_record(record_id="bad-1", task_type="text_summarization")
        good = modern_record(record_id="good-1")
        report = _import([bad, good])

        self.assertEqual(report["summary"], {"records_seen": 2, "accepted": 1, "rejected": 1})
        self.assertEqual(report["records"][0]["record_id"], "good-1")
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], UNREGISTERED_TASK_TYPE)
        self.assertEqual(failure["field"], "$.task_type")
        self.assertEqual(failure["line"], 1)
        self.assertEqual(failure["record_id"], "bad-1")

    def test_unknown_version_is_rejected(self) -> None:
        report = _import([modern_record(version="99")])
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], UNREGISTERED_VERSION)
        self.assertEqual(failure["field"], "$.version")
        self.assertIn("1, 2", failure["detail"])

    def test_known_type_for_wrong_version_is_unregistered_combination(self) -> None:
        report = _import([legacy_record(version="2")])
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], UNREGISTERED_COMBINATION)
        self.assertEqual(failure["field"], "$.task_type")
        self.assertIn("version '1'", failure["detail"])

    def test_task_type_cannot_select_a_module_or_callable(self) -> None:
        hostile = [
            "os.system",
            "subprocess.run",
            "builtins.eval",
            "__import__",
            "json.loads",
            "../../etc/passwd",
            "text_label\n",
        ]
        records = [modern_record(record_id=f"h-{i}", task_type=t) for i, t in enumerate(hostile)]
        report = _import(records)
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertEqual(report["summary"]["rejected"], len(hostile))
        for failure in report["failures"]:
            self.assertEqual(failure["classification"], UNREGISTERED_TASK_TYPE)
            self.assertEqual(failure["field"], "$.task_type")

    def test_version_is_not_coerced(self) -> None:
        report = _import([modern_record(version=1)])
        self.assertEqual(report["failures"][0]["classification"], "type_error")
        self.assertEqual(report["failures"][0]["field"], "$.version")

    def test_extra_fields_are_not_silently_dropped(self) -> None:
        report = _import([legacy_record(unexpected="x")])
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], UNKNOWN_FIELD)
        self.assertEqual(failure["field"], "$.unexpected")

    def test_missing_required_field_is_located(self) -> None:
        record = legacy_record()
        del record["content"]
        report = _import([record])
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], MISSING_FIELD)
        self.assertEqual(failure["field"], "$.content")


class ExtensionTableTests(unittest.TestCase):
    def test_extension_from_other_version_is_rejected(self) -> None:
        report = _import(
            [modern_record(extensions={"legacy_tags": ["a"]})]
        )
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], UNKNOWN_EXTENSION)
        self.assertEqual(failure["field"], "$.extensions.legacy_tags")

    def test_extension_object_is_preserved_verbatim(self) -> None:
        nested = {"reviewer": "synthetic", "flags": ["a", "b"], "score": 3}
        report = _import([document_record(extensions={"annotations": nested})])
        self.assertEqual(report["records"][0]["extensions"], {"annotations": nested})

    def test_extension_type_is_checked(self) -> None:
        report = _import([modern_record(extensions={"source_system": 7})])
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], "type_error")
        self.assertEqual(failure["field"], "$.extensions.source_system")


if __name__ == "__main__":
    unittest.main()
