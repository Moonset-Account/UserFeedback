import unittest

from acl_diag.evaluator import (
    ALLOW,
    DENY,
    UNVERIFIED,
    MODIFY_FILE,
    REPLACE_ENTRY,
    evaluate_inventory,
)
from acl_diag.remediation import (
    RemediationPlan,
    apply_remediation,
    build_plan,
    remediate,
    reverify,
    snapshot_to_dict,
)
from tests._fixtures import ace, example_inputs


class RemediationTest(unittest.TestCase):
    def setUp(self):
        self.inventory, self.subjects, self.snapshot = example_inputs()
        self.findings = evaluate_inventory(
            self.inventory, self.subjects, self.snapshot
        )
        self.plan, self.fixed, self.recheck = remediate(
            self.inventory, self.subjects, self.snapshot, self.findings
        )

    def test_plan_uses_explicit_deny_for_inherited_allow(self):
        billing_actions = [
            a for a in self.plan.actions
            if a.object_path.startswith("/srv/fictional/billing")
        ]
        self.assertTrue(billing_actions)
        self.assertTrue(all(a.kind == "add_explicit_deny" for a in billing_actions))
        targets = {a.target_subject for a in billing_actions}
        # All three ordinary accounts inherit the shared-drive allow.
        self.assertEqual(
            targets, {"acc_alice", "acc_bob", "acc_carol"}
        )

    def test_plan_removes_explicit_allow_ace(self):
        ledger = [
            a for a in self.plan.actions
            if a.object_path == "/srv/fictional/ledger/ledger.ini"
        ]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0].kind, "remove_ace")
        self.assertEqual(ledger[0].ace.principal, "acc_bob")

    def test_billing_directory_grant_is_narrowed_not_deleted(self):
        # The billing directory's inheritable allow must survive
        # remediation (other children may rely on it); only a targeted
        # explicit deny is added.
        billing_dir = self.fixed.get("/srv/fictional/billing")
        inheritable_principals = {
            a.principal for a in billing_dir["inheritable_aces"]
        }
        self.assertIn("grp_legacy_share", inheritable_principals)
        deny_aces = [
            a for a in billing_dir["aces"]
            if a.ace_type == DENY and not a.inherited
        ]
        self.assertTrue(deny_aces)
        billing_actions = [
            a for a in self.plan.actions
            if a.object_path == "/srv/fictional/billing"
            and a.action == REPLACE_ENTRY
        ]
        self.assertTrue(
            all(a.kind == "add_explicit_deny" for a in billing_actions)
        )

    def test_unexported_parent_is_listed_as_unresolved_not_patched(self):
        unresolved = {
            (u["service"], u["action"]) for u in self.plan.unresolved
        }
        self.assertIn(("svc_ledger", REPLACE_ENTRY), unresolved)

    def test_original_snapshot_is_not_mutated(self):
        original_aces = self.snapshot.get("/srv/fictional/ledger/ledger.ini")["aces"]
        self.assertEqual(len(original_aces), 1)
        self.assertEqual(original_aces[0].ace_type, ALLOW)

    def test_reverification_no_ordinary_account_remains_allowed(self):
        residual = [r for r in self.recheck if r["still_allowed"]]
        self.assertEqual(residual, [])
        # Explicit spot checks.
        def row(service, subject, action):
            rows = [
                r for r in self.recheck
                if r["service"] == service
                and r["subject_id"] == subject
                and r["action"] == action
            ]
            self.assertEqual(len(rows), 1)
            return rows[0]

        self.assertEqual(
            row("svc_billing", "acc_bob", MODIFY_FILE)["post_fix_decision"], DENY
        )
        # The router directory's own explicit allow ACE is removed; with no
        # remaining applicable ACE the path is no longer allowed and falls
        # back to unverified rather than being silently denied.
        self.assertEqual(
            row("svc_router", "acc_alice", REPLACE_ENTRY)["post_fix_decision"],
            UNVERIFIED,
        )
        # Unexported evidence gap stays unverified rather than being faked.
        self.assertEqual(
            row("svc_ledger", "acc_bob", REPLACE_ENTRY)["post_fix_decision"],
            UNVERIFIED,
        )

    def test_fixed_snapshot_round_trips_through_loader(self):
        from acl_diag.evaluator import load_snapshot
        import json
        import tempfile
        import os

        payload = snapshot_to_dict(self.fixed)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fixed.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            reloaded = load_snapshot(path)
            rerun = reverify(self.inventory, self.subjects, reloaded)
            self.assertFalse(any(r["still_allowed"] for r in rerun))

    def test_minimality_no_action_when_already_denied_or_unverified(self):
        # authd.yaml modify_file is already an explicit deny for carol;
        # no remediation action may target it.
        auth_actions = [
            a for a in self.plan.actions
            if a.object_path == "/srv/fictional/auth/authd.yaml"
            and a.action == MODIFY_FILE
        ]
        self.assertEqual(auth_actions, [])

    def test_apply_remove_ace_on_synthetic_snapshot(self):
        from acl_diag.models import PermissionSnapshot

        snapshot = PermissionSnapshot(
            objects={
                "/d/f": {
                    "type": "file",
                    "exported": True,
                    "aces": [ace(ALLOW, "acc_bob", MODIFY_FILE, False, "/d/f")],
                    "inheritable_aces": [],
                }
            }
        )

        class _Fake:
            pass

        from acl_diag.remediation import RemediationAction

        plan = RemediationPlan(
            actions=[
                RemediationAction(
                    kind="remove_ace",
                    object_path="/d/f",
                    action=MODIFY_FILE,
                    target_subject="acc_bob",
                    ace=snapshot.get("/d/f")["aces"][0],
                    rationale="test",
                )
            ]
        )
        fixed = apply_remediation(snapshot, plan)
        self.assertEqual(fixed.get("/d/f")["aces"], [])


if __name__ == "__main__":
    unittest.main()
