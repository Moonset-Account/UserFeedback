"""Acceptance: export serializes only publicly reachable objects and re-extracts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from docredact import Document, extract_page_speech, extract_page_text
from tests import support


class ExportReachabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.document = Document.from_dict(support.build_document())

    def _redacted(self) -> Document:
        start, end = support.COPY_SPANS["alice@example.com"]
        return self.document.redact("p1", ("main", "body"), start, end).document

    def test_private_page_objects_are_not_serialized(self) -> None:
        exported = self._redacted().export()
        self.assertNotIn("txt_secret", exported["objects"])
        self.assertNotIn("inst_secret", exported["objects"])
        self.assertNotIn("pg_secret", exported["objects"])
        self.assertNotIn("secret", exported["pages"])
        self.assertNotIn("ALPHA-9", json.dumps(exported, ensure_ascii=False))

    def test_orphan_objects_are_not_serialized(self) -> None:
        exported = self.document.export()
        self.assertNotIn("txt_orphan", exported["objects"])
        self.assertNotIn("backup@example.com", json.dumps(exported, ensure_ascii=False))

    def test_every_serialized_object_is_reachable_from_a_public_page(self) -> None:
        exported = self._redacted().export()
        reloaded = Document.from_dict(exported)  # also proves the export is self-consistent
        reachable = reloaded.reachable_objects(public_only=True)
        self.assertEqual(reachable, set(exported["objects"]))
        roots = [record["root"] for record in exported["pages"].values()]
        self.assertTrue(all(root in exported["objects"] for root in roots))

    def test_redacted_page_no_longer_references_confidential_text(self) -> None:
        exported = self._redacted().export()
        rendered = json.dumps(exported, ensure_ascii=False)
        p1 = extract_page_text(exported, "p1")
        self.assertNotIn("alice@example.com", p1)
        # The shared original is still published for p2/p3, which is expected:
        # redaction is local, so the public copy legitimately keeps its content.
        self.assertIn("alice@example.com", extract_page_text(exported, "p2"))
        self.assertIn("txt_copy", exported["objects"])
        self.assertIn("txt_copy~redacted", exported["objects"])
        # No speech segment quotes the removed address on p1.
        self.assertNotIn(
            "alice at example dot com",
            [s["utterance"] for s in extract_page_speech(exported, "p1")],
        )
        self.assertIn("alice at example dot com", rendered)

    def test_private_objects_can_be_included_explicitly(self) -> None:
        exported = self._redacted().export(public_only=False)
        self.assertIn("txt_secret", exported["objects"])
        self.assertIn("secret", exported["pages"])
        # Orphans are still unreachable, even with every page included.
        self.assertNotIn("txt_orphan", exported["objects"])

    def test_export_does_not_mutate_the_document(self) -> None:
        document = self._redacted()
        before = document.to_dict()
        document.export()
        self.assertEqual(document.to_dict(), before)

    def test_exported_json_round_trips_through_a_file(self) -> None:
        document = self._redacted()
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "out.json")
            document.export_to(path)
            with open(path, "r", encoding="utf-8") as handle:
                on_disk = json.load(handle)
            reloaded = Document.from_dict(on_disk)
        self.assertEqual(extract_page_text(reloaded, "p1"), support.p1_text(copy=support.copy_without("alice@example.com")))
        self.assertEqual(extract_page_text(reloaded, "p2"), support.HEAD_TEXT + support.COPY_TEXT)
        self.assertEqual(extract_page_text(reloaded, "p3"), support.HEAD_TEXT + support.COPY_TEXT)
        with self.assertRaises(Exception):
            extract_page_text(reloaded, "secret")

    def test_reloaded_export_can_be_redacted_again(self) -> None:
        first = self._redacted().export()
        reloaded = Document.from_dict(first)
        start, end = support.COPY_SPANS["alice@example.com"]
        second = reloaded.redact("p2", ("main", "body"), start, end)
        trimmed = support.copy_without("alice@example.com")
        self.assertEqual(extract_page_text(second.document, "p1"), support.p1_text(copy=trimmed))
        self.assertEqual(extract_page_text(second.document, "p2"), support.p2_text(copy=trimmed))
        self.assertEqual(extract_page_text(second.document, "p3"), support.p2_text())

    def test_duplicate_page_paths_still_reach_one_instance(self) -> None:
        exported = self.document.export()
        self.assertEqual(extract_page_text(exported, "p3"), support.HEAD_TEXT + support.COPY_TEXT)
        paths = dict((path, inst) for path, inst in Document.from_dict(exported).iter_instances("p3"))
        self.assertEqual(paths[("duplicate", "body")], "inst_copy")

    def test_export_is_deterministic(self) -> None:
        first = self._redacted().export()
        second = self._redacted().export()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
