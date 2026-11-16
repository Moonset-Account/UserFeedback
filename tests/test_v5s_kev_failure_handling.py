"""RED regressions for KEV file-level vs record-level failure handling.

Covers:
- corrupted JSON files produce a payload-level receipt with zero imports;
- well-formed batches still import valid records when individual records fail;
- syntactically correct but calendar-impossible dates are rejected per record
  with a locatable reason;
- boolean and string ransomware flags from historical/convention sources stay
  compatible, while null/numeric/foreign-typed fields follow field semantics
  and never abort later records.
"""

import copy
import unittest

from src.vulnerability.database import ImportStats, VulnerabilityDatabase
from src.vulnerability.sources.kev import parse_kev_record
from tests.test_v5p_kev import KEV


def _seed(database: VulnerabilityDatabase, cve_id: str = "CVE-2025-9601", advisory_id: str = "OSV-KEV-FIX-1") -> None:
    database.import_osv_records([
        {"id": advisory_id, "aliases": [cve_id], "summary": "OSV", "affected": [], "references": []}
    ])


class TestKevFileLevelFailures(unittest.TestCase):
    def test_corrupted_json_returns_payload_error_and_imports_nothing(self):
        stats = VulnerabilityDatabase().import_kev_json("{not valid json")
        self.assertIsInstance(stats, ImportStats)
        self.assertEqual(stats.records_seen, 0)
        self.assertEqual(stats.advisories_imported, 0)
        self.assertEqual(stats.affected_packages_imported, 0)
        self.assertEqual(len(stats.errors), 1)
        self.assertTrue(stats.errors[0].startswith("payload:"))

    def test_corrupted_utf8_bytes_return_payload_error(self):
        stats = VulnerabilityDatabase().import_kev_json(b'{"vulnerabilities": [\xff}')
        self.assertEqual(stats.records_seen, 0)
        self.assertEqual(stats.advisories_imported, 0)
        self.assertTrue(stats.errors[0].startswith("payload:"))

    def test_payload_error_does_not_touch_kev_table(self):
        database = VulnerabilityDatabase()
        _seed(database)
        stats = database.import_kev_json("{broken")
        self.assertEqual(stats.advisories_imported, 0)
        self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM kev_records").fetchone()[0], 0)


class TestKevCalendarDateValidation(unittest.TestCase):
    def test_well_formed_but_impossible_date_is_rejected_per_record(self):
        bad = copy.deepcopy(KEV)
        bad["dateAdded"] = "2025-02-30"  # correct format, not on any calendar
        database = VulnerabilityDatabase()
        _seed(database)
        stats = database.import_kev_json({"vulnerabilities": [bad]})
        self.assertEqual(stats.records_seen, 1)
        self.assertEqual(stats.advisories_imported, 0)
        self.assertEqual(len(stats.errors), 1)
        self.assertIn("record 1", stats.errors[0])
        self.assertIn("dateAdded", stats.errors[0])
        with self.assertRaises(KeyError):
            database.kev_record("CVE-2025-9601")

    def test_impossible_due_date_is_rejected_with_field_name(self):
        bad = copy.deepcopy(KEV)
        bad["dueDate"] = "2025-13-01"
        with self.assertRaises(ValueError) as ctx:
            parse_kev_record(bad)
        self.assertIn("dueDate", str(ctx.exception))

    def test_non_iso_date_format_is_rejected(self):
        bad = copy.deepcopy(KEV)
        bad["dateAdded"] = "01/03/2025"
        with self.assertRaises(ValueError) as ctx:
            parse_kev_record(bad)
        self.assertIn("dateAdded", str(ctx.exception))

    def test_valid_dates_still_parse(self):
        parsed = parse_kev_record(KEV)
        self.assertEqual(parsed["date_added"], "2025-01-03")
        self.assertEqual(parsed["due_date"], "2025-01-24")
        # Leap day on a leap year is a real calendar date.
        leap = copy.deepcopy(KEV)
        leap["dateAdded"] = "2024-02-29"
        self.assertEqual(parse_kev_record(leap)["date_added"], "2024-02-29")

    def test_leap_day_on_common_year_is_rejected(self):
        leap = copy.deepcopy(KEV)
        leap["dateAdded"] = "2025-02-29"
        with self.assertRaises(ValueError):
            parse_kev_record(leap)


class TestKevPartialBatchImport(unittest.TestCase):
    def test_invalid_record_does_not_block_valid_records_in_same_batch(self):
        database = VulnerabilityDatabase()
        _seed(database, "CVE-2025-9601", "OSV-KEV-FIX-1")
        _seed(database, "CVE-2025-9602", "OSV-KEV-FIX-2")

        good = copy.deepcopy(KEV)
        bad_date = copy.deepcopy(KEV)
        bad_date["cveID"] = "CVE-2025-9602"
        bad_date["dateAdded"] = "2025-02-30"

        stats = database.import_kev_json({"vulnerabilities": [bad_date, good]})
        self.assertEqual(stats.records_seen, 2)
        self.assertEqual(stats.advisories_imported, 1)
        self.assertEqual(len(stats.errors), 1)
        self.assertIn("record 1", stats.errors[0])
        self.assertEqual(database.kev_record("CVE-2025-9601")["cve_id"], "CVE-2025-9601")
        with self.assertRaises(KeyError):
            database.kev_record("CVE-2025-9602")

    def test_malformed_record_then_valid_record_both_handled(self):
        database = VulnerabilityDatabase()
        _seed(database)
        stats = database.import_kev_json({"vulnerabilities": ["not-an-object", KEV]})
        self.assertEqual(stats.records_seen, 2)
        self.assertEqual(stats.advisories_imported, 1)
        self.assertIn("record 1", stats.errors[0])


class TestKevRansomwareFlagCompatibility(unittest.TestCase):
    def test_historical_boolean_flags_are_accepted_and_preserved(self):
        for flag in (True, False):
            record = copy.deepcopy(KEV)
            record["knownRansomwareCampaignUse"] = flag
            self.assertIs(parse_kev_record(record)["known_ransomware_campaign_use"], flag)

    def test_convention_string_flags_are_accepted(self):
        for flag in ("Known", "Unknown", "Yes", "No"):
            record = copy.deepcopy(KEV)
            record["knownRansomwareCampaignUse"] = flag
            self.assertEqual(parse_kev_record(record)["known_ransomware_campaign_use"], flag)

    def test_null_flag_is_accepted_and_preserved(self):
        record = copy.deepcopy(KEV)
        record["knownRansomwareCampaignUse"] = None
        self.assertIsNone(parse_kev_record(record)["known_ransomware_campaign_use"])

    def test_missing_flag_defaults_to_none(self):
        record = copy.deepcopy(KEV)
        del record["knownRansomwareCampaignUse"]
        self.assertIsNone(parse_kev_record(record)["known_ransomware_campaign_use"])

    def test_numeric_flag_is_rejected_per_record_without_aborting_batch(self):
        database = VulnerabilityDatabase()
        _seed(database, "CVE-2025-9601", "OSV-KEV-FIX-1")
        _seed(database, "CVE-2025-9602", "OSV-KEV-FIX-2")

        bad_flag = copy.deepcopy(KEV)
        bad_flag["knownRansomwareCampaignUse"] = 1
        good = copy.deepcopy(KEV)
        good["cveID"] = "CVE-2025-9602"

        stats = database.import_kev_json({"vulnerabilities": [bad_flag, good]})
        self.assertEqual(stats.records_seen, 2)
        self.assertEqual(stats.advisories_imported, 1)
        self.assertEqual(len(stats.errors), 1)
        self.assertIn("record 1", stats.errors[0])
        self.assertIn("ransomware", stats.errors[0])
        with self.assertRaises(KeyError):
            database.kev_record("CVE-2025-9601")
        self.assertEqual(database.kev_record("CVE-2025-9602")["cve_id"], "CVE-2025-9602")

    def test_foreign_typed_flag_is_rejected_by_parser(self):
        for flag in (["Known"], {"use": "Known"}, 0):
            record = copy.deepcopy(KEV)
            record["knownRansomwareCampaignUse"] = flag
            with self.assertRaises(ValueError):
                parse_kev_record(record)


class TestKevRequiredFieldTypeHandling(unittest.TestCase):
    def test_null_required_field_is_rejected_with_locatable_field(self):
        record = copy.deepcopy(KEV)
        record["product"] = None
        with self.assertRaises(ValueError) as ctx:
            parse_kev_record(record)
        self.assertIn("product", str(ctx.exception))

    def test_numeric_required_field_is_rejected(self):
        record = copy.deepcopy(KEV)
        record["dateAdded"] = 20250103
        with self.assertRaises(ValueError):
            parse_kev_record(record)

    def test_nested_object_required_field_is_rejected(self):
        record = copy.deepcopy(KEV)
        record["vendorProject"] = {"name": "Vendor"}
        with self.assertRaises(ValueError):
            parse_kev_record(record)

    def test_optional_notes_accepts_any_json_value(self):
        for value in (None, 5, ["a", "b"], {"k": "v"}, "note"):
            record = copy.deepcopy(KEV)
            record["notes"] = value
            self.assertEqual(parse_kev_record(record)["notes"], value)

    def test_bad_record_never_aborts_later_records(self):
        database = VulnerabilityDatabase()
        _seed(database)
        bad = copy.deepcopy(KEV)
        bad["cveID"] = "not-a-cve"
        stats = database.import_kev_json({"vulnerabilities": [bad, KEV, 42]})
        self.assertEqual(stats.records_seen, 3)
        self.assertEqual(stats.advisories_imported, 1)
        self.assertEqual(
            [error.split(":", 1)[0] for error in stats.errors],
            ["record 1", "record 3"],
        )
        self.assertEqual(database.kev_record("CVE-2025-9601")["advisory_id"], "OSV-KEV-FIX-1")


if __name__ == "__main__":
    unittest.main()
