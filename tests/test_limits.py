"""Configured size, depth, and list-item boundaries.

Every boundary is inclusive: exactly-at-limit passes, one-over fails.  The tests
measure the records they build and assert the real byte counts, so a limit
cannot be satisfied by a claim inside the payload.

Depth counts containers (objects and lists), with the record root at 1.
"""

from __future__ import annotations

import json
import unittest

from importer import core
from importer.errors import (
    DEPTH_LIMIT,
    INPUT_SIZE_LIMIT,
    LIST_ITEM_LIMIT,
    RECORD_SIZE_LIMIT,
)
from importer.limits import Limits
from tests.support import document_record, modern_record


def _line(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False).encode()


class RecordSizeBoundaryTests(unittest.TestCase):
    def test_exact_record_size_is_accepted(self) -> None:
        probe = modern_record(text="x" * 32)
        size = len(_line(probe))
        report = core.import_bytes(
            _line(probe) + b"\n", limits=Limits(max_record_bytes=size)
        )
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(report["records"][0]["byte_length"], size)

    def test_one_byte_over_record_size_is_rejected(self) -> None:
        probe = modern_record(text="x" * 32)
        size = len(_line(probe))
        report = core.import_bytes(
            _line(probe) + b"\n", limits=Limits(max_record_bytes=size - 1)
        )
        self.assertEqual(report["summary"]["accepted"], 0)
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], RECORD_SIZE_LIMIT)
        self.assertEqual(failure["field"], "$")
        self.assertIn(f"record is {size} bytes", failure["detail"])

    def test_oversized_record_does_not_stop_other_records(self) -> None:
        small = modern_record(record_id="kept", text="ok")
        big = modern_record(record_id="dropped", text="z" * 600)
        big_line = _line(big)
        report = core.import_bytes(
            _line(small) + b"\n" + big_line + b"\n",
            limits=Limits(max_record_bytes=len(big_line) - 1),
        )
        self.assertEqual(
            report["summary"], {"records_seen": 2, "accepted": 1, "rejected": 1}
        )
        self.assertEqual(report["records"][0]["record_id"], "kept")
        self.assertEqual(report["failures"][0]["record_id"], "dropped")
        self.assertEqual(report["failures"][0]["classification"], RECORD_SIZE_LIMIT)
        self.assertEqual(report["failures"][0]["line"], 2)

    def test_byte_length_is_measured_not_claimed(self) -> None:
        # A short field claiming to be large is irrelevant; a long field is
        # measured at its real length regardless of what it says about itself.
        probe = modern_record(
            text="y" * 4000, extensions={"import_batch": "declared-small"}
        )
        size = len(_line(probe))
        report = core.import_bytes(
            _line(probe) + b"\n", limits=Limits(max_record_bytes=size - 10)
        )
        self.assertEqual(report["failures"][0]["classification"], RECORD_SIZE_LIMIT)
        self.assertIn(f"record is {size} bytes", report["failures"][0]["detail"])


class FileSizeBoundaryTests(unittest.TestCase):
    def test_exact_file_size_is_accepted(self) -> None:
        raw = _line(modern_record()) + b"\n"
        report = core.import_bytes(raw, limits=Limits(max_file_bytes=len(raw)))
        self.assertEqual(report["summary"]["accepted"], 1)

    def test_one_byte_over_file_size_is_input_error(self) -> None:
        raw = _line(modern_record()) + b"\n"
        report = core.import_bytes(raw, limits=Limits(max_file_bytes=len(raw) - 1))
        self.assertEqual(report["summary"]["accepted"], 0)
        self.assertEqual(report["failures"][0]["classification"], INPUT_SIZE_LIMIT)
        self.assertEqual(core.exit_code(report), core.EXIT_INPUT_ERROR)


def nested_annotations(target_depth: int) -> dict:
    """Build ``extensions.annotations`` whose deepest object is ``target_depth``.

    Record root is depth 1, ``extensions`` is 2, the ``annotations`` value is 3;
    each wrapper object under it adds one more level.
    """

    assert target_depth >= 3
    node: dict = {"leaf": True}
    for _ in range(target_depth - 3):
        node = {"child": node}
    return modern_record(extensions={"annotations": node})


class DepthBoundaryTests(unittest.TestCase):
    def test_depth_exactly_at_limit_is_accepted(self) -> None:
        record = nested_annotations(5)
        report = core.import_bytes(
            _line(record) + b"\n", limits=Limits(max_object_depth=5)
        )
        self.assertEqual(report["summary"]["accepted"], 1)

    def test_depth_one_over_limit_is_rejected(self) -> None:
        record = nested_annotations(6)
        report = core.import_bytes(
            _line(record) + b"\n", limits=Limits(max_object_depth=5)
        )
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], DEPTH_LIMIT)
        self.assertEqual(failure["field"], "$.extensions.annotations.child.child.child")
        self.assertIn("depth 6", failure["detail"])

    def test_list_nesting_counts_toward_depth(self) -> None:
        # root=1, extensions=2, annotations=3, rows list=4, inner list=5.
        record = modern_record(extensions={"annotations": {"rows": [["deep"]]}})
        limit_three = core.import_bytes(
            _line(record) + b"\n", limits=Limits(max_object_depth=3)
        )
        failure = limit_three["failures"][0]
        self.assertEqual(failure["classification"], DEPTH_LIMIT)
        self.assertEqual(failure["field"], "$.extensions.annotations.rows")

        limit_four = core.import_bytes(
            _line(record) + b"\n", limits=Limits(max_object_depth=4)
        )
        self.assertEqual(limit_four["failures"][0]["classification"], DEPTH_LIMIT)
        self.assertEqual(limit_four["failures"][0]["field"], "$.extensions.annotations.rows[0]")

        limit_five = core.import_bytes(
            _line(record) + b"\n", limits=Limits(max_object_depth=5)
        )
        self.assertEqual(limit_five["summary"]["accepted"], 1)

    def test_depth_failure_reports_line_and_keeps_other_records(self) -> None:
        deep = nested_annotations(6)
        shallow = modern_record(record_id="shallow")
        report = core.import_bytes(
            _line(deep) + b"\n" + _line(shallow) + b"\n",
            limits=Limits(max_object_depth=5),
        )
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(report["records"][0]["record_id"], "shallow")
        self.assertEqual(report["failures"][0]["line"], 1)

    def test_pathological_depth_does_not_crash_other_records(self) -> None:
        deep = (
            b'{"version":"2","task_type":"text_classification","record_id":"deep",'
            b'"text":"t","labels":[],"extensions":{"annotations":'
            + b"[" * 5000
            + b"0"
            + b"]" * 5000
            + b"}}\n"
        )
        good = _line(modern_record(record_id="ok"))
        report = core.import_bytes(
            deep + good + b"\n", limits=Limits(max_record_bytes=60000)
        )
        self.assertEqual(report["summary"]["accepted"], 1)
        self.assertEqual(report["records"][0]["record_id"], "ok")
        self.assertEqual(report["failures"][0]["classification"], DEPTH_LIMIT)


class ListItemBoundaryTests(unittest.TestCase):
    def test_list_exactly_at_limit_is_accepted(self) -> None:
        record = modern_record(labels=[f"l{i}" for i in range(5)])
        report = core.import_bytes(
            _line(record) + b"\n", limits=Limits(max_list_items=5)
        )
        self.assertEqual(report["summary"]["accepted"], 1)

    def test_list_one_over_limit_is_rejected(self) -> None:
        record = modern_record(labels=[f"l{i}" for i in range(6)])
        report = core.import_bytes(
            _line(record) + b"\n", limits=Limits(max_list_items=5)
        )
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], LIST_ITEM_LIMIT)
        self.assertEqual(failure["field"], "$.labels")
        self.assertIn("6 items", failure["detail"])

    def test_nested_list_limit_is_located(self) -> None:
        record = document_record(
            document={"title": "t", "paragraphs": ["p0", "p1", "p2", "p3"]},
            extensions={"annotations": {"rows": [1, 2, 3, 4]}},
        )
        report = core.import_bytes(
            _line(record) + b"\n",
            limits=Limits(max_list_items=3, max_object_depth=6),
        )
        failure = report["failures"][0]
        self.assertEqual(failure["classification"], LIST_ITEM_LIMIT)
        self.assertEqual(failure["field"], "$.document.paragraphs")

    def test_limit_is_per_list_not_global(self) -> None:
        record = document_record(
            document={"title": "t", "paragraphs": ["p0", "p1", "p2", "p3"]},
            extensions={"annotations": {"rows": [1, 2, 3, 4]}},
        )
        report = core.import_bytes(
            _line(record) + b"\n",
            limits=Limits(max_list_items=4, max_object_depth=6),
        )
        self.assertEqual(report["summary"]["accepted"], 1)

    def test_list_item_limit_applies_to_declared_labels(self) -> None:
        # A record cannot dodge the list bound by declaring far fewer labels.
        record = modern_record(labels=[f"l{i}" for i in range(9)])
        report = core.import_bytes(
            _line(record) + b"\n", limits=Limits(max_list_items=8)
        )
        self.assertEqual(report["failures"][0]["classification"], LIST_ITEM_LIMIT)


if __name__ == "__main__":
    unittest.main()
