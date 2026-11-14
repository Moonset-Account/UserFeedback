"""SEC-VULN-043: approval object binding for high-risk batch export.

In-memory regression core. An approval is valid only for one exact
operation binding: tenant + resource id + resource revision + operation
kind + normalized parameters. Two distinct subjects must approve in the
roles ``data_owner`` and ``auditor`` with valid (approved, unexpired)
records, and the commit path atomically re-checks the live resource
revision against the approved binding before any export record is
written.

Standard library only.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

OP_BATCH_EXPORT = "batch_export"
ROLE_DATA_OWNER = "data_owner"
ROLE_AUDITOR = "auditor"
REQUIRED_ROLES = (ROLE_DATA_OWNER, ROLE_AUDITOR)

RULE_BINDING_MATCH = "R1_BINDING_MATCH"
RULE_DECISION_APPROVE = "R2_DECISION_APPROVE"
RULE_NOT_EXPIRED = "R3_NOT_EXPIRED"
RULE_ROLE_COVERAGE = "R4_ROLE_COVERAGE"
RULE_DISTINCT_SUBJECTS = "R5_DISTINCT_SUBJECTS"
RULE_ATOMIC_REVISION_MATCH = "R6_ATOMIC_REVISION_MATCH"

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"


# ---------------------------------------------------------------------------
# Parameter normalization
# ---------------------------------------------------------------------------

def _canonical_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_params(value: Any) -> Any:
    """Recursively normalize parameters so that semantically identical
    parameter sets compare equal regardless of ordering.

    - dict keys are sorted (recursively);
    - lists/tuples are treated as unordered collections and sorted by
      their canonical form;
    - scalars are passed through unchanged.
    """
    if isinstance(value, dict):
        return {k: normalize_params(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        items = [normalize_params(v) for v in value]
        return sorted(items, key=_canonical_key)
    return value


def canonical_params(params: Any) -> str:
    """Canonical string form of normalized parameters."""
    return _canonical_key(normalize_params(params))


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

def _binding_key(tenant: str, resource_id: str, resource_revision: int,
                 operation: str, params: Any) -> str:
    return "|".join([
        tenant,
        resource_id,
        str(resource_revision),
        operation,
        canonical_params(params),
    ])


@dataclass(frozen=True)
class Operation:
    """A requested high-risk operation (e.g. batch export)."""
    tenant: str
    resource_id: str
    resource_revision: int
    operation: str
    params: Any

    def binding_key(self) -> str:
        return _binding_key(self.tenant, self.resource_id,
                            self.resource_revision, self.operation, self.params)


@dataclass(frozen=True)
class ApprovalRecord:
    """One approver's decision, bound to one exact operation binding."""
    record_id: str
    tenant: str
    resource_id: str
    resource_revision: int
    operation: str
    params: Any
    approver: str
    role: str
    decision: str  # "approve" | "reject"
    expires_at: int

    def binding_key(self) -> str:
        return _binding_key(self.tenant, self.resource_id,
                            self.resource_revision, self.operation, self.params)


class ApprovalStore:
    """In-memory approval record store."""

    def __init__(self) -> None:
        self._records: List[ApprovalRecord] = []

    def add(self, record: ApprovalRecord) -> None:
        self._records.append(record)

    def all(self) -> List[ApprovalRecord]:
        return list(self._records)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

@dataclass
class RuleResult:
    rule: str
    status: str  # pass | fail | skip
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"rule": self.rule, "status": self.status, "detail": self.detail}


@dataclass
class Evaluation:
    approved: bool
    records_used: List[ApprovalRecord]
    rules: List[RuleResult]
    reason: str


def _select_pair(owner_recs: List[ApprovalRecord],
                 auditor_recs: List[ApprovalRecord]
                 ) -> Optional[Tuple[ApprovalRecord, ApprovalRecord]]:
    """Deterministically pick the first (owner, auditor) pair with
    distinct subjects. Inputs must already be sorted by record_id."""
    for owner in owner_recs:
        for auditor in auditor_recs:
            if owner.approver != auditor.approver:
                return owner, auditor
    return None


class ApprovalEvaluator:
    """Evaluates whether an operation satisfies dual-approval conditions."""

    def __init__(self, store: ApprovalStore) -> None:
        self._store = store

    def evaluate(self, op: Operation, now: int) -> Evaluation:
        rules: List[RuleResult] = []
        records = sorted(self._store.all(), key=lambda r: r.record_id)

        # R1: binding match (tenant, resource, revision, op, normalized params)
        matched = [r for r in records if r.binding_key() == op.binding_key()]
        rules.append(RuleResult(
            RULE_BINDING_MATCH,
            STATUS_PASS if matched else STATUS_FAIL,
            f"{len(matched)} record(s) match operation binding"))

        # R2: decision must be "approve"
        decided = [r for r in matched if r.decision == "approve"]
        rules.append(RuleResult(
            RULE_DECISION_APPROVE,
            STATUS_PASS if decided else STATUS_FAIL,
            f"{len(decided)} of {len(matched)} matched record(s) are approvals"))

        # R3: not expired (expiry is exclusive: expires_at must be > now)
        valid = [r for r in decided if r.expires_at > now]
        rules.append(RuleResult(
            RULE_NOT_EXPIRED,
            STATUS_PASS if valid else STATUS_FAIL,
            f"{len(valid)} valid, {len(decided) - len(valid)} expired at now={now}"))

        # R4: both required roles covered
        by_role = {role: [r for r in valid if r.role == role]
                   for role in REQUIRED_ROLES}
        covered = [role for role in REQUIRED_ROLES if by_role[role]]
        coverage = len(covered) == len(REQUIRED_ROLES)
        rules.append(RuleResult(
            RULE_ROLE_COVERAGE,
            STATUS_PASS if coverage else STATUS_FAIL,
            "roles covered: " + (",".join(covered) if covered else "none")))
        if not coverage:
            rules.append(RuleResult(RULE_DISTINCT_SUBJECTS, STATUS_SKIP,
                                    "role coverage incomplete"))
            return Evaluation(False, [], rules, "role coverage incomplete")

        # R5: the two roles must be filled by two distinct subjects
        pair = _select_pair(by_role[ROLE_DATA_OWNER], by_role[ROLE_AUDITOR])
        if pair is None:
            rules.append(RuleResult(
                RULE_DISTINCT_SUBJECTS, STATUS_FAIL,
                "no approval pair with distinct subjects"))
            return Evaluation(False, [], rules,
                              "same subject cannot fill both roles")
        rules.append(RuleResult(
            RULE_DISTINCT_SUBJECTS, STATUS_PASS,
            f"{pair[0].approver} ({ROLE_DATA_OWNER}) != "
            f"{pair[1].approver} ({ROLE_AUDITOR})"))
        return Evaluation(True, [pair[0], pair[1]], rules,
                          "dual approval satisfied")


# ---------------------------------------------------------------------------
# Resource registry and commit path
# ---------------------------------------------------------------------------

class ResourceRegistry:
    """In-memory live resource revisions, keyed by (tenant, resource_id)."""

    def __init__(self) -> None:
        self._revisions: Dict[Tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def register(self, tenant: str, resource_id: str, revision: int) -> None:
        with self._lock:
            self._revisions[(tenant, resource_id)] = revision

    def revision_of(self, tenant: str, resource_id: str) -> int:
        with self._lock:
            return self._revisions[(tenant, resource_id)]

    def bump(self, tenant: str, resource_id: str) -> int:
        """Simulate a resource update: increment the live revision."""
        with self._lock:
            key = (tenant, resource_id)
            self._revisions[key] = self._revisions[key] + 1
            return self._revisions[key]


@dataclass(frozen=True)
class ExportRecord:
    export_id: str
    tenant: str
    resource_id: str
    resource_revision: int
    operation: str
    binding_key: str
    approval_ids: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "export_id": self.export_id,
            "tenant": self.tenant,
            "resource_id": self.resource_id,
            "resource_revision": self.resource_revision,
            "operation": self.operation,
            "approval_ids": list(self.approval_ids),
        }


@dataclass
class ExportOutcome:
    approved: bool
    evaluation: Evaluation
    export_record: Optional[ExportRecord]


class ExportService:
    """Commit path for batch export. The live resource revision is
    re-checked atomically (under one lock) against the approved binding
    immediately before the export record is written."""

    def __init__(self, evaluator: ApprovalEvaluator,
                 registry: ResourceRegistry) -> None:
        self._evaluator = evaluator
        self._registry = registry
        self._lock = threading.Lock()
        self._seq = 0
        self.exports: List[ExportRecord] = []

    def execute(self, op: Operation, now: int,
                pre_commit: Optional[Callable[[], None]] = None
                ) -> ExportOutcome:
        evaluation = self._evaluator.evaluate(op, now)
        if not evaluation.approved:
            evaluation.rules.append(RuleResult(
                RULE_ATOMIC_REVISION_MATCH, STATUS_SKIP,
                "approval evaluation failed; commit not attempted"))
            return ExportOutcome(False, evaluation, None)

        # Atomic commit section: deterministic interleaving hook, then
        # re-read the live revision and compare with the approved binding.
        with self._lock:
            if pre_commit is not None:
                pre_commit()
            try:
                actual = self._registry.revision_of(op.tenant, op.resource_id)
            except KeyError:
                actual = None
            if actual != op.resource_revision:
                evaluation.rules.append(RuleResult(
                    RULE_ATOMIC_REVISION_MATCH, STATUS_FAIL,
                    f"approved revision {op.resource_revision} != live "
                    f"revision {actual}; commit aborted"))
                evaluation.approved = False
                evaluation.reason = "resource revision changed before commit"
                return ExportOutcome(False, evaluation, None)
            evaluation.rules.append(RuleResult(
                RULE_ATOMIC_REVISION_MATCH, STATUS_PASS,
                f"live revision {actual} matches approved revision"))
            self._seq += 1
            record = ExportRecord(
                export_id=f"exp-{self._seq:04d}",
                tenant=op.tenant,
                resource_id=op.resource_id,
                resource_revision=op.resource_revision,
                operation=op.operation,
                binding_key=op.binding_key(),
                approval_ids=tuple(r.record_id for r in evaluation.records_used))
            self.exports.append(record)
            return ExportOutcome(True, evaluation, record)


class DeterministicScheduler:
    """Runs pre-commit steps in a fixed, recorded order so that
    validate/update/commit interleavings are reproducible."""

    def __init__(self) -> None:
        self._steps: List[Tuple[str, Callable[[], None]]] = []
        self.trace: List[str] = []

    def then(self, label: str, fn: Callable[[], None]) -> "DeterministicScheduler":
        self._steps.append((label, fn))
        return self

    def run(self) -> None:
        for label, fn in self._steps:
            self.trace.append(label)
            fn()
