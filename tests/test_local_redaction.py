"""Acceptance: one localised redaction must not disturb other pages."""

from __future__ import annotations

import unittest

from docredact import Document, extract_page_text
from tests import support

EMAIL = "alice@example.com"
CODENAME = "代号「曙光」"


class LocalRedactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document.from_dict(support.build_document())

    def _redact_email_on_p1(self):
        start, end = support.COPY_SPANS[EMAIL]
        return self.document.redact("p1", ("main", "body"), start, end)

    def test_redacts_only_the_selected_instance(self) -> None:
        result = self._redact_email_on_p1()

        self.assertEqual(result.removed_text, EMAIL)
        self.assertEqual(
            extract_page_text(result.document, "p1"),
            support.p1_text(copy=support.copy_without(EMAIL)),
        )
        self.assertNotIn(EMAIL, extract_page_text(result.document, "p1"))

    def test_other_pages_keep_the_original_text(self) -> None:
        result = self._redact_email_on_p1()

        for page in ("p2", "p3"):
            with self.subTest(page=page):
                self.assertEqual(extract_page_text(result.document, page), support.p2_text())
                self.assertIn(EMAIL, extract_page_text(result.document, page))

    def test_sibling_reuse_on_the_same_page_is_untouched(self) -> None:
        start, end = support.HEAD_SPANS[CODENAME]
        result = self.document.redact("p1", ("main", "lead"), start, end)

        # The "top" instance on p1 and the shared "lead" on p2 still render the
        # full head text; only p1's "main.lead" lost the code name.
        self.assertEqual(
            extract_page_text(result.document, "p1"),
            support.HEAD_TEXT + support.head_without(CODENAME) + support.COPY_TEXT,
        )
        self.assertEqual(extract_page_text(result.document, "p2"), support.p2_text())
        self.assertEqual(extract_page_text(result.document, "p3"), support.p2_text())

    def test_source_document_is_not_mutated(self) -> None:
        self._redact_email_on_p1()
        self.assertEqual(self.document.to_dict(), support.build_document())
        self.assertEqual(extract_page_text(self.document, "p1"), support.p1_text())

    def test_redacted_copy_of_shared_text_still_editable_locally(self) -> None:
        result = self._redact_email_on_p1()

        result.document.edit_text(result.new_text_id, 0, len("Project"), "Program")
        self.assertTrue(
            extract_page_text(result.document, "p1").startswith(
                support.HEAD_TEXT * 2 + "Program Aurora"
            )
        )
        # p2 keeps the pristine shared object and its original wording.
        self.assertEqual(extract_page_text(result.document, "p2"), support.p2_text())

    def test_shared_original_can_still_be_edited_after_redaction(self) -> None:
        result = self._redact_email_on_p1()

        start, end = support.COPY_SPANS["Owner: "]
        dropped = result.document.edit_text("txt_copy", start, end, "Reporter: ")
        self.assertEqual([s["utterance"] for s in dropped], ["Owner"])

        for page in ("p2", "p3"):
            self.assertIn("Reporter: alice@example.com", extract_page_text(result.document, page))
        # p1's local copy is deliberately unaffected by the shared edit.
        self.assertNotIn(EMAIL, extract_page_text(result.document, "p1"))
        self.assertIn("Owner: ", extract_page_text(result.document, "p1"))

    def test_two_redactions_on_different_pages_compose(self) -> None:
        start, end = support.COPY_SPANS[EMAIL]
        first = self.document.redact("p1", ("main", "body"), start, end)
        second = first.document.redact("p2", ("main", "body"), start, end)

        for page in ("p1", "p2"):
            with self.subTest(page=page):
                self.assertEqual(
                    extract_page_text(second.document, page),
                    support.p1_text(copy=support.copy_without(EMAIL)) if page == "p1"
                    else support.p2_text(copy=support.copy_without(EMAIL)),
                )
        self.assertEqual(extract_page_text(second.document, "p3"), support.p2_text())

    def test_resolved_paths_and_instance_reporting(self) -> None:
        self.assertEqual(self.document.resolve_instance("p1", ("main", "body")), "inst_copy")
        self.assertEqual(
            self.document.instances_referencing("txt_copy"),
            [
                ("p1", ("main", "body"), "inst_copy"),
                ("p2", ("main", "body"), "inst_copy"),
                ("p3", ("duplicate", "body"), "inst_copy"),
            ],
        )

    def test_dotted_string_path_matches_tuple_path(self) -> None:
        start, end = support.COPY_SPANS[EMAIL]
        via_string = self.document.redact("p1", "main.body", start, end)
        via_tuple = self.document.redact("p1", ("main", "body"), start, end)
        self.assertEqual(via_string.path, via_tuple.path)
        self.assertEqual(via_string.document.to_dict(), via_tuple.document.to_dict())

    def test_result_reports_object_identity(self) -> None:
        result = self._redact_email_on_p1()
        self.assertEqual(result.path, ("main", "body"))
        self.assertEqual(result.old_text_id, "txt_copy")
        self.assertEqual(result.old_instance_id, "inst_copy")
        self.assertNotEqual(result.new_text_id, result.old_text_id)
        self.assertEqual(
            result.document.objects[result.new_text_id]["text"],
            support.copy_without(EMAIL),
        )


if __name__ == "__main__":
    unittest.main()
