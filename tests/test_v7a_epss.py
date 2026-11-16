"""RED tests for strict offline FIRST EPSS enrichment."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase
from src.vulnerability.sources.epss import parse_epss_record

EPSS = {"cve": "CVE-2026-9701", "epss": "0.42", "percentile": "0.81", "date": "2026-01-07"}
OSV = {"id": "OSV-EPSS-1", "aliases": ["CVE-2026-9701"], "summary": "EPSS", "affected": [], "references": []}


class TestV7aEpss(unittest.TestCase):
    def test_parser_normalizes_score_percentile_and_date(self):
        parsed = parse_epss_record(EPSS)
        self.assertEqual(parsed["cve_id"], "CVE-2026-9701")
        self.assertEqual(parsed["score"], 0.42)
        self.assertEqual(parsed["percentile"], 0.81)
        self.assertEqual(parsed["date"], "2026-01-07")

    def test_import_requires_exact_advisory_and_readback_is_provenanced(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([OSV])
        stats = database.import_epss_json({"data": [EPSS]})
        self.assertEqual(stats.advisories_imported, 1)
        record = database.epss_record("CVE-2026-9701")
        self.assertEqual(record["advisory_id"], "OSV-EPSS-1")
        self.assertEqual(record["source"], "first-epss")
        self.assertEqual(record["score"], 0.42)
        report = database.exploitation_intelligence_report("CVE-2026-9701")
        self.assertEqual(report["epss"]["percentile"], 0.81)
        self.assertTrue(report["content_digest"].startswith("sha256:"))

    def test_parser_rejects_invalid_probability_or_date(self):
        for field, value in (("epss", "1.1"), ("percentile", -0.1), ("date", "2026-02-30")):
            record: dict[str, object] = dict(EPSS)
            record[field] = value
            with self.assertRaises(ValueError):
                parse_epss_record(record)

    def test_unknown_cve_is_rejected_without_record(self):
        database = VulnerabilityDatabase()
        stats = database.import_epss_json({"data": [EPSS]})
        self.assertEqual(stats.advisories_imported, 0)
        with self.assertRaises(KeyError):
            database.epss_record("CVE-2026-9701")


if __name__ == "__main__":
    unittest.main()
