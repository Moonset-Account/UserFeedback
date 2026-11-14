"""Input parsing: accepted shape, content identity and rejections."""

import hashlib
import os
import tempfile
import unittest

from parser_regression import LoadError, load_report, parse_report, render_json
from parser_regression.identity import normalize_hash, resolve_identity, summarize

from .helpers import export, fixture_path, record, write_json


class AcceptedExportTests(unittest.TestCase):
    def test_fixture_exports_load_with_expected_group_counts(self):
        baseline = load_report(fixture_path("baseline.json"))
        current = load_report(fixture_path("current.json"))
        self.assertEqual(baseline.build_id, "base-2026-11-01")
        self.assertEqual(current.build_id, "build-2026-11-14")
        self.assertEqual(len(baseline.records), 17)
        self.assertEqual(len(current.records), 20)

    def test_statuses_and_categories_are_normalized(self):
        document = export("b1", [
            record("s", content="c", status=" FAILED ", failure_category=" Parse:Oops "),
        ])
        parsed = parse_report(document, source="inline")
        entry = parsed.records[0]
        self.assertEqual(entry.status, "failed")
        self.assertEqual(entry.failure_category, "parse:oops")
        self.assertEqual(entry.build_id, "b1")

    def test_per_result_build_id_may_repeat_document_build_id(self):
        document = export("b1", [record("s", content="c", build_id="b1")])
        parsed = parse_report(document)
        self.assertEqual(parsed.records[0].build_id, "b1")

    def test_whole_number_float_attempt_is_accepted(self):
        document = export("b1", [record("s", content="c")])
        document["results"][0]["attempt"] = 2.0
        parsed = parse_report(document)
        self.assertEqual(parsed.records[0].attempt, 2)

    def test_load_report_keeps_file_path_and_export_source(self):
        path = write_json(export("b1", [record("s", content="c")],
                                 source="ci-export-note"))
        try:
            parsed = load_report(path)
        finally:
            os.unlink(path)
        self.assertEqual(parsed.path, path)
        self.assertEqual(parsed.source, "ci-export-note")

    def test_load_report_falls_back_to_path_as_source_when_absent(self):
        path = write_json(export("b1", [record("s", content="c")]))
        try:
            parsed = load_report(path)
        finally:
            os.unlink(path)
        self.assertEqual(parsed.source, path)
        self.assertEqual(parsed.path, path)


class RejectedExportTests(unittest.TestCase):
    def assert_rejected(self, document, fragment):
        with self.assertRaises(LoadError) as caught:
            parse_report(document, source="doc.json")
        self.assertIn(fragment, str(caught.exception))

    def test_missing_results(self):
        self.assert_rejected({"build_id": "b1"}, "results")

    def test_empty_results(self):
        self.assert_rejected(export("b1", []), "at least one result")

    def test_blank_build_id(self):
        self.assert_rejected({"build_id": "  ", "results": [record("s", content="c")]},
                             "build_id")

    def test_unknown_top_level_field(self):
        document = export("b1", [record("s", content="c")])
        document["notes"] = "hi"
        self.assert_rejected(document, "unknown top-level")

    def test_unknown_record_field_typo_is_rejected(self):
        document = export("b1", [record("s", content="c")])
        document["results"][0]["statuss"] = "passed"
        self.assert_rejected(document, "unknown field")

    def test_unknown_status(self):
        document = export("b1", [record("s", content="c", status="flaky")])
        self.assert_rejected(document, "status must be one of")

    def test_failed_without_category(self):
        document = export("b1", [record("s", content="c", status="failed")])
        self.assert_rejected(document, "failure_category")

    def test_passed_with_category_is_rejected(self):
        document = export("b1", [
            record("s", content="c", status="passed", failure_category="parse"),
        ])
        self.assert_rejected(document, "only failed results")

    def test_zero_attempt(self):
        document = export("b1", [record("s", content="c", attempt=0)])
        self.assert_rejected(document, "attempt")

    def test_boolean_attempt(self):
        document = export("b1", [record("s", content="c")])
        document["results"][0]["attempt"] = True
        self.assert_rejected(document, "attempt")

    def test_fractional_attempt(self):
        document = export("b1", [record("s", content="c")])
        document["results"][0]["attempt"] = 1.5
        self.assert_rejected(document, "attempt")

    def test_record_build_id_conflict(self):
        document = export("b1", [record("s", content="c", build_id="b2")])
        self.assert_rejected(document, "must equal the document build_id")

    def test_blank_parse_mode(self):
        document = export("b1", [record("s", content="c", parse_mode="  ")])
        self.assert_rejected(document, "parse_mode")

    def test_non_object_result(self):
        self.assert_rejected(export("b1", ["nope"]), "JSON object")

    def test_hash_mismatch_with_content(self):
        document = export("b1", [
            record("s", content="actual", content_hash="sha256:" + "0" * 64),
        ])
        self.assert_rejected(document, "does not match content")

    def test_invalid_json_reports_position(self):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        try:
            handle.write('{"build_id": "b1", "results": [}')
            handle.close()
            with self.assertRaises(LoadError) as caught:
                load_report(handle.name)
        finally:
            os.unlink(handle.name)
        message = str(caught.exception)
        self.assertIn("invalid JSON at line", message)

    def test_missing_file(self):
        with self.assertRaises(LoadError) as caught:
            load_report(os.path.join(os.path.dirname(__file__), "does-not-exist.json"))
        self.assertIn("cannot read file", str(caught.exception))

    def test_non_object_top_level(self):
        self.assert_rejected([], "top-level JSON value must be an object")


class IdentityTests(unittest.TestCase):
    def test_content_hash_identity_is_stable_across_renames(self):
        first, basis_a = resolve_identity("old", "same", None)
        second, basis_b = resolve_identity("new", "same", None)
        self.assertEqual(first, second)
        self.assertEqual(basis_a, "content")
        self.assertEqual(basis_b, "content")
        self.assertEqual(first, "sha256:" + hashlib.sha256(b"same").hexdigest())

    def test_declared_hash_is_preferred_over_content(self):
        digest = hashlib.sha256(b"same").hexdigest()
        token, basis = resolve_identity("s", "same", digest)
        self.assertEqual(basis, "hash")
        self.assertEqual(token, "sha256:" + digest)

    def test_sha256_prefix_and_bare_digest_normalize_identically(self):
        digest = hashlib.sha256(b"x").hexdigest()
        self.assertEqual(normalize_hash(digest), normalize_hash("SHA256:" + digest.upper()))

    def test_legacy_export_falls_back_to_name_basis(self):
        token, basis = resolve_identity("legacy", None, None)
        self.assertEqual(basis, "name")
        self.assertEqual(token, "name:legacy")

    def test_blank_hash_is_rejected(self):
        with self.assertRaises(Exception):
            normalize_hash("   ")

    def test_summary_prefers_explicit_then_content_then_name(self):
        self.assertEqual(summarize("content", "explicit", "name"), "explicit")
        self.assertEqual(summarize("content", None, "name"), "content")
        self.assertEqual(summarize(None, None, "name"), "name")

    def test_summary_collapses_whitespace_and_truncates(self):
        self.assertEqual(summarize("a\n\n b   c", None, "n"), "a b c")
        long_summary = summarize("x" * 200, None, "n")
        self.assertEqual(len(long_summary), 72)
        self.assertTrue(long_summary.endswith("..."))


class RenderJsonTests(unittest.TestCase):
    def test_json_render_is_parseable_and_stable(self):
        import json
        from parser_regression import compare_reports

        baseline = load_report(fixture_path("baseline.json"))
        current = load_report(fixture_path("current.json"))
        first = render_json(compare_reports(baseline, current))
        second = render_json(compare_reports(load_report(fixture_path("baseline.json")),
                                             load_report(fixture_path("current.json"))))
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["current"]["build_id"], "build-2026-11-14")


if __name__ == "__main__":
    unittest.main()
