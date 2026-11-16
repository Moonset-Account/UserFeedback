"""RED tests for read-only merge-decision validation."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase


class TestV5iMergeDecisionValidation(unittest.TestCase):
    def setUp(self):
        self.database = VulnerabilityDatabase()
        self.database.import_osv_records([
            {"id": "OSV-DECISION-1", "aliases": ["CVE-2025-9201"], "summary": "osv", "affected": [], "references": []},
            {"id": "OSV-DECISION-2", "aliases": ["CVE-2025-9202"], "summary": "other", "affected": [], "references": []},
        ])

    def test_validates_only_exact_alias_evidence(self):
        decision_id = self.database.record_merge_decision(
            "merge", ["OSV-DECISION-1", "OSV-DECISION-2"], "operator reviewed exact evidence", ["fixture:decision"]
 )["decision_id"]
        result = self.database.validate_merge_decision(decision_id)
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["reason"], "no_exact_alias_evidence")
        self.assertEqual(result["evidence_aliases"], [])

    def test_validates_shared_alias_and_exposes_conflicts_read_only(self):
        self.database.import_osv_records([
            {"id": "OSV-DECISION-3", "aliases": ["CVE-2025-9201"], "summary": "different", "affected": [], "references": []}
        ])
        decision_id = self.database.record_merge_decision(
            "merge", ["OSV-DECISION-1", "OSV-DECISION-3"], "operator reviewed exact evidence", ["fixture:decision"]
        )["decision_id"]
        before = self.database.advisory_count()
        result = self.database.validate_merge_decision(decision_id)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["evidence_aliases"], ["CVE-2025-9201"])
        self.assertIn("summary_details", result["conflict_types"])
        self.assertEqual(self.database.advisory_count(), before)

    def test_missing_advisory_makes_decision_stale(self):
        decision_id = "sha256:stale-fixture"
        self.database.connection.execute(
            "INSERT INTO merge_decisions(decision_id, decision_type, advisory_ids_json, reason, provenance_json, rules_version) VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, "split", '["OSV-DECISION-1","OSV-DECISION-MISSING"]', "operator reviewed stale data", '["fixture:decision"]', "v5d-exact-ledger-1"),
        )
        self.database.connection.commit()
        result = self.database.validate_merge_decision(decision_id)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["missing_advisory_ids"], ["OSV-DECISION-MISSING"])


if __name__ == "__main__":
    unittest.main()
