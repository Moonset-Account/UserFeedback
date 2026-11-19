"""Acceptance: invariants hold across randomly interleaved redactions and edits.

A fixed seed keeps the run reproducible.  After every operation the document
must still be a valid acyclic graph, every speech segment must address existing
code points, and no page other than the one being edited may change.
"""

from __future__ import annotations

import random
import unittest

from docredact import Document, RedactionError, extract_page_text, verify_export
from tests import support


class InvariantTest(unittest.TestCase):
    def _assert_document_invariants(self, document: Document) -> None:
        document.validate()
        for object_id, node in document.objects.items():
            if node["kind"] != "text":
                continue
            for segment in node.get("speech", []):
                self.assertGreaterEqual(segment["start"], 0, object_id)
                self.assertLessEqual(segment["end"], len(node["text"]), object_id)
                self.assertLess(segment["start"], segment["end"], object_id)

    def test_random_interleaving_keeps_invariants(self) -> None:
        rng = random.Random(20261120)
        document = Document.from_dict(support.build_document())
        pages = ["p1", "p2", "p3"]
        paths = {"p1": [("top",), ("main", "lead"), ("main", "body")],
                 "p2": [("main", "lead"), ("main", "body")],
                 "p3": [("duplicate", "lead"), ("duplicate", "body")]}

        for step in range(120):
            page = rng.choice(pages)
            path = rng.choice(paths[page])
            instance_id = document.resolve_instance(page, path)
            text_id = document.objects[instance_id]["ref"]
            length = len(document.objects[text_id]["text"])
            before = {p: extract_page_text(document, p) for p in pages}
            operation = rng.choice(["redact", "redact", "insert", "replace"])

            if length == 0:
                operation = "insert"

            if operation == "redact":
                if length < 2:
                    continue
                start = rng.randrange(length)
                end = rng.randrange(start + 1, length + 1)
                expected_removal = document.objects[text_id]["text"][start:end]
                result = document.redact(page, path, start, end)
                self.assertEqual(result.removed_text, expected_removal)
                self.assertEqual(result.start, start)
                self.assertEqual(result.end, end)
                expected_changed = {page}
                document = result.document
            else:
                # A shared-object edit is visible on every page that still
                # references that object -- that is the "still editable" rule.
                expected_changed = {
                    p for p in pages if self._references(document, p, text_id)
                }
                start = rng.randrange(length + 1)
                if operation == "insert":
                    document.edit_text(text_id, start, start, rng.choice(["X", "—", "𝄞"]))
                else:
                    if length < 1:
                        continue
                    end = rng.randrange(start, length + 1)
                    document.edit_text(text_id, start, end, rng.choice(["Y", ""]))

            self._assert_document_invariants(document)
            after = {p: extract_page_text(document, p) for p in pages}
            changed = {p for p in pages if after[p] != before[p]}
            self.assertTrue(
                changed <= expected_changed,
                f"step {step} ({operation}) changed {sorted(changed)} outside {sorted(expected_changed)}",
            )
            if operation == "redact":
                self.assertEqual(changed, {page}, f"step {step} local redaction leaked to {sorted(changed)}")

            exported = document.export()
            verify_export(exported)

    @staticmethod
    def _references(document: Document, page: str, text_id: str) -> bool:
        return any(
            document.objects[instance_id].get("ref") == text_id
            for _, instance_id in document.iter_instances(page)
        )

    def test_redaction_that_orphans_the_original_omits_it_from_export(self) -> None:
        document = Document(
            {
                "txt": {"kind": "text", "text": "only copy", "speech": []},
                "inst": {"kind": "instance", "ref": "txt"},
                "root": {"kind": "container", "children": {"body": "inst"}},
            },
            {"p": {"root": "root", "public": True}},
        )
        result = document.redact("p", ("body",), 0, 4)
        exported = result.document.export()
        self.assertNotIn("txt", exported["objects"])
        self.assertIn("txt~redacted", exported["objects"])
        verify_export(exported)
        # The pre-redaction document still owns both copies until exported.
        self.assertIn("txt", document.objects)

    def test_redaction_on_a_private_page_never_leaks_on_export(self) -> None:
        document = Document.from_dict(support.build_document())
        start, end = support.span(support.SECRET_TEXT, "ALPHA-9")
        result = document.redact("secret", ("lock",), start, end)
        self.assertNotIn("ALPHA-9", extract_page_text(result.document, "secret"))

        exported = result.document.export()
        self.assertNotIn("ALPHA-9", str(exported))
        self.assertNotIn("txt_secret~redacted", exported["objects"])
        verify_export(exported)

        with_private = result.document.export(public_only=False)
        self.assertIn("secret", with_private["pages"])
        self.assertIn("txt_secret~redacted", with_private["objects"])
        self.assertNotIn("ALPHA-9", str(with_private))

    def test_failed_operation_leaves_document_untouched(self) -> None:
        document = Document.from_dict(support.build_document())
        before = document.to_dict()
        bad_operations = [
            lambda: document.redact("p1", ("main", "body"), 3, 3),
            lambda: document.redact("p1", ("main", "body"), -5, 1),
            lambda: document.redact("p1", ("main", "nope"), 0, 1),
            lambda: document.redact("p1", ("main", "lead"), 0, 999),
            lambda: document.edit_text("inst_copy", 0, 1, "x"),
            lambda: document.edit_text("txt_copy", 1, 0, "x"),
        ]
        for operation in bad_operations:
            with self.assertRaises(RedactionError):
                operation()
            self.assertEqual(document.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
