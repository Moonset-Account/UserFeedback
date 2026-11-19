"""Acceptance: independent verification of exported documents.

The verifier deliberately does not trust :meth:`Document.export`; it re-reads
the serialized mapping and rejects any export that still carries a private page
or an object unreachable from a public page.
"""

from __future__ import annotations

import json
import unittest

from docredact import DocError, Document, verify_export
from tests import support


class VerifyExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document.from_dict(support.build_document())

    def _redacted_export(self) -> dict:
        start, end = support.COPY_SPANS["alice@example.com"]
        return self.document.redact("p1", ("main", "body"), start, end).document.export()

    def test_clean_export_verifies_and_reports_extractable_text(self) -> None:
        summary = verify_export(self._redacted_export())
        self.assertEqual(summary["pages"], ["p1", "p2", "p3"])
        self.assertNotIn("secret", summary["pages"])
        self.assertEqual(
            summary["text"]["p1"],
            support.p1_text(copy=support.copy_without("alice@example.com")),
        )
        self.assertEqual(summary["text"]["p2"], support.p2_text())
        self.assertNotIn("alice at example dot com", [s["utterance"] for s in summary["speech"]["p1"]])

    def test_export_containing_a_private_page_is_rejected(self) -> None:
        leaked = self.document.to_dict()
        with self.assertRaises(DocError) as caught:
            verify_export(leaked)
        self.assertIn("private pages", str(caught.exception))

    def test_export_with_an_unreachable_object_is_rejected(self) -> None:
        exported = self._redacted_export()
        exported["objects"]["txt_extra"] = {"kind": "text", "text": "left over", "speech": []}
        with self.assertRaises(DocError) as caught:
            verify_export(exported)
        self.assertIn("unreachable objects", str(caught.exception))
        self.assertIn("txt_extra", str(caught.exception))

    def test_export_with_orphan_fixture_object_is_rejected(self) -> None:
        exported = self._redacted_export()
        exported["objects"]["txt_orphan"] = self.document.objects["txt_orphan"]
        with self.assertRaises(DocError):
            verify_export(exported)

    def test_verifier_accepts_a_document_with_no_private_pages(self) -> None:
        document = Document.from_dict(support.build_document())
        document.pages["secret"]["public"] = True
        summary = verify_export(document.export())
        self.assertIn("secret", summary["pages"])
        # The formerly private text is now legitimately reachable and present.
        self.assertIn("txt_secret", summary["objects"])

    def test_verifier_works_on_json_round_tripped_export(self) -> None:
        encoded = json.dumps(self._redacted_export(), ensure_ascii=False)
        summary = verify_export(json.loads(encoded))
        self.assertEqual(summary["pages"], ["p1", "p2", "p3"])
        self.assertNotIn("ALPHA-9", encoded)
        self.assertNotIn("backup@example.com", encoded)


if __name__ == "__main__":
    unittest.main()
