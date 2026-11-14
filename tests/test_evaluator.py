import unittest

from acl_diag.evaluator import (
    ALLOW,
    DENY,
    UNVERIFIED,
    MODIFY_FILE,
    REPLACE_ENTRY,
    ancestor_dirs,
    evaluate_inventory,
    evaluate_subject,
)
from tests._fixtures import example_inputs, subjects_v2


class EvaluatorScenarioTest(unittest.TestCase):
    def setUp(self):
        self.inventory, self.subjects, self.snapshot = example_inputs()

    def _finding(self, service, path, subject):
        return evaluate_subject(
            service, path, subject, self.subjects, self.snapshot
        )

    # --- Required scenario 1: inherited allow from the old shared drive ---
    def test_billing_inherited_allow_for_group_member(self):
        finding = self._finding(
            "svc_billing", "/srv/fictional/billing/billing.conf", "acc_bob"
        )
        self.assertEqual(finding.results[MODIFY_FILE].decision, ALLOW)
        self.assertEqual(finding.results[REPLACE_ENTRY].decision, ALLOW)
        ace = finding.results[MODIFY_FILE].deciding_ace
        self.assertTrue(ace.inherited)
        self.assertEqual(ace.principal, "grp_legacy_share")
        self.assertEqual(ace.source_path, "/srv/fictional/billing")

    # --- Required scenario 1b: membership change removes the inherited allow ---
    def test_billing_denied_after_membership_removal(self):
        finding = evaluate_subject(
            "svc_billing",
            "/srv/fictional/billing/billing.conf",
            "acc_bob",
            subjects_v2(),
            self.snapshot,
        )
        # bob no longer belongs to grp_legacy_share in subjects_v2.
        self.assertEqual(finding.results[MODIFY_FILE].decision, UNVERIFIED)
        self.assertEqual(finding.results[REPLACE_ENTRY].decision, UNVERIFIED)

    # --- Required scenario 2: explicit deny overrides inherited allow ---
    def test_explicit_deny_overrides_inherited_allow(self):
        finding = self._finding(
            "svc_auth", "/srv/fictional/auth/authd.yaml", "acc_carol"
        )
        modify = finding.results[MODIFY_FILE]
        self.assertEqual(modify.decision, DENY)
        self.assertFalse(modify.deciding_ace.inherited)
        # The inherited allow candidate is still recorded as evidence.
        inherited_principals = {a.principal for a in modify.inherited_candidates}
        self.assertIn("grp_oncall", inherited_principals)
        # No explicit allow/deny exists for replace_entry; only the
        # inherited allow on the parent directory applies.
        self.assertEqual(finding.results[REPLACE_ENTRY].decision, ALLOW)

    # --- Required scenario 3: read-only file but replaceable directory entry ---
    def test_readonly_file_but_directory_entry_replaceable(self):
        finding = self._finding(
            "svc_router", "/srv/fictional/router/routes.conf", "acc_alice"
        )
        self.assertEqual(finding.results[MODIFY_FILE].decision, DENY)
        replace = finding.results[REPLACE_ENTRY]
        self.assertEqual(replace.decision, ALLOW)
        self.assertEqual(replace.target_object, "/srv/fictional/router")
        self.assertFalse(replace.deciding_ace.inherited)
        self.assertEqual(replace.deciding_ace.principal, "acc_alice")

    # --- Required scenario 4: directory permissions not exported ---
    def test_unexported_parent_directory_is_unverified(self):
        finding = self._finding(
            "svc_ledger", "/srv/fictional/ledger/ledger.ini", "acc_bob"
        )
        # The file itself is exported and carries an explicit allow.
        self.assertEqual(finding.results[MODIFY_FILE].decision, ALLOW)
        # The parent directory is exported=false -> replace_entry unverified.
        replace = finding.results[REPLACE_ENTRY]
        self.assertEqual(replace.decision, UNVERIFIED)
        self.assertFalse(replace.object_exported)
        self.assertTrue(
            any("未导出" in reason for reason in replace.reasons)
        )

    # --- Missing-record handling -------------------------------------------
    def test_missing_file_is_unverified_not_deny(self):
        finding = self._finding(
            "svc_ghost", "/srv/fictional/missing/ghost.conf", "acc_bob"
        )
        self.assertEqual(finding.results[MODIFY_FILE].decision, UNVERIFIED)
        self.assertFalse(finding.results[MODIFY_FILE].object_present)

    def test_unresolved_subject_is_unverified(self):
        finding = self._finding(
            "svc_billing",
            "/srv/fictional/billing/billing.conf",
            "acc_unknown_contractor",
        )
        self.assertFalse(finding.subject_resolved)
        self.assertEqual(finding.results[MODIFY_FILE].decision, UNVERIFIED)
        self.assertEqual(finding.results[REPLACE_ENTRY].decision, UNVERIFIED)

    def test_exported_object_without_applicable_ace_is_unverified(self):
        # carol has no path to any router ACE.
        finding = self._finding(
            "svc_router", "/srv/fictional/router/routes.conf", "acc_carol"
        )
        self.assertEqual(finding.results[MODIFY_FILE].decision, UNVERIFIED)
        self.assertEqual(finding.results[REPLACE_ENTRY].decision, UNVERIFIED)

    # --- Priority mechanics ------------------------------------------------
    def test_explicit_allow_beats_inherited_deny(self):
        from acl_diag.models import PermissionSnapshot
        from tests._fixtures import ace

        snapshot = PermissionSnapshot(
            objects={
                "/d": {
                    "type": "directory",
                    "exported": True,
                    "aces": [],
                    "inheritable_aces": [
                        ace(DENY, "acc_bob", "all", False, "/d")
                    ],
                },
                "/d/f": {
                    "type": "file",
                    "exported": True,
                    "aces": [ace(ALLOW, "acc_bob", MODIFY_FILE, False, "/d/f")],
                    "inheritable_aces": [],
                },
            }
        )
        finding = evaluate_subject(
            "svc_x", "/d/f", "acc_bob", self.subjects, snapshot
        )
        self.assertEqual(finding.results[MODIFY_FILE].decision, ALLOW)

    def test_inherited_deny_beats_inherited_allow(self):
        from acl_diag.models import PermissionSnapshot
        from tests._fixtures import ace

        snapshot = PermissionSnapshot(
            objects={
                "/d": {
                    "type": "directory",
                    "exported": True,
                    "aces": [
                        ace(ALLOW, "acc_bob", REPLACE_ENTRY, False, "/d"),
                        ace(DENY, "acc_bob", REPLACE_ENTRY, False, "/d"),
                    ],
                    "inheritable_aces": [
                        ace(ALLOW, "acc_bob", "all", False, "/d"),
                        ace(DENY, "acc_bob", MODIFY_FILE, False, "/d"),
                    ],
                },
                "/d/f": {
                    "type": "file",
                    "exported": True,
                    "aces": [],
                    "inheritable_aces": [],
                },
            }
        )
        finding = evaluate_subject(
            "svc_x", "/d/f", "acc_bob", self.subjects, snapshot
        )
        # On the file, both entries are inherited; deny wins over allow.
        self.assertEqual(finding.results[MODIFY_FILE].decision, DENY)
        # On the directory itself the deny/allow pair are explicit entries;
        # explicit deny wins there too.
        self.assertEqual(finding.results[REPLACE_ENTRY].decision, DENY)


class InventoryLevelTest(unittest.TestCase):
    def test_inventory_covers_ordinary_accounts_and_skips_privileged(self):
        inventory, subjects, snapshot = example_inputs()
        findings = evaluate_inventory(inventory, subjects, snapshot)
        pairs = {(f.service, f.subject_id) for f in findings}
        # Privileged account must not appear in the ordinary-account audit.
        self.assertNotIn(("svc_billing", "acc_rootops"), pairs)
        self.assertIn(("svc_billing", "acc_bob"), pairs)
        self.assertTrue(all(not f.privileged for f in findings))

    def test_ancestor_chain_order_nearest_first(self):
        chain = ancestor_dirs("/srv/fictional/billing/billing.conf")
        self.assertEqual(
            chain,
            ["/srv/fictional/billing", "/srv/fictional", "/srv", "/"],
        )


if __name__ == "__main__":
    unittest.main()
