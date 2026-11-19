"""Acceptance tests for the diagnostic content and the shipped entry point."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import (
    FIXTURE_DIR,
    REPO_ROOT,
    V1_FIELDS,
    V2_PROMOTED,
    CompatTestCase,
    ac,
    hop,
    load_fixture,
    route,
    version,
)

from approval_compat import check as cli


class DiagnosticContentTest(CompatTestCase):
    """A failing report must say where the permission meaning changed."""

    def setUp(self):
        self.v1 = version("v1", V1_FIELDS)
        self.v2 = version("v2", V2_PROMOTED)

    def test_diagnosis_names_version_field_and_origin(self):
        report = ac.check_route(route("legacy", self.v1, self.v2, hop("relay")))
        self.assertBlocked(report, "legacy_ordinary_promoted")
        text = report.format_text()
        self.assertIn("first permission change: v2", text)
        self.assertIn("v1: field 7", text)
        self.assertIn("display_name", text)
        self.assertIn("approval_decision", text)
        self.assertIn("relay:unknown-preserved", text)

    def test_machine_report_carries_the_same_facts(self):
        report = ac.check_route(route("legacy", self.v1, self.v2, hop("relay")))
        payload = report.as_dict()
        self.assertFalse(payload["ok"])
        finding = payload["violations"][0]
        self.assertEqual(finding["first_permission_change"], "v2")
        self.assertEqual(finding["origin_source"]["version"], "v1")
        self.assertEqual(finding["origin_source"]["field"], 7)
        self.assertEqual(finding["approval_source"]["meaning"], "approval_decision")
        self.assertEqual(finding["taint_path"], ["relay:unknown-preserved"])

    def test_first_permission_change_picks_the_earliest_version(self):
        v1 = version("v1", {4: ("normal", "summary")})
        v2 = version("v2", {4: ("normal", "approval_scope")})
        v3 = version("v3", {4: ("normal", "approval_scope")})
        report = ac.check_route(route("e", v1, v3, hop("relay"), timeline=(v1, v2, v3)))
        self.assertEqual(report.first_permission_change().first_permission_change, "v2")

    def test_accepted_route_still_explains_the_rebuild(self):
        report = ac.check_route(
            route(
                "ok",
                self.v1,
                self.v2,
                hop("core", known_numbers=[7], regenerate=[7], rebuilt_from={7: "trusted"}),
            )
        )
        self.assertAccepted(report)
        self.assertIn("trusted rebuild: core", report.format_text())

    def test_summarize_lists_every_route(self):
        reports = (
            ac.check_route(route("a", self.v1, self.v2, hop("relay"))),
            ac.check_route(
                route(
                    "b",
                    self.v1,
                    self.v2,
                    hop("core", known_numbers=[7], regenerate=[7], rebuilt_from={7: "trusted"}),
                )
            ),
        )
        summary = ac.summarize(reports)
        self.assertFalse(summary["ok"])
        self.assertEqual([r["route"] for r in summary["routes"]], ["a", "b"])
        self.assertEqual(summary["routes"][0]["blocking"], ["legacy_ordinary_promoted"])
        self.assertEqual(summary["routes"][1]["blocking"], [])


class FixtureValidityTest(CompatTestCase):
    def test_fixture_is_well_formed_and_loads(self):
        document = load_fixture()
        self.assertIn("routes", document)
        self.assertGreaterEqual(len(document["routes"]), 5)
        for raw in document["routes"]:
            with self.subTest(route=raw["name"]):
                parsed = ac.route_from_dict(raw)
                self.assertEqual(parsed.name, raw["name"])
                self.assertTrue(parsed.hops)

    def test_fixture_has_both_outcomes(self):
        document = load_fixture()
        outcomes = {
            ac.check_route(ac.route_from_dict(raw)).ok for raw in document["routes"]
        }
        self.assertEqual(outcomes, {True, False})


class CliTest(CompatTestCase):
    def _invoke(self, path: Path):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main([str(path)])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_exits_nonzero_when_a_route_blocks(self):
        code, out, err = self._invoke(FIXTURE_DIR / "routes.json")
        self.assertEqual(code, 1, err)
        self.assertIn("FAIL", out)
        self.assertIn("PASS", out)

    def test_cli_exits_zero_for_accepted_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.json"
            document = {
                "routes": [
                    {
                        "name": "clean",
                        "origin": {
                            "version": "v1",
                            "fields": {"7": {"encoding": "normal", "meaning": "display_name"}},
                        },
                        "destination": {
                            "version": "v2",
                            "fields": {"7": {"encoding": "normal", "meaning": "approval"}},
                        },
                        "hops": [
                            {
                                "hop": "core",
                                "known_numbers": [7],
                                "regenerate": [7],
                                "rebuilt_from": {"7": "trusted"},
                            }
                        ],
                    }
                ]
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            code, out, err = self._invoke(path)
        self.assertEqual(code, 0, out + err)
        self.assertIn("[PASS]", out)

    def test_cli_json_output_is_parseable(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["--json", str(FIXTURE_DIR / "routes.json")])
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["summary"]["ok"])
        self.assertEqual(
            len(payload["reports"]), len(payload["summary"]["routes"])
        )

    def test_cli_rejects_malformed_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            empty = Path(tmp) / "empty.json"
            empty.write_text('{"routes": []}', encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(cli.main([str(bad)]), 2)
                self.assertEqual(cli.main([str(empty)]), 2)
        message = stderr.getvalue()
        self.assertIn("Expecting property name", message)
        self.assertIn("at least one route", message)

    def test_module_entry_point_runs_as_subprocess(self):
        completed = subprocess.run(
            [sys.executable, "-m", "approval_compat.check", str(FIXTURE_DIR / "routes.json")],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("legacy-direct-to-approval", completed.stdout)


class SchemaValidationTest(CompatTestCase):
    def test_duplicate_field_numbers_rejected(self):
        with self.assertRaises(ac.SchemaError):
            ac.MessageVersion(
                version="v1",
                fields=(
                    ac.FieldDef(number=1, encoding="normal", meaning="request_id"),
                    ac.FieldDef(number=1, encoding="normal", meaning="timestamp"),
                ),
            )

    def test_overlapping_hop_buckets_rejected(self):
        with self.assertRaises(ac.SchemaError):
            ac.HopTransform(hop="h", regenerate=[7], drop=[7])

    def test_rebuilt_from_requires_regenerate(self):
        with self.assertRaises(ac.SchemaError):
            ac.HopTransform(hop="h", rebuilt_from={7: "trusted"})

    def test_unknown_rebuild_source_rejected(self):
        with self.assertRaises(ac.SchemaError):
            ac.HopTransform(hop="h", regenerate=[7], rebuilt_from={7: "database"})

    def test_unknown_encoding_rejected(self):
        with self.assertRaises(ac.SchemaError):
            ac.FieldDef(number=7, encoding="varint", meaning="approval")

    def test_missing_hop_requires_at_least_one(self):
        with self.assertRaises(ac.SchemaError):
            route("empty", version("v1", {}), version("v2", {}))

    def test_check_routes_rejects_empty_input(self):
        with self.assertRaises(ac.SchemaError):
            ac.check_routes([])


if __name__ == "__main__":
    unittest.main()
