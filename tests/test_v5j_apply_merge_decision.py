"""RED tests for applying approved merge/split decisions as relations."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase


class TestV5jApplyMergeDecision(unittest.TestCase):
    def setUp(self):
        self.database = VulnerabilityDatabase()
        self.database.import_osv_records([
            {"id": "OSV-APPLY-1", "aliases": ["CVE-2025-9301"], "summary": "one", "affected": [], "references": []},
            {"id": "OSV-APPLY-2", "aliases": ["CVE-2025-9301"], "summary": "two", "affected": [], "references": []},
        ])
        self.decision_id = self.database.record_merge_decision(
            "merge", ["OSV-APPLY-1", "OSV-APPLY-2"], "approved exact alias relation", ["fixture:operator"]
        )["decision_id"]

    def test_applies_valid_decision_without_mutating_advisories(self):
        before = self.database.advisory_metadata("OSV-APPLY-1")
        result = self.database.apply_merge_decision(self.decision_id)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.database.advisory_relation_count(), 1)
        self.assertEqual(self.database.advisory_metadata("OSV-APPLY-1"), before)

    def test_application_is_idempotent_and_rollback_is_reversible(self):
        first = self.database.apply_merge_decision(self.decision_id)
        second = self.database.apply_merge_decision(self.decision_id)
        self.assertEqual(first["relation_id"], second["relation_id"])
        self.assertEqual(second["status"], "already-applied")
        rolled_back = self.database.rollback_merge_decision(self.decision_id)
        self.assertEqual(rolled_back["status"], "rolled-back")
        self.assertEqual(self.database.advisory_relation_count(active_only=True), 0)
        reapplied = self.database.apply_merge_decision(self.decision_id)
        self.assertEqual(reapplied["status"], "applied")

    def test_invalid_decision_is_not_applied(self):
        other = self.database.import_osv_records([
            {"id": "OSV-APPLY-3", "aliases": ["CVE-2025-9303"], "summary": "three", "affected": [], "references": []}
        ])
        decision_id = self.database.record_merge_decision(
            "merge", ["OSV-APPLY-1", "OSV-APPLY-3"], "no shared evidence", ["fixture:operator"]
        )["decision_id"]
        result = self.database.apply_merge_decision(decision_id)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "no_exact_alias_evidence")
        self.assertEqual(self.database.advisory_relation_count(), 0)


if __name__ == "__main__":
    unittest.main()
