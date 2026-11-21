"""Tests for the CLI entry point and the zone-file loader."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from authoritative_dns.cli import EXIT_INPUT_ERROR, EXIT_OK, load_zone, run
from authoritative_dns.fixtures import sample_zone
from authoritative_dns.records import InvalidRecord
from authoritative_dns.zone import ZoneError

from .support import LookupAssertions

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
ZONE_FILE = os.path.join(REPO_ROOT, "zones", "example.zone")


class ZoneFileLoaderTests(LookupAssertions, unittest.TestCase):
    def test_fixture_file_matches_the_python_sample_zone(self):
        from_file = load_zone(ZONE_FILE)
        from_code = sample_zone()
        self.assertEqual(from_file.existing_names(), from_code.existing_names())
        self.assertEqual(
            [(rr.name, rr.rtype, rr.value, rr.ttl) for rr in from_file.records()],
            [(rr.name, rr.rtype, rr.value, rr.ttl) for rr in from_code.records()],
        )

    def test_relative_and_absolute_owners(self):
        text = (
            "$ORIGIN example.\n"
            "$TTL 600\n"
            "@      IN A 192.0.2.1\n"
            "www    IN A 192.0.2.10\n"
            "abs.example. IN AAAA 2001:db8::10\n"
            'txt    IN TXT "a" "b"\n'
        )
        with tempfile.NamedTemporaryFile("w", suffix=".zone", delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            zone = load_zone(path)
        finally:
            os.unlink(path)

        self.assert_answer(zone, "example.", "A", ["192.0.2.1"])
        self.assert_answer(zone, "www.example.", "A", ["192.0.2.10"])
        self.assert_answer(zone, "abs.example.", "AAAA", ["2001:db8::10"])
        self.assert_answer(zone, "txt.example.", "TXT", [("a", "b")])
        self.assertEqual(zone.lookup("www.example.", "A").records[0].ttl, 600)

    def test_soa_and_comments_are_skipped(self):
        text = (
            "; leading comment\n"
            "$ORIGIN example.\n"
            "@ IN SOA ns hostmaster 1 3600 600 86400 300 ; trailing\n"
            "@ IN NS ns.example.\n"
            "www IN A 192.0.2.10\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".zone", delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            zone = load_zone(path)
        finally:
            os.unlink(path)
        self.assertEqual(zone.existing_names(),
                         ["example.", "www.example."])
        self.assert_answer(zone, "www.example.", "A", ["192.0.2.10"])

    def test_malformed_zone_file_is_rejected(self):
        text = "$ORIGIN example.\nwww IN A not-an-ip\n"
        with tempfile.NamedTemporaryFile("w", suffix=".zone", delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            with self.assertRaises(InvalidRecord):
                load_zone(path)
        finally:
            os.unlink(path)

    def test_relative_owner_without_origin_is_rejected(self):
        text = "www IN A 192.0.2.10\n"
        with tempfile.NamedTemporaryFile("w", suffix=".zone", delete=False) as fh:
            fh.write(text)
            path = fh.name
        try:
            with self.assertRaises(ZoneError):
                load_zone(path)
        finally:
            os.unlink(path)


class CliTests(unittest.TestCase):
    def _run(self, argv):
        out = io.StringIO()
        code = run(argv, stdout=out)
        return code, out.getvalue()

    def test_text_output_reports_status_and_records(self):
        code, text = self._run(
            ["--zone", ZONE_FILE, "www.example.", "A"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("ANSWER", text)
        self.assertIn("www.example. 300 IN A 192.0.2.10", text)

    def test_referral_is_rendered_with_ns_records(self):
        code, text = self._run(
            ["--zone", ZONE_FILE, "host.child.example.", "TXT"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("REFERRAL", text)
        self.assertIn("NS ns1.child.example.", text)
        self.assertIn("NS ns2.child.example.", text)

    def test_nxdomain_and_nodata_are_distinguished(self):
        code, text = self._run(
            ["--zone", ZONE_FILE,
             "y.b.example.", "A",
             "b.example.", "A"]
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("y.b.example. A -> NXDOMAIN", text)
        self.assertIn("b.example. A -> NODATA", text)

    def test_json_output_is_machine_readable(self):
        code, text = self._run(
            ["--zone", ZONE_FILE, "--json", "y.example.", "A"]
        )
        self.assertEqual(code, EXIT_OK)
        payload = json.loads(text)
        self.assertEqual(payload["status"], "ANSWER")
        self.assertTrue(payload["wildcard"])
        self.assertEqual(payload["source"], "*.example.")
        self.assertEqual(payload["records"][0]["name"], "y.example.")
        self.assertEqual(payload["records"][0]["value"], "192.0.2.99")

    def test_missing_zone_file_returns_input_error(self):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = run(
                ["--zone", "/nonexistent/zone.file", "www.example.", "A"],
                stdout=out,
            )
        self.assertEqual(code, EXIT_INPUT_ERROR)
        self.assertIn("error:", err.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
