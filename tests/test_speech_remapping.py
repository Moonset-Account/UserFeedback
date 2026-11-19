"""Acceptance: speech (assistive reading) segments follow the removed range.

Rule under test: a segment is dropped whole when its source range intersects
the removed range; segments starting at or after the removed range shift left
by the number of removed code points; segments wholly before it are untouched.
"""

from __future__ import annotations

import unittest

from docredact import Document, extract_page_speech, extract_page_text
from tests import support

TITLE = "Project Aurora"
BODY = "launches in Q3."
OWNER = "Owner: "
EMAIL = "alice@example.com"
UTTERANCE = {
    TITLE: "Project Aurora",
    BODY: "launches in Q3.",
    OWNER: "Owner",
    EMAIL: "alice at example dot com",
}


def utterance_list(document, page: str) -> list[str]:
    return [segment["utterance"] for segment in extract_page_speech(document, page)]


def copy_utterance_list(document, page: str) -> list[str]:
    return [
        segment["utterance"]
        for segment in extract_page_speech(document, page)
        if segment["object"] == "txt_copy" or segment["object"].startswith("txt_copy~")
    ]


class SpeechRemappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document.from_dict(support.build_document())
        self.text = support.COPY_TEXT
        self.segments = support.SEGMENTS_BY_TEXT["txt_copy"]

    def _redact_copy(self, start: int, end: int):
        return self.document.redact("p1", ("main", "body"), start, end)

    def test_partial_overlap_removes_the_whole_segment(self) -> None:
        start, end = support.span(self.text, "alice@")
        result = self._redact_copy(start, end)
        self.assertEqual([s["utterance"] for s in result.removed_speech], [UTTERANCE[EMAIL]])
        self.assertNotIn(UTTERANCE[EMAIL], utterance_list(result.document, "p1"))

    def test_partial_overlap_at_segment_tail_removes_whole_segment(self) -> None:
        start, end = support.span(self.text, "Q3.")
        result = self._redact_copy(start, end)
        # "launches in Q3." overlaps by its last three code points and goes
        # entirely; the adjacent "Owner: " segment starts after the range.
        self.assertEqual([s["utterance"] for s in result.removed_speech], [UTTERANCE[BODY]])
        self.assertIn(UTTERANCE[OWNER], utterance_list(result.document, "p1"))
        self.assertEqual(utterance_list(result.document, "p1")[-1], UTTERANCE[EMAIL])

    def test_touching_segment_is_not_removed(self) -> None:
        # Delete exactly "launches in Q3."; the following "Owner: " segment
        # starts at the deletion point but does not overlap the removed range.
        start, end = support.COPY_SPANS[BODY]
        result = self._redact_copy(start, end)
        self.assertEqual([s["utterance"] for s in result.removed_speech], [UTTERANCE[BODY]])

        node = result.document.objects[result.new_text_id]
        owner = next(s for s in node["speech"] if s["utterance"] == UTTERANCE[OWNER])
        self.assertEqual(node["text"][owner["start"]:owner["end"]], OWNER)
        self.assertEqual(owner["start"], start + 1)

    def test_segments_after_the_range_shift_left(self) -> None:
        start, end = support.COPY_SPANS[TITLE]
        result = self._redact_copy(start, end)
        node = result.document.objects[result.new_text_id]
        shift = end - start

        expected = []
        for segment in self.segments:
            if segment["start"] < end and segment["end"] > start:
                continue
            if segment["start"] >= end:
                expected.append((segment["start"] - shift, segment["end"] - shift, segment["utterance"]))
            else:
                expected.append((segment["start"], segment["end"], segment["utterance"]))
        self.assertEqual(
            [(s["start"], s["end"], s["utterance"]) for s in node["speech"]],
            expected,
        )
        # Surviving coordinates address the trimmed text exactly.
        for segment in node["speech"]:
            self.assertGreaterEqual(segment["start"], 0)
            self.assertLessEqual(segment["end"], len(node["text"]))
        owner = next(s for s in node["speech"] if s["utterance"] == UTTERANCE[OWNER])
        self.assertEqual(node["text"][owner["start"]:owner["end"]], OWNER)
        email = next(s for s in node["speech"] if s["utterance"] == UTTERANCE[EMAIL])
        self.assertEqual(node["text"][email["start"]:email["end"]], EMAIL)
        body = next(s for s in node["speech"] if s["utterance"] == UTTERANCE[BODY])
        self.assertEqual(node["text"][body["start"]:body["end"]], BODY)

    def test_speech_of_other_pages_is_unchanged(self) -> None:
        start, end = support.COPY_SPANS[TITLE]
        result = self._redact_copy(start, end)
        for page in ("p2", "p3"):
            with self.subTest(page=page):
                self.assertEqual(
                    list(self._copy_segments(result.document, page)),
                    [(s["start"], s["end"], s["utterance"]) for s in self.segments],
                )
        self.assertEqual(extract_page_text(result.document, "p2"), support.p2_text())

    def _copy_segments(self, document: Document, page: str):
        for path, instance_id in document.iter_instances(page):
            if instance_id == "inst_copy":
                object_id = document.objects[instance_id]["ref"]
                for segment in document.objects[object_id]["speech"]:
                    yield segment["start"], segment["end"], segment["utterance"]

    def test_segment_covering_entire_redaction_is_removed(self) -> None:
        start, end = support.COPY_SPANS[EMAIL]
        result = self._redact_copy(start, end)
        self.assertEqual([s["utterance"] for s in result.removed_speech], [UTTERANCE[EMAIL]])
        self.assertEqual(
            copy_utterance_list(result.document, "p1"),
            [UTTERANCE[TITLE], UTTERANCE[BODY], UTTERANCE[OWNER]],
        )

    def test_redaction_in_a_speech_gap_keeps_all_segments(self) -> None:
        gap = support.COPY_SPANS[TITLE][1]  # the space between title and body
        self.assertFalse(any(s["start"] < gap < s["end"] for s in self.segments))
        result = self._redact_copy(gap, gap + 1)
        self.assertEqual(result.removed_speech, ())
        self.assertEqual(len(result.document.objects[result.new_text_id]["speech"]), len(self.segments))

    def test_removed_segments_are_reported_once_each(self) -> None:
        result = self._redact_copy(0, len(self.text))
        self.assertEqual(
            [s["utterance"] for s in result.removed_speech],
            [UTTERANCE[TITLE], UTTERANCE[BODY], UTTERANCE[OWNER], UTTERANCE[EMAIL]],
        )
        self.assertEqual(len({id(s) for s in result.removed_speech}), len(result.removed_speech))


if __name__ == "__main__":
    unittest.main()
