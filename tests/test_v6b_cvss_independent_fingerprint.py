"""Acceptance regression: NVD CVSS revisions do not rotate match fingerprints."""

import copy
import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.models import Component
from tests.test_v5m_nvd_enrichment import NVD


OSV = {
    "id": "OSV-V6B-1",
    "aliases": ["CVE-2025-9601"],
    "summary": "fingerprint fixture",
    "affected": [{
        "package": {"ecosystem": "PyPI", "name": "requests"},
        "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "3.0.0"}]}],
    }],
    "references": [],
}


class TestV6bCvssIndependentFingerprint(unittest.TestCase):
    def test_nvd_cvss_revision_does_not_change_real_match_fingerprint(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([OSV])
        component = Component(
            ecosystem="PyPI",
            name="requests",
            version="2.31.0",
            purl="pkg:pypi/requests@2.31.0",
            manifest_path="requirements.txt",
        )
        before = database.match_component(component)[0].fingerprint

        first = copy.deepcopy(NVD)
        first["cve"]["id"] = "CVE-2025-9601"
        database.import_nvd_json(first)
        after_first = database.match_component(component)[0].fingerprint

        changed = copy.deepcopy(first)
        changed["cve"]["lastModified"] = "2026-01-03T00:00:00.000"
        changed["cve"]["metrics"]["cvssMetricV31"][0]["cvssData"]["baseScore"] = 4.2
        database.import_nvd_json(changed)
        after_revision = database.match_component(component)[0].fingerprint

        self.assertEqual(before, after_first)
        self.assertEqual(after_first, after_revision)


if __name__ == "__main__":
    unittest.main()
