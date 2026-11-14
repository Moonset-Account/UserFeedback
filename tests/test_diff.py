import unittest

from acl_diag.evaluator import ALLOW, DENY, UNVERIFIED, MODIFY_FILE, REPLACE_ENTRY, evaluate_inventory
from acl_diag.diff import build_diff
from tests._fixtures import example_inputs


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.inventory, self.subjects, self.snapshot = example_inputs()
        self.findings = evaluate_inventory(self.inventory, self.subjects, self.snapshot)
        self.rows = build_diff(self.inventory, self.findings)

    def _row(self, service, subject, action):
        matches = [
            r for r in self.rows
            if r["service"] == service
            and r["subject_id"] == subject
            and r["action"] == action
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def test_one_row_per_legacy_subject_action_pair(self):
        # inventory carries 8 legacy conclusions
        # (2 billing + 2 auth + 2 router + 2 ledger).
        self.assertEqual(len(self.rows), 8)

    def test_billing_legacy_allow_confirmed_by_new_analysis(self):
        row = self._row("svc_billing", "acc_bob", MODIFY_FILE)
        self.assertEqual(row["legacy_conclusion"], ALLOW)
        self.assertEqual(row["new_conclusion"], ALLOW)
        self.assertFalse(row["changed"])
        self.assertTrue(any("grp_legacy_share" in b for b in row["new_basis"]))

    def test_auth_flips_from_allow_to_deny_with_basis(self):
        row = self._row("svc_auth", "acc_carol", MODIFY_FILE)
        self.assertEqual(row["legacy_conclusion"], ALLOW)
        self.assertEqual(row["new_conclusion"], DENY)
        self.assertTrue(row["changed"])
        self.assertTrue(any("显式" in b for b in row["new_basis"]))

    def test_router_replace_entry_flips_deny_to_allow(self):
        row = self._row("svc_router", "acc_alice", REPLACE_ENTRY)
        self.assertEqual(row["legacy_conclusion"], DENY)
        self.assertEqual(row["new_conclusion"], ALLOW)
        self.assertTrue(row["changed"])

    def test_router_modify_file_stays_deny(self):
        row = self._row("svc_router", "acc_alice", MODIFY_FILE)
        self.assertFalse(row["changed"])
        self.assertEqual(row["new_conclusion"], DENY)

    def test_ledger_replace_entry_becomes_unverified(self):
        row = self._row("svc_ledger", "acc_bob", REPLACE_ENTRY)
        self.assertEqual(row["new_conclusion"], UNVERIFIED)
        self.assertTrue(row["changed"])
        self.assertTrue(any("未导出" in b for b in row["new_basis"]))


if __name__ == "__main__":
    unittest.main()
