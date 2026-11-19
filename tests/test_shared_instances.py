"""Acceptance: interleavings around shared instances and repeated redactions.

The main fixture shares one *instance* between pages.  These cases use
purpose-built graphs where two distinct instances reference the same text
object, and where several redactions land on the same page.
"""

from __future__ import annotations

import unittest

from docredact import Document, RedactionError, extract_page_text
from tests import support

TEXT = "共享原文 secret-token 尾部"
SECRET = "secret-token"


def two_instance_document() -> dict:
    start, end = support.span(TEXT, SECRET)
    return {
        "format": "docredact/1",
        "objects": {
            "txt": {
                "kind": "text",
                "text": TEXT,
                "speech": [
                    {"start": 0, "end": 4, "utterance": "共享原文"},
                    {"start": start, "end": end, "utterance": "secret token"},
                    {"start": end + 1, "end": len(TEXT), "utterance": "尾部"},
                ],
            },
            "inst_a": {"kind": "instance", "ref": "txt"},
            "inst_b": {"kind": "instance", "ref": "txt"},
            "inst_c": {"kind": "instance", "ref": "txt"},
            "root_a": {"kind": "container", "children": {"body": "inst_a"}},
            "root_b": {"kind": "container", "children": {"body": "inst_b"}},
            "root_c": {"kind": "container", "children": {"first": "inst_c", "second": "inst_c"}},
        },
        "pages": {
            "a": {"root": "root_a", "public": True},
            "b": {"root": "root_b", "public": True},
            "c": {"root": "root_c", "public": True},
        },
    }


class SharedInstanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document.from_dict(two_instance_document())
        self.start, self.end = support.span(TEXT, SECRET)

    def test_distinct_instances_of_one_text_split_cleanly(self) -> None:
        self.assertEqual(self.document.resolve_instance("a", ("body",)), "inst_a")
        self.assertEqual(self.document.resolve_instance("b", ("body",)), "inst_b")
        self.assertEqual(
            sorted(self.document.instances_referencing("txt")),
            [("a", ("body",), "inst_a"), ("b", ("body",), "inst_b"),
             ("c", ("first",), "inst_c"), ("c", ("second",), "inst_c")],
        )

        result = self.document.redact("a", ("body",), self.start, self.end)
        self.assertEqual(extract_page_text(result.document, "a"), TEXT.replace(SECRET, ""))
        for page in ("b", "c"):
            with self.subTest(page=page):
                self.assertIn(SECRET, extract_page_text(result.document, page))
        # The untouched instance still points at the pristine text object.
        self.assertEqual(result.document.objects["inst_b"]["ref"], "txt")
        self.assertEqual(result.document.objects["inst_a"]["ref"], "txt")

    def test_the_other_instance_stays_editable_through_the_shared_object(self) -> None:
        result = self.document.redact("a", ("body",), self.start, self.end)
        result.document.edit_text("txt", 0, 4, "公共")

        self.assertTrue(extract_page_text(result.document, "b").startswith("公共 secret-token"))
        self.assertNotIn(SECRET, extract_page_text(result.document, "a"))
        self.assertTrue(extract_page_text(result.document, "a").startswith("共享原文"))

    def test_two_instances_on_the_same_page_are_independent(self) -> None:
        result = self.document.redact("c", ("first",), self.start, self.end)
        rendered = extract_page_text(result.document, "c")
        self.assertEqual(rendered, TEXT.replace(SECRET, "") + TEXT)
        self.assertEqual(rendered.count(SECRET), 1)

        second = result.document.redact("c", ("second",), self.start, self.end)
        self.assertEqual(extract_page_text(second.document, "c"), TEXT.replace(SECRET, "") * 2)
        self.assertIn(SECRET, extract_page_text(second.document, "a"))

    def test_sequential_redactions_on_one_page_use_current_offsets(self) -> None:
        # Remove "共享" first; every later offset refers to the shortened text.
        first = self.document.redact("a", ("body",), 0, 2)
        self.assertEqual(extract_page_text(first.document, "a"), TEXT[2:])

        new_start, new_end = support.span(TEXT[2:], SECRET)
        second = first.document.redact("a", ("body",), new_start, new_end)
        self.assertEqual(extract_page_text(second.document, "a"), TEXT[2:].replace(SECRET, ""))
        self.assertEqual(second.removed_text, SECRET)
        self.assertEqual([s["utterance"] for s in second.removed_speech], ["secret token"])

        # Reusing the *old* offsets is no longer valid: the shortened text is
        # only 6 code points long, so the request is refused instead of quietly
        # deleting an innocent suffix.
        trimmed = TEXT[2:].replace(SECRET, "")
        self.assertEqual(len(trimmed), 6)
        with self.assertRaises(RedactionError):
            second.document.redact("a", ("body",), self.end, self.end + 1)
        # A stale offset that still fits is accepted, but the reported removal
        # lets the caller notice it did not remove what was intended.
        stale = second.document.redact("a", ("body",), len(trimmed) - 1, len(trimmed))
        self.assertEqual(stale.removed_text, trimmed[-1])
        self.assertNotEqual(stale.removed_text, SECRET)
        self.assertEqual(extract_page_text(stale.document, "a"), "原文  尾")

    def test_aliased_container_is_split_by_the_reference_path(self) -> None:
        # The same container reachable through two keys of the page root: only
        # the selected path may be rewritten.
        document = Document.from_dict(two_instance_document())
        aliased = Document(
            {**document.objects, "root": {"kind": "container", "children": {"left": "root_a", "right": "root_a"}}},
            {"p": {"root": "root", "public": True}},
        )
        self.assertEqual(extract_page_text(aliased, "p"), TEXT + TEXT)
        result = aliased.redact("p", ("left", "body"), self.start, self.end)
        self.assertEqual(extract_page_text(result.document, "p"), TEXT.replace(SECRET, "") + TEXT)
        # Redaction copied the aliased container, so the left branch now owns a
        # private text object.  Editing the shared original only reaches the
        # right branch -- exactly the "other occurrence stays editable" rule.
        result.document.edit_text("txt", 0, 2, "公共")
        self.assertEqual(
            extract_page_text(result.document, "p"),
            TEXT.replace(SECRET, "") + TEXT.replace("共享", "公共"),
        )

    def test_page_whose_root_is_an_instance_uses_an_empty_path(self) -> None:
        document = Document(
            {
                "txt": {"kind": "text", "text": "abcdef", "speech": []},
                "inst": {"kind": "instance", "ref": "txt"},
            },
            {"p": {"root": "inst", "public": True}},
        )
        self.assertEqual(document.resolve_instance("p", ()), "inst")
        result = document.redact("p", (), 1, 3)
        self.assertEqual(extract_page_text(result.document, "p"), "adef")
        self.assertEqual(extract_page_text(document, "p"), "abcdef")

    def test_empty_path_on_a_container_root_is_rejected(self) -> None:
        document = Document.from_dict(support.build_document())
        with self.assertRaises(RedactionError):
            document.redact("p1", (), 0, 1)


if __name__ == "__main__":
    unittest.main()
