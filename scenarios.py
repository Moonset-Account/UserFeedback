"""Deterministic scenario suite for SEC-VULN-043.

Each scenario builds a fresh in-memory environment (identity stub,
approval store, resource registry, export service) and executes one
batch-export operation under a fixed logical clock. Results are
deterministic: record ids, export ids and rule outputs are stable
across runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from approval_core import (
    ApprovalEvaluator,
    ApprovalStore,
    DeterministicScheduler,
    ExportService,
    OP_BATCH_EXPORT,
    Operation,
    ROLE_AUDITOR,
    ROLE_DATA_OWNER,
    ResourceRegistry,
)
from identity_stub import IdentityStub

NOW = 1000
EXPIRES_AT = 2000
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
BASE_PARAMS = {"format": "csv", "tables": ["alpha", "beta"]}


def build_env():
    """Fresh environment with a fixed set of principals."""
    ids = IdentityStub()
    ids.register("alice", [ROLE_DATA_OWNER], TENANT_A)
    ids.register("bob", [ROLE_AUDITOR], TENANT_A)
    ids.register("carol", [ROLE_DATA_OWNER, ROLE_AUDITOR], TENANT_A)
    ids.register("dave", [ROLE_DATA_OWNER], TENANT_B)
    ids.register("erin", [ROLE_AUDITOR], TENANT_B)
    registry = ResourceRegistry()
    store = ApprovalStore()
    service = ExportService(ApprovalEvaluator(store), registry)
    return ids, registry, store, service


def approve(ids: IdentityStub, store: ApprovalStore, record_id: str,
            subject: str, role: str, tenant: str, resource_id: str,
            revision: int, params: Any, decision: str = "approve",
            expires_at: int = EXPIRES_AT):
    rec = ids.issue_approval(
        subject, role,
        record_id=record_id, tenant=tenant, resource_id=resource_id,
        resource_revision=revision, operation=OP_BATCH_EXPORT,
        params=params, decision=decision, expires_at=expires_at)
    store.add(rec)
    return rec


@dataclass
class CaseResult:
    case_id: str
    description: str
    expected: str  # "approved" | "rejected"
    decision: str
    approvals_used: List[Dict[str, str]]
    rules_triggered: List[Dict[str, Any]]
    export_record: Optional[Dict[str, Any]]
    export_count: int

    @property
    def ok(self) -> bool:
        if self.decision != self.expected:
            return False
        # Invariant: a rejection must never produce an export record.
        if self.decision == "rejected":
            return self.export_record is None and self.export_count == 0
        return self.export_record is not None and self.export_count == 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "description": self.description,
            "expected": self.expected,
            "decision": self.decision,
            "ok": self.ok,
            "approvals_used": self.approvals_used,
            "rules_triggered": self.rules_triggered,
            "export_record": self.export_record,
            "export_count": self.export_count,
        }


def _case(case_id: str, description: str, expected: str,
          service: ExportService, op: Operation,
          pre_commit: Optional[Callable[[], None]] = None) -> CaseResult:
    outcome = service.execute(op, NOW, pre_commit=pre_commit)
    decision = "approved" if outcome.approved else "rejected"
    return CaseResult(
        case_id=case_id,
        description=description,
        expected=expected,
        decision=decision,
        approvals_used=[
            {"record_id": r.record_id, "approver": r.approver, "role": r.role}
            for r in outcome.evaluation.records_used
        ],
        rules_triggered=[rule.to_dict() for rule in outcome.evaluation.rules],
        export_record=(outcome.export_record.to_dict()
                       if outcome.export_record else None),
        export_count=len(service.exports),
    )


def scenario_s1() -> CaseResult:
    ids, registry, store, service = build_env()
    registry.register(TENANT_A, "res-1", 7)
    approve(ids, store, "appr-s1-owner", "alice", ROLE_DATA_OWNER,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    approve(ids, store, "appr-s1-auditor", "bob", ROLE_AUDITOR,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    op = Operation(TENANT_A, "res-1", 7, OP_BATCH_EXPORT, dict(BASE_PARAMS))
    return _case("S1_normal_dual_approval",
                 "two distinct subjects approve in their own roles; "
                 "binding and live revision match",
                 "approved", service, op)


def scenario_s2() -> CaseResult:
    ids, registry, store, service = build_env()
    registry.register(TENANT_A, "res-1", 7)
    approve(ids, store, "appr-s2-owner", "carol", ROLE_DATA_OWNER,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    approve(ids, store, "appr-s2-auditor", "carol", ROLE_AUDITOR,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    op = Operation(TENANT_A, "res-1", 7, OP_BATCH_EXPORT, dict(BASE_PARAMS))
    return _case("S2_same_subject_both_roles",
                 "carol holds both roles and approves twice; the two "
                 "approvals come from one subject",
                 "rejected", service, op)


def scenario_s3() -> CaseResult:
    ids, registry, store, service = build_env()
    registry.register(TENANT_A, "res-1", 7)
    registry.register(TENANT_B, "res-1", 7)
    approve(ids, store, "appr-s3-owner", "alice", ROLE_DATA_OWNER,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    approve(ids, store, "appr-s3-auditor", "bob", ROLE_AUDITOR,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    # Same resource name and same revision, but a different tenant.
    op = Operation(TENANT_B, "res-1", 7, OP_BATCH_EXPORT, dict(BASE_PARAMS))
    return _case("S3_cross_tenant_same_name",
                 "tenant-b requests export of its own res-1 (rev 7) using "
                 "approvals bound to tenant-a/res-1",
                 "rejected", service, op)


def scenario_s4() -> CaseResult:
    ids, registry, store, service = build_env()
    registry.register(TENANT_A, "res-1", 7)
    registry.register(TENANT_A, "res-2", 7)
    approve(ids, store, "appr-s4-owner", "alice", ROLE_DATA_OWNER,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    approve(ids, store, "appr-s4-auditor", "bob", ROLE_AUDITOR,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    # Same tenant, same revision number, different resource id.
    op = Operation(TENANT_A, "res-2", 7, OP_BATCH_EXPORT, dict(BASE_PARAMS))
    return _case("S4_same_revision_different_resource",
                 "res-2 happens to share revision 7 with approved res-1 "
                 "inside tenant-a",
                 "rejected", service, op)


def scenario_s5() -> CaseResult:
    ids, registry, store, service = build_env()
    registry.register(TENANT_A, "res-1", 7)
    approve(ids, store, "appr-s5-owner", "alice", ROLE_DATA_OWNER,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    approve(ids, store, "appr-s5-auditor", "bob", ROLE_AUDITOR,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    # Same semantics, different ordering (dict keys and list elements).
    reordered = {"tables": ["beta", "alpha"], "format": "csv"}
    op = Operation(TENANT_A, "res-1", 7, OP_BATCH_EXPORT, reordered)
    return _case("S5_param_order_semantic_same",
                 "request parameters are reordered but semantically "
                 "identical to the approved parameters",
                 "approved", service, op)


def scenario_s6() -> CaseResult:
    ids, registry, store, service = build_env()
    registry.register(TENANT_A, "res-1", 7)
    approve(ids, store, "appr-s6-owner", "alice", ROLE_DATA_OWNER,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    approve(ids, store, "appr-s6-auditor", "bob", ROLE_AUDITOR,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    # Parameter content changed after approval.
    changed = {"format": "csv", "tables": ["alpha", "gamma"]}
    op = Operation(TENANT_A, "res-1", 7, OP_BATCH_EXPORT, changed)
    return _case("S6_param_content_changed",
                 "request parameters differ in content from the approved "
                 "parameters (table beta -> gamma)",
                 "rejected", service, op)


def scenario_s7() -> CaseResult:
    ids, registry, store, service = build_env()
    registry.register(TENANT_A, "res-1", 7)
    approve(ids, store, "appr-s7-owner", "alice", ROLE_DATA_OWNER,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    approve(ids, store, "appr-s7-auditor", "bob", ROLE_AUDITOR,
            TENANT_A, "res-1", 7, BASE_PARAMS)
    op = Operation(TENANT_A, "res-1", 7, OP_BATCH_EXPORT, dict(BASE_PARAMS))
    # Deterministic interleaving: after validation passes, a resource
    # update lands before the commit re-check.
    scheduler = DeterministicScheduler()
    scheduler.then("concurrent-resource-update",
                   lambda: registry.bump(TENANT_A, "res-1"))
    return _case("S7_revision_changed_before_commit",
                 "resource revision bumps from 7 to 8 after approval "
                 "validation but before commit",
                 "rejected", service, op, pre_commit=scheduler.run)


SCENARIOS = [
    scenario_s1,
    scenario_s2,
    scenario_s3,
    scenario_s4,
    scenario_s5,
    scenario_s6,
    scenario_s7,
]


def run_all() -> List[CaseResult]:
    return [scenario() for scenario in SCENARIOS]
