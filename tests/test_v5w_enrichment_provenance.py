"""RED tests for catalog-level NVD/KEV provenance."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from tests.test_v5m_nvd_enrichment import NVD
from tests.test_v5p_kev import KEV


class TestV5wEnrichmentProvenance(unittest.TestCase):
    def test_nvd_and_kev_imports_create_canonical_provenance_rows(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-PROV-1", "aliases": ["CVE-2025-9601"], "summary": "OSV", "affected": [], "references": []}])
        database.import_nvd_json(NVD)
        database.import_kev_json({"vulnerabilities": [KEV]})
        report = database.enrichment_provenance_report("CVE-2025-9601", sources=("nvd", "kev"))
        self.assertEqual(report["record_count"], 2)
        self.assertEqual([(row["source_id"], row["native_record_id"]) for row in report["records"]], [("kev", "CVE-2025-9601"), ("nvd", "CVE-2025-9601")])
        self.assertTrue(all(row["content_digest"].startswith("sha256:") for row in report["records"]))
        self.assertTrue(all("record_json" not in row for row in report["records"]))

    def test_duplicate_enrichment_import_does_not_create_duplicate_provenance(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([{"id": "OSV-PROV-2", "aliases": ["CVE-2025-9601"], "summary": "OSV", "affected": [], "references": []}])
        database.import_nvd_json(NVD)
        database.import_nvd_json(NVD)
        report = database.enrichment_provenance_report("CVE-2025-9601", sources=("nvd",))
        self.assertEqual(report["record_count"], 1)
        self.assertEqual(report["counts_by_source"], {"nvd": 1})

    def test_provenance_rejects_unknown_source(self):
        with self.assertRaises(ValueError):
            VulnerabilityDatabase().enrichment_provenance_report("CVE-2025-9601", sources=("unknown",))


if __name__ == "__main__":
    unittest.main()
