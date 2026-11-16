"""RED tests for the V3a local OSV SQLite database and matcher."""

import tempfile
import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.models import Component


OSV_RANGE_RECORD = {
    "id": "OSV-2024-REQUESTS",
    "aliases": ["CVE-2024-9999"],
    "summary": "Synthetic requests advisory fixture",
    "details": "Synthetic data for offline contract tests.",
    "published": "2024-01-01T00:00:00Z",
    "modified": "2024-01-02T00:00:00Z",
    "affected": [{
        "package": {"ecosystem": "PyPI", "name": "requests", "purl": "pkg:pypi/requests"},
        "ranges": [{
            "type": "ECOSYSTEM",
            "events": [{"introduced": "0"}, {"fixed": "2.32.0"}],
        }],
    }],
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
    "references": [{"type": "WEB", "url": "https://example.invalid/advisory"}],
}

WITHDRAWN_RECORD = {
    "id": "OSV-2024-WITHDRAWN",
    "aliases": ["CVE-2024-0001"],
    "withdrawn": "2024-02-01T00:00:00Z",
    "affected": [{
        "package": {"ecosystem": "PyPI", "name": "requests"},
        "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "99.0.0"}]}],
    }],
}


class TestV3aDatabase(unittest.TestCase):
    def test_schema_import_and_integrity_check(self):
        database = VulnerabilityDatabase(":memory:")
        stats = database.import_osv_records([OSV_RANGE_RECORD])
        self.assertEqual(stats.records_seen, 1)
        self.assertEqual(stats.advisories_imported, 1)
        self.assertEqual(database.integrity_check(), "ok")
        self.assertTrue(database.has_table("advisories"))
        self.assertTrue(database.has_table("affected_ranges"))

    def test_ecosystem_range_match_returns_explainable_vulnerability(self):
        database = VulnerabilityDatabase(":memory:")
        database.import_osv_records([OSV_RANGE_RECORD])
        component = Component(
            ecosystem="pypi", name="requests", version="2.31.0",
            purl="pkg:pypi/requests@2.31.0", manifest_path="requirements.txt",
            exact_version=True,
        )
        matches = database.match_component(component)
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match.status, "affected")
        self.assertEqual(match.method, "ecosystem-range")
        self.assertEqual(match.confidence, "high")
        self.assertEqual(match.fixed_versions, ("2.32.0",))
        self.assertIn("2.32.0", match.explanation)
        self.assertTrue(match.fingerprint.startswith("sha256:"))

    def test_exact_version_match_is_reported_when_no_range_exists(self):
        record = {
            "id": "OSV-2024-EXACT",
            "affected": [{
                "package": {"ecosystem": "PyPI", "name": "urllib3"},
                "versions": ["2.2.1"],
            }],
        }
        database = VulnerabilityDatabase(":memory:")
        database.import_osv_records([record])
        component = Component(
            ecosystem="pypi", name="urllib3", version="2.2.1",
            purl="pkg:pypi/urllib3@2.2.1", exact_version=True,
        )
        match = database.match_component(component)[0]
        self.assertEqual(match.method, "exact-version")

    def test_fixed_version_is_not_reported_as_affected(self):
        database = VulnerabilityDatabase(":memory:")
        database.import_osv_records([OSV_RANGE_RECORD])
        component = Component(
            ecosystem="pypi", name="requests", version="2.32.0",
            purl="pkg:pypi/requests@2.32.0", exact_version=True,
        )
        self.assertEqual(database.match_component(component), [])

    def test_withdrawn_advisory_is_filtered_by_default(self):
        database = VulnerabilityDatabase(":memory:")
        database.import_osv_records([WITHDRAWN_RECORD])
        component = Component(
            ecosystem="pypi", name="requests", version="2.31.0",
            purl="pkg:pypi/requests@2.31.0", exact_version=True,
        )
        self.assertEqual(database.match_component(component), [])

    def test_explain_match_round_trips_by_fingerprint(self):
        database = VulnerabilityDatabase(":memory:")
        database.import_osv_records([OSV_RANGE_RECORD])
        component = Component(
            ecosystem="pypi", name="requests", version="2.31.0",
            purl="pkg:pypi/requests@2.31.0", manifest_path="requirements.txt",
            exact_version=True,
        )
        match = database.match_component(component)[0]
        explanation = database.explain_match(match.fingerprint)
        self.assertEqual(explanation["advisory_id"], "OSV-2024-REQUESTS")
        self.assertEqual(explanation["component_purl"], "pkg:pypi/requests@2.31.0")
        self.assertEqual(explanation["source"], "osv")

    def test_database_can_be_reopened_from_a_file(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as handle:
            database = VulnerabilityDatabase(handle.name)
            database.import_osv_records([OSV_RANGE_RECORD])
            database.close()
            reopened = VulnerabilityDatabase(handle.name)
            self.assertEqual(reopened.integrity_check(), "ok")
            self.assertEqual(reopened.advisory_count(), 1)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
