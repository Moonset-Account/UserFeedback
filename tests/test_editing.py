"""Acceptance: the surviving original object stays editable for other pages."""

from __future__ import annotations

import unittest

from docredact import Document, RedactionError, extract_page_text
from tests import support

EMAIL = "alice@example.com"
TITLE = "Project Aurora"
BODY = "launches in Q3."
OWNER = "Owner: "


class EditingSharedTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document.from_dict(support.build_document())
        start, end = support.COPY_SPANS[EMAIL]
        self.redacted = self.document.redact("p1", ("main", "body"), start, end).document

    def test_edit_propagates_to_every_page_that_still_shares_the_object(self) -> None:
        start, end = support.COPY_SPANS[TITLE]
        dropped = self.redacted.edit_text("txt_copy", start, end, "Program Aurora")
        # The replaced range was itself a spoken segment, so its utterance is
        # dropped; the surrounding segments keep their coordinates.
        self.assertEqual([s["utterance"] for s in dropped], [TITLE])
        for page in ("p2", "p3"):
            with self.subTest(page=page):
                self.assertEqual(
                    extract_page_text(self.redacted, page),
                    support.p2_text(copy=support.COPY_TEXT.replace(TITLE, "Program Aurora")),
                )
        # p1 keeps its private redacted copy untouched.
        self.assertEqual(
            extract_page_text(self.redacted, "p1"),
            support.p1_text(copy=support.copy_without(EMAIL)),
        )

    def test_edit_is_local_to_the_redacted_copy_too(self) -> None:
        redacted_copy = next(
            object_id for object_id in self.redacted.objects if object_id.startswith("txt_copy~")
        )
        start, end = support.COPY_SPANS[TITLE]
        self.redacted.edit_text(redacted_copy, start, end, "Program Aurora")
        self.assertIn("Program Aurora", extract_page_text(self.redacted, "p1"))
        self.assertEqual(extract_page_text(self.redacted, "p2"), support.p2_text())

    def test_insertion_shifts_later_segments_and_keeps_them_addressable(self) -> None:
        gap = support.COPY_SPANS[TITLE][1]  # between title and body
        dropped = self.redacted.edit_text("txt_copy", gap, gap, " —")
        self.assertEqual(dropped, [])
        node = self.redacted.objects["txt_copy"]
        body = next(s for s in node["speech"] if s["utterance"] == BODY)
        self.assertEqual(node["text"][body["start"]:body["end"]], BODY)
        self.assertEqual(body["start"], support.COPY_SPANS[BODY][0] + 2)
        # The segment immediately before the insertion point is untouched.
        title = next(s for s in node["speech"] if s["utterance"] == TITLE)
        self.assertEqual((title["start"], title["end"]), support.COPY_SPANS[TITLE])

    def test_insertion_inside_a_segment_drops_that_segment_only(self) -> None:
        inside = support.COPY_SPANS[TITLE][0] + 3
        dropped = self.redacted.edit_text("txt_copy", inside, inside, "X")
        self.assertEqual([s["utterance"] for s in dropped], [TITLE])
        node = self.redacted.objects["txt_copy"]
        self.assertEqual([s["utterance"] for s in node["speech"]], [BODY, "Owner", "alice at example dot com"])
        for segment in node["speech"]:
            self.assertGreaterEqual(segment["start"], 0)
            self.assertLessEqual(segment["end"], len(node["text"]))

    def test_replacement_overlapping_segments_drops_them(self) -> None:
        start, end = support.COPY_SPANS[OWNER]
        dropped = self.redacted.edit_text("txt_copy", start, end, "Reporter: ")
        self.assertEqual([s["utterance"] for s in dropped], ["Owner"])
        node = self.redacted.objects["txt_copy"]
        email = next(s for s in node["speech"] if s["utterance"] == "alice at example dot com")
        self.assertEqual(node["text"][email["start"]:email["end"]], EMAIL)
        self.assertIn("Reporter: alice@example.com", extract_page_text(self.redacted, "p2"))

    def test_invalid_edit_is_rejected_without_changing_the_object(self) -> None:
        before = self.redacted.objects["txt_copy"]
        snapshot = {key: (value if key != "speech" else [dict(s) for s in value]) for key, value in before.items()}
        with self.assertRaises(RedactionError):
            self.redacted.edit_text("txt_copy", 5, 2, "x")
        with self.assertRaises(RedactionError):
            self.redacted.edit_text("txt_copy", 0, len(support.COPY_TEXT) + 1, "x")
        with self.assertRaises(RedactionError):
            self.redacted.edit_text("txt_copy", 0, 1, None)  # type: ignore[arg-type]
        self.assertEqual(self.redacted.objects["txt_copy"], snapshot)
        self.assertEqual(extract_page_text(self.redacted, "p2"), support.p2_text())

    def test_editing_a_non_text_object_is_rejected(self) -> None:
        with self.assertRaises(RedactionError):
            self.redacted.edit_text("inst_copy", 0, 1, "x")
        with self.assertRaises(RedactionError):
            self.redacted.edit_text("sec_shared", 0, 1, "x")
        with self.assertRaises(RedactionError):
            self.redacted.edit_text("nope", 0, 1, "x")


if __name__ == "__main__":
    unittest.main()
