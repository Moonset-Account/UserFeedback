"""RED tests for V3b OSV payload import, migration, and quality metrics."""

import json
import sqlite3
import tempfile
import unittest

from src.vulnerability.database import VulnerabilityDatabase


RECORD = {
    "schema_version": "1.4.0",
    "id": "OSV-2025-IMPORT",
    "aliases": ["CVE-2025-1111"],
    "summary": "Synthetic import fixture",
    "details": "Used only for local tests.",
    "published": "2025-01-01T00:00:00Z",
    "modified": "2025-01-02T00:00:00Z",
    "affected": [{
        "package": {"ecosystem": "PyPI", "name": "demo", "purl": "pkg:pypi/demo"},
        "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.0.0"}]}],
    }],
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
    "database_specific": {"severity": "HIGH", "source": "synthetic"},
    "references": [{"type": "WEB", "url": "https://example.invalid/osv"}],
}


class TestV3bImport(unittest.TestCase):
    def test_imports_single_record_and_vulns_batch_payload(self):
        database = VulnerabilityDatabase(":memory:")
        single = database.import_osv_json(json.dumps(RECORD))
        batch = database.import_osv_json({"vulns": [RECORD]})
        self.assertEqual((single.records_seen, single.advisories_imported), (1, 1))
        self.assertEqual((batch.records_seen, batch.advisories_imported), (1, 1))
        self.assertEqual(database.advisory_count(), 1)
        metadata = database.advisory_metadata("OSV-2025-IMPORT")
        self.assertEqual(metadata["schema_version"], "1.4.0")
        self.assertEqual(metadata["database_specific"]["severity"], "HIGH")
        self.assertEqual(metadata["severity"][0]["type"], "CVSS_V3")

    def test_list_payload_continues_after_malformed_record(self):
        database = VulnerabilityDatabase(":memory:")
        stats = database.import_osv_json([RECORD, {"aliases": ["CVE-BROKEN"]}])
        self.assertEqual(stats.records_seen, 2)
        self.assertEqual(stats.advisories_imported, 1)
        self.assertEqual(len(stats.errors), 1)
        self.assertEqual(database.advisory_count(), 1)
        self.assertEqual(database.affected_package_count(), 1)

    def test_reimport_is_idempotent_and_reports_quality_metrics(self):
        database = VulnerabilityDatabase(":memory:")
        database.import_osv_records([RECORD])
        database.import_osv_records([RECORD])
        database.record_snapshot("osv-2025-01-02", "sha256:fixture")
        self.assertEqual(database.advisory_count(), 1)
        metrics = database.quality_metrics()
        self.assertEqual(metrics["advisories"], 1)
        self.assertEqual(metrics["affected_packages"], 1)
        self.assertEqual(metrics["withdrawn_advisories"], 0)
        self.assertEqual(metrics["schema_version"], "2")
        self.assertEqual(metrics["snapshot_id"], "osv-2025-01-02")
        self.assertEqual(metrics["source_digest"], "sha256:fixture")

    def test_invalid_json_is_structured_import_error(self):
        database = VulnerabilityDatabase(":memory:")
        stats = database.import_osv_json("{not-json")
        self.assertEqual(stats.records_seen, 0)
        self.assertEqual(stats.advisories_imported, 0)
        self.assertEqual(len(stats.errors), 1)
        self.assertEqual(database.advisory_count(), 0)

    def test_v3a_database_schema_is_migrated_without_data_loss(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as handle:
            connection = sqlite3.connect(handle.name)
            connection.executescript("""
                CREATE TABLE advisories (
                    id TEXT PRIMARY KEY, aliases_json TEXT NOT NULL, summary TEXT,
                    details TEXT, published TEXT, modified TEXT, withdrawn TEXT,
                    source TEXT NOT NULL
                );
                INSERT INTO advisories VALUES
                    ('OSV-OLD', '[]', 'old', 'old', NULL, NULL, NULL, 'osv');
            """)
            connection.commit()
            connection.close()
            database = VulnerabilityDatabase(handle.name)
            self.assertEqual(database.advisory_count(), 1)
            self.assertEqual(database.advisory_metadata("OSV-OLD")["id"], "OSV-OLD")
            self.assertEqual(database.integrity_check(), "ok")


if __name__ == "__main__":
    unittest.main()
