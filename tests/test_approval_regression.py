"""Regression tests for SEC-VULN-043 approval object binding."""

import unittest

from approval_core import (
    ApprovalEvaluator,
    ApprovalStore,
    ExportService,
    OP_BATCH_EXPORT,
    Operation,
    ROLE_AUDITOR,
    ROLE_DATA_OWNER,
    RULE_ATOMIC_REVISION_MATCH,
    RULE_BINDING_MATCH,
    RULE_DECISION_APPROVE,
    RULE_DISTINCT_SUBJECTS,
    RULE_NOT_EXPIRED,
    RULE_ROLE_COVERAGE,
    ResourceRegistry,
    canonical_params,
    normalize_params,
)
from identity_stub import IdentityStub
from scenarios import (
    BASE_PARAMS,
    EXPIRES_AT,
    NOW,
    TENANT_A,
    approve,
    build_env,
    run_all,
)


def _rule(case, rule_id):
    for rule in case.rules_triggered:
        if rule["rule"] == rule_id:
            return rule
    raise AssertionError(f"rule {rule_id} not reported in {case.case_id}")


class ScenarioSuiteTest(unittest.TestCase):
    """End-to-end expectations for the deterministic scenario suite."""

    @classmethod
    def setUpClass(cls):
        cls.results = {r.case_id: r for r in run_all()}

    def test_seven_cases_present(self):
        self.assertEqual(len(self.results), 7)

    def test_all_cases_match_expected(self):
        for case in self.results.values():
            self.assertTrue(
                case.ok,
                f"{case.case_id}: expected {case.expected}, "
                f"got {case.decision}")

    def test_expected_decision_matrix(self):
        expected = {
            "S1_normal_dual_approval": "approved",
            "S2_same_subject_both_roles": "rejected",
            "S3_cross_tenant_same_name": "rejected",
            "S4_same_revision_different_resource": "rejected",
            "S5_param_order_semantic_same": "approved",
            "S6_param_content_changed": "rejected",
            "S7_revision_changed_before_commit": "rejected",
        }
        for case_id, decision in expected.items():
            self.assertEqual(self.results[case_id].decision, decision,
                             f"{case_id} decision mismatch")

    def test_rejection_produces_no_export_record(self):
        for case in self.results.values():
            if case.decision == "rejected":
                self.assertIsNone(case.export_record, case.case_id)
                self.assertEqual(case.export_count, 0, case.case_id)

    def test_approval_reports_records_and_rules(self):
        case = self.results["S1_normal_dual_approval"]
        self.assertEqual(
            [a["record_id"] for a in case.approvals_used],
            ["appr-s1-owner", "appr-s1-auditor"])
        roles = sorted(a["role"] for a in case.approvals_used)
        self.assertEqual(roles, [ROLE_AUDITOR, ROLE_DATA_OWNER])
        self.assertTrue(
            all(r["status"] == "pass" for r in case.rules_triggered),
            "all six rules must pass for an approved export")
        self.assertIsNotNone(case.export_record)
        self.assertEqual(
            case.export_record["approval_ids"],
            ["appr-s1-owner", "appr-s1-auditor"])

    def test_same_subject_both_roles_fails_distinct_rule(self):
        case = self.results["S2_same_subject_both_roles"]
        self.assertEqual(_rule(case, RULE_ROLE_COVERAGE)["status"], "pass")
        self.assertEqual(_rule(case, RULE_DISTINCT_SUBJECTS)["status"], "fail")
        self.assertEqual(case.approvals_used, [])

    def test_cross_tenant_same_name_fails_binding(self):
        case = self.results["S3_cross_tenant_same_name"]
        self.assertEqual(_rule(case, RULE_BINDING_MATCH)["status"], "fail")
        self.assertEqual(case.approvals_used, [])

    def test_same_revision_different_resource_fails_binding(self):
        case = self.results["S4_same_revision_different_resource"]
        self.assertEqual(_rule(case, RULE_BINDING_MATCH)["status"], "fail")

    def test_param_content_change_fails_binding(self):
        case = self.results["S6_param_content_changed"]
        self.assertEqual(_rule(case, RULE_BINDING_MATCH)["status"], "fail")

    def test_param_order_change_still_approved(self):
        case = self.results["S5_param_order_semantic_same"]
        self.assertEqual(_rule(case, RULE_BINDING_MATCH)["status"], "pass")
        self.assertEqual(case.decision, "approved")

    def test_revision_change_before_commit_fails_atomic_rule(self):
        case = self.results["S7_revision_changed_before_commit"]
        # Validation rules all pass; only the atomic commit check fails.
        self.assertEqual(_rule(case, RULE_DISTINCT_SUBJECTS)["status"], "pass")
        atomic = _rule(case, RULE_ATOMIC_REVISION_MATCH)
        self.assertEqual(atomic["status"], "fail")
        self.assertIn("7 != live revision 8", atomic["detail"])
        self.assertEqual(case.decision, "rejected")
        self.assertIsNone(case.export_record)


class NormalizationTest(unittest.TestCase):
    def test_dict_key_order_insensitive(self):
        a = {"format": "csv", "tables": ["alpha", "beta"]}
        b = {"tables": ["alpha", "beta"], "format": "csv"}
        self.assertEqual(canonical_params(a), canonical_params(b))

    def test_list_order_insensitive(self):
        a = {"tables": ["alpha", "beta"]}
        b = {"tables": ["beta", "alpha"]}
        self.assertEqual(canonical_params(a), canonical_params(b))

    def test_nested_order_insensitive(self):
        a = {"opts": {"gzip": True, "cols": ["c1", "c2"]}, "format": "csv"}
        b = {"format": "csv", "opts": {"cols": ["c2", "c1"], "gzip": True}}
        self.assertEqual(canonical_params(a), canonical_params(b))

    def test_content_change_detected(self):
        a = {"format": "csv", "tables": ["alpha", "beta"]}
        b = {"format": "csv", "tables": ["alpha", "gamma"]}
        self.assertNotEqual(canonical_params(a), canonical_params(b))

    def test_scalar_passthrough(self):
        self.assertEqual(normalize_params(42), 42)
        self.assertEqual(normalize_params("x"), "x")


class EvaluatorUnitTest(unittest.TestCase):
    def _op(self, revision=7, params=None):
        return Operation(TENANT_A, "res-1", revision, OP_BATCH_EXPORT,
                         params if params is not None else dict(BASE_PARAMS))

    def test_expired_approval_rejected(self):
        ids, registry, store, service = build_env()
        registry.register(TENANT_A, "res-1", 7)
        approve(ids, store, "appr-x-owner", "alice", ROLE_DATA_OWNER,
                TENANT_A, "res-1", 7, BASE_PARAMS, expires_at=NOW - 1)
        approve(ids, store, "appr-x-auditor", "bob", ROLE_AUDITOR,
                TENANT_A, "res-1", 7, BASE_PARAMS, expires_at=NOW - 1)
        outcome = service.execute(self._op(), NOW)
        self.assertFalse(outcome.approved)
        statuses = {r.rule: r.status for r in outcome.evaluation.rules}
        self.assertEqual(statuses[RULE_NOT_EXPIRED], "fail")
        self.assertEqual(len(service.exports), 0)

    def test_one_expired_approval_breaks_role_coverage(self):
        ids, registry, store, service = build_env()
        registry.register(TENANT_A, "res-1", 7)
        approve(ids, store, "appr-x-owner", "alice", ROLE_DATA_OWNER,
                TENANT_A, "res-1", 7, BASE_PARAMS, expires_at=NOW - 1)
        approve(ids, store, "appr-x-auditor", "bob", ROLE_AUDITOR,
                TENANT_A, "res-1", 7, BASE_PARAMS)
        outcome = service.execute(self._op(), NOW)
        self.assertFalse(outcome.approved)
        statuses = {r.rule: r.status for r in outcome.evaluation.rules}
        self.assertEqual(statuses[RULE_NOT_EXPIRED], "pass")
        self.assertEqual(statuses[RULE_ROLE_COVERAGE], "fail")
        self.assertEqual(len(service.exports), 0)

    def test_expiry_boundary_is_exclusive(self):
        ids, registry, store, service = build_env()
        registry.register(TENANT_A, "res-1", 7)
        approve(ids, store, "appr-x-owner", "alice", ROLE_DATA_OWNER,
                TENANT_A, "res-1", 7, BASE_PARAMS, expires_at=NOW)
        approve(ids, store, "appr-x-auditor", "bob", ROLE_AUDITOR,
                TENANT_A, "res-1", 7, BASE_PARAMS, expires_at=NOW)
        outcome = service.execute(self._op(), NOW)
        self.assertFalse(outcome.approved)
        self.assertEqual(len(service.exports), 0)

    def test_reject_decision_not_counted(self):
        ids, registry, store, service = build_env()
        registry.register(TENANT_A, "res-1", 7)
        approve(ids, store, "appr-x-owner", "alice", ROLE_DATA_OWNER,
                TENANT_A, "res-1", 7, BASE_PARAMS, decision="reject")
        approve(ids, store, "appr-x-auditor", "bob", ROLE_AUDITOR,
                TENANT_A, "res-1", 7, BASE_PARAMS, decision="reject")
        outcome = service.execute(self._op(), NOW)
        self.assertFalse(outcome.approved)
        statuses = {r.rule: r.status for r in outcome.evaluation.rules}
        self.assertEqual(statuses[RULE_DECISION_APPROVE], "fail")
        self.assertEqual(len(service.exports), 0)

    def test_one_reject_decision_breaks_role_coverage(self):
        ids, registry, store, service = build_env()
        registry.register(TENANT_A, "res-1", 7)
        approve(ids, store, "appr-x-owner", "alice", ROLE_DATA_OWNER,
                TENANT_A, "res-1", 7, BASE_PARAMS, decision="reject")
        approve(ids, store, "appr-x-auditor", "bob", ROLE_AUDITOR,
                TENANT_A, "res-1", 7, BASE_PARAMS)
        outcome = service.execute(self._op(), NOW)
        self.assertFalse(outcome.approved)
        statuses = {r.rule: r.status for r in outcome.evaluation.rules}
        self.assertEqual(statuses[RULE_ROLE_COVERAGE], "fail")

    def test_missing_role_rejected(self):
        ids, registry, store, service = build_env()
        registry.register(TENANT_A, "res-1", 7)
        approve(ids, store, "appr-x-owner", "alice", ROLE_DATA_OWNER,
                TENANT_A, "res-1", 7, BASE_PARAMS)
        outcome = service.execute(self._op(), NOW)
        self.assertFalse(outcome.approved)
        statuses = {r.rule: r.status for r in outcome.evaluation.rules}
        self.assertEqual(statuses[RULE_ROLE_COVERAGE], "fail")
        self.assertEqual(statuses[RULE_DISTINCT_SUBJECTS], "skip")

    def test_stale_revision_in_operation_rejected(self):
        ids, registry, store, service = build_env()
        registry.register(TENANT_A, "res-1", 8)
        approve(ids, store, "appr-x-owner", "alice", ROLE_DATA_OWNER,
                TENANT_A, "res-1", 7, BASE_PARAMS)
        approve(ids, store, "appr-x-auditor", "bob", ROLE_AUDITOR,
                TENANT_A, "res-1", 7, BASE_PARAMS)
        # Approvals bound to rev 7; request also says rev 7, but the live
        # resource is already at rev 8.
        outcome = service.execute(self._op(revision=7), NOW)
        self.assertFalse(outcome.approved)
        statuses = {r.rule: r.status for r in outcome.evaluation.rules}
        self.assertEqual(statuses[RULE_ATOMIC_REVISION_MATCH], "fail")
        self.assertEqual(len(service.exports), 0)


class IdentityStubTest(unittest.TestCase):
    def test_unknown_subject_rejected(self):
        ids = IdentityStub()
        with self.assertRaises(PermissionError):
            ids.issue_approval(
                "ghost", ROLE_DATA_OWNER, record_id="appr-g",
                tenant=TENANT_A, resource_id="res-1", resource_revision=1,
                operation=OP_BATCH_EXPORT, params={}, decision="approve",
                expires_at=EXPIRES_AT)

    def test_role_not_held_rejected(self):
        ids = IdentityStub()
        ids.register("alice", [ROLE_DATA_OWNER], TENANT_A)
        with self.assertRaises(PermissionError):
            ids.issue_approval(
                "alice", ROLE_AUDITOR, record_id="appr-g",
                tenant=TENANT_A, resource_id="res-1", resource_revision=1,
                operation=OP_BATCH_EXPORT, params={}, decision="approve",
                expires_at=EXPIRES_AT)

    def test_cross_tenant_issuance_rejected(self):
        ids = IdentityStub()
        ids.register("alice", [ROLE_DATA_OWNER], TENANT_A)
        with self.assertRaises(PermissionError):
            ids.issue_approval(
                "alice", ROLE_DATA_OWNER, record_id="appr-g",
                tenant="tenant-b", resource_id="res-1", resource_revision=1,
                operation=OP_BATCH_EXPORT, params={}, decision="approve",
                expires_at=EXPIRES_AT)


class CommitAtomicityTest(unittest.TestCase):
    def test_export_ids_deterministic(self):
        ids, registry, store, service = build_env()
        registry.register(TENANT_A, "res-1", 7)
        approve(ids, store, "appr-a", "alice", ROLE_DATA_OWNER,
                TENANT_A, "res-1", 7, BASE_PARAMS)
        approve(ids, store, "appr-b", "bob", ROLE_AUDITOR,
                TENANT_A, "res-1", 7, BASE_PARAMS)
        op = Operation(TENANT_A, "res-1", 7, OP_BATCH_EXPORT, dict(BASE_PARAMS))
        outcome = service.execute(op, NOW)
        self.assertTrue(outcome.approved)
        self.assertEqual(outcome.export_record.export_id, "exp-0001")
        self.assertEqual(registry.revision_of(TENANT_A, "res-1"), 7)

    def test_missing_resource_rejected_at_commit(self):
        ids, registry, store, service = build_env()
        registry.register(TENANT_A, "res-1", 7)
        approve(ids, store, "appr-a", "alice", ROLE_DATA_OWNER,
                TENANT_A, "res-1", 7, BASE_PARAMS)
        approve(ids, store, "appr-b", "bob", ROLE_AUDITOR,
                TENANT_A, "res-1", 7, BASE_PARAMS)
        op = Operation(TENANT_A, "res-1", 7, OP_BATCH_EXPORT, dict(BASE_PARAMS))
        # Resource deleted between validation and commit.
        outcome = service.execute(
            op, NOW,
            pre_commit=lambda: registry._revisions.pop((TENANT_A, "res-1")))
        self.assertFalse(outcome.approved)
        self.assertEqual(len(service.exports), 0)


if __name__ == "__main__":
    unittest.main()
