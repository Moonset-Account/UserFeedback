"""RED tests for strict offline GitHub Advisory Database import."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.models import Component
from src.vulnerability.sources.ghsa import parse_ghsa_advisory


GHSA = {
    "ghsa_id": "GHSA-ABCD-EFGH-IJKL",
    "cve_id": "CVE-2025-8001",
    "html_url": "https://github.com/advisories/GHSA-ABCD-EFGH-IJKL",
    "summary": "GHSA fixture summary",
    "description": "GHSA fixture description",
    "severity": "high",
    "published_at": "2025-02-01T00:00:00Z",
    "updated_at": "2025-02-02T00:00:00Z",
    "withdrawn_at": None,
    "identifiers": [{"value": "GHSA-ABCD-EFGH-IJKL", "type": "GHSA"}, {"value": "CVE-2025-8001", "type": "CVE"}],
    "cvss": {"vector_string": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "score": 9.8},
    "vulnerabilities": [{"package": {"ecosystem": "pip", "name": "fixture-pkg"}, "vulnerable_version_range": ">= 1.0, < 2.0", "first_patched_version": {"identifier": "2.0"}}],
    "references": [{"url": "https://example.invalid/ghsa", "type": "ADVISORY"}],
}


class TestV5fGHSAImport(unittest.TestCase):
    def test_parser_preserves_identity_aliases_metadata_and_references(self):
        parsed = parse_ghsa_advisory(GHSA)
        self.assertEqual(parsed["id"], "GHSA-ABCD-EFGH-IJKL")
        self.assertEqual(parsed["aliases"], ["CVE-2025-8001"])
        self.assertEqual(parsed["severity"][0]["type"], "GHSA")
        self.assertEqual(parsed["references"][0]["url"], "https://example.invalid/ghsa")

    def test_database_imports_supported_vulnerable_range_and_source_provenance(self):
        database = VulnerabilityDatabase()
        result = database.import_ghsa_json(GHSA)
        self.assertEqual(result.advisories_imported, 1)
        self.assertEqual(result.affected_packages_imported, 1)
        self.assertEqual(database.advisory_metadata("GHSA-ABCD-EFGH-IJKL")["source"], "github-advisory")
        self.assertEqual(database.source_record_revision_count("github-advisory", "GHSA-ABCD-EFGH-IJKL"), 1)
        matches = database.match_component(Component(ecosystem="pypi", name="fixture-pkg", version="1.5", purl="pkg:pypi/fixture-pkg@1.5"))
        self.assertEqual(matches[0].source, "github-advisory")
        self.assertEqual(matches[0].aliases, ("CVE-2025-8001",))

    def test_unsupported_range_is_retained_without_guessing_active_package_assertion(self):
        database = VulnerabilityDatabase()
        unsupported = dict(GHSA, ghsa_id="GHSA-zzzz-yyyy-xxxx", cve_id=None, identifiers=[], vulnerabilities=[{**GHSA["vulnerabilities"][0], "vulnerable_version_range": "~> 1.2"}])
        database.import_ghsa_json(unsupported)
        self.assertEqual(database.affected_package_count(), 0)
        self.assertEqual(database.advisory_metadata("GHSA-ZZZZ-YYYY-XXXX")["database_specific"]["unsupported_ranges"], ["~> 1.2"])

    def test_parser_rejects_invalid_id_and_missing_required_fields(self):
        with self.assertRaises(ValueError):
            parse_ghsa_advisory({"ghsa_id": "not-ghsa", "summary": "x", "description": "x"})
        with self.assertRaises(ValueError):
            parse_ghsa_advisory({"ghsa_id": "GHSA-ABCD-EFGH-IJKL"})


if __name__ == "__main__":
    unittest.main()
