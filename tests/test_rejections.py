"""Acceptance: rejected requests and rejected documents.

Every case here must be refused rather than silently producing a document that
leaks text, loses speech alignment, or serializes private objects.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from docredact import DocError, Document, RedactionError, extract_page_text
from tests import support


class RejectedRedactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document.from_dict(support.build_document())

    def _counts(self) -> tuple[int, int]:
        return len(self.document.objects), len(self.document.pages)

    def test_reversed_range_is_rejected(self) -> None:
        before = self._counts()
        with self.assertRaises(RedactionError):
            self.document.redact("p1", ("main", "body"), 10, 5)
        self.assertEqual(self._counts(), before)

    def test_empty_range_is_rejected(self) -> None:
        with self.assertRaises(RedactionError):
            self.document.redact("p1", ("main", "body"), 7, 7)

    def test_end_past_text_length_is_rejected(self) -> None:
        length = len(support.COPY_TEXT)
        with self.assertRaises(RedactionError):
            self.document.redact("p1", ("main", "body"), 0, length + 1)

    def test_negative_start_is_rejected(self) -> None:
        with self.assertRaises(RedactionError):
            self.document.redact("p1", ("main", "body"), -1, 3)

    def test_non_integer_offsets_are_rejected(self) -> None:
        for bad in (1.0, "1", True, None):
            with self.subTest(value=bad):
                with self.assertRaises(RedactionError):
                    self.document.redact("p1", ("main", "body"), bad, 5)

    def test_unknown_page_is_rejected(self) -> None:
        with self.assertRaises(RedactionError):
            self.document.redact("nope", ("main", "body"), 0, 1)

    def test_path_through_text_is_rejected(self) -> None:
        with self.assertRaises(RedactionError):
            self.document.redact("p1", ("top", "deeper"), 0, 1)

    def test_missing_path_step_is_rejected(self) -> None:
        with self.assertRaises(RedactionError):
            self.document.redact("p1", ("main", "footer"), 0, 1)

    def test_path_ending_at_a_container_is_rejected(self) -> None:
        with self.assertRaises(RedactionError):
            self.document.redact("p1", ("main",), 0, 1)

    def test_path_ending_at_text_is_rejected(self) -> None:
        # A resolved instance is required, not the raw text object.
        with self.assertRaises(RedactionError):
            self.document.redact("p1", ("main", "body", "ref"), 0, 1)


class RejectedDocumentTest(unittest.TestCase):
    def test_cycle_is_rejected(self) -> None:
        with self.assertRaises(DocError):
            Document(
                {
                    "a": {"kind": "container", "children": {"next": "b"}},
                    "b": {"kind": "container", "children": {"next": "a"}},
                },
                {"p": {"root": "a"}},
            )

    def test_self_referential_instance_is_rejected(self) -> None:
        with self.assertRaises(DocError):
            Document(
                {"i": {"kind": "instance", "ref": "i"}},
                {"p": {"root": "i"}},
            )

    def test_unknown_reference_is_rejected(self) -> None:
        with self.assertRaises(DocError):
            Document({"i": {"kind": "instance", "ref": "ghost"}}, {"p": {"root": "i"}})

    def test_speech_out_of_bounds_is_rejected(self) -> None:
        with self.assertRaises(DocError):
            Document(
                {
                    "t": {"kind": "text", "text": "abc", "speech": [{"start": 2, "end": 9, "utterance": "x"}]},
                    "i": {"kind": "instance", "ref": "t"},
                },
                {"p": {"root": "i"}},
            )

    def test_backwards_speech_segment_is_rejected(self) -> None:
        with self.assertRaises(DocError):
            Document(
                {
                    "t": {"kind": "text", "text": "abc", "speech": [{"start": 2, "end": 2, "utterance": "x"}]},
                    "i": {"kind": "instance", "ref": "t"},
                },
                {"p": {"root": "i"}},
            )

    def test_unknown_page_root_is_rejected(self) -> None:
        with self.assertRaises(DocError):
            Document({"t": {"kind": "text", "text": "x"}}, {"p": {"root": "missing"}})

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(DocError):
            Document({"t": {"kind": "blob", "data": 1}}, {"p": {"root": "t"}})

    def test_wrong_format_is_rejected(self) -> None:
        with self.assertRaises(DocError):
            Document.from_dict({"format": "docredact/999", "objects": {}, "pages": {}})

    def test_bad_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "broken.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(DocError):
                Document.load(path)

    def test_rejected_redaction_leaves_document_usable(self) -> None:
        document = Document.from_dict(support.build_document())
        with self.assertRaises(RedactionError):
            document.redact("p1", ("main", "body"), 5, 2)
        start, end = support.COPY_SPANS["alice@example.com"]
        result = document.redact("p1", ("main", "body"), start, end)
        self.assertEqual(
            extract_page_text(result.document, "p1"),
            support.p1_text(copy=support.copy_without("alice@example.com")),
        )
        self.assertEqual(json.loads(json.dumps(document.to_dict())), support.build_document())


if __name__ == "__main__":
    unittest.main()
