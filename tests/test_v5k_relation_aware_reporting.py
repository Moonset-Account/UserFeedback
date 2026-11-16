"""RED tests for relation-aware advisory readback."""

import unittest

from src.vulnerability.database import VulnerabilityDatabase


class TestV5kRelationAwareReporting(unittest.TestCase):
    def test_active_relation_readback_contains_sources_and_alias_evidence(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([
            {"id": "OSV-REL-1", "aliases": ["CVE-2025-9401"], "summary": "one", "affected": [], "references": []},
            {"id": "OSV-REL-2", "aliases": ["CVE-2025-9401"], "summary": "two", "affected": [], "references": []},
        ])
        decision = database.record_merge_decision("merge", ["OSV-REL-1", "OSV-REL-2"], "reviewed", ["fixture:relation"])
        database.apply_merge_decision(decision["decision_id"])
        report = database.advisory_relation_report()
        self.assertEqual(report["relation_count"], 1)
        relation = report["relations"][0]
        self.assertEqual(relation["advisory_ids"], ["OSV-REL-1", "OSV-REL-2"])
        self.assertEqual(relation["evidence_aliases"], ["CVE-2025-9401"])
        self.assertEqual(relation["sources"], ["osv"])
        self.assertTrue(report["content_digest"].startswith("sha256:"))

    def test_inactive_relations_are_hidden_by_default_but_readable(self):
        database = VulnerabilityDatabase()
        database.import_osv_records([
            {"id": "OSV-REL-3", "aliases": ["CVE-2025-9402"], "summary": "one", "affected": [], "references": []},
            {"id": "OSV-REL-4", "aliases": ["CVE-2025-9402"], "summary": "two", "affected": [], "references": []},
        ])
        decision_id = database.record_merge_decision("split", ["OSV-REL-3", "OSV-REL-4"], "reviewed", ["fixture:relation"])["decision_id"]
        database.apply_merge_decision(decision_id)
        database.rollback_merge_decision(decision_id)
        self.assertEqual(database.advisory_relation_report()["relation_count"], 0)
        self.assertEqual(database.advisory_relation_report(active_only=False)["relation_count"], 1)


if __name__ == "__main__":
    unittest.main()
