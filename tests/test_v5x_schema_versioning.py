"""RED tests for explicit vulnerability-database schema versioning."""

import os
import sqlite3
import tempfile
import unittest

from src.vulnerability.database import VulnerabilityDatabase


class TestV5xSchemaVersioning(unittest.TestCase):
    def test_new_database_reports_initialized_current_schema(self):
        report = VulnerabilityDatabase().schema_status_report()
        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(report["status"], "initialized")
        self.assertEqual(report["supported_versions"], [4])

    def test_existing_legacy_schema_reports_migration_source(self):
        database = VulnerabilityDatabase()
        database.connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
        database.connection.commit()
        database._create_schema()
        report = database.schema_status_report()
        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(report["status"], "migrated")
        self.assertEqual(report["migrated_from"], 1)

    def test_future_schema_fails_closed_without_overwriting_metadata(self):
        fd, path = tempfile.mkstemp(prefix="coderisktools-v5x-", suffix=".sqlite3")
        os.close(fd)
        try:
            database = VulnerabilityDatabase(path)
            database.connection.execute("UPDATE metadata SET value = '99' WHERE key = 'schema_version'")
            database.connection.commit()
            with self.assertRaises(ValueError):
                VulnerabilityDatabase(path)
            connection = sqlite3.connect(path)
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()[0], "99")
            connection.close()
        finally:
            os.unlink(path)

    def test_schema_report_digest_is_deterministic(self):
        database = VulnerabilityDatabase()
        first = database.schema_status_report()
        second = database.schema_status_report()
        self.assertEqual(first, second)
        self.assertTrue(first["content_digest"].startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
