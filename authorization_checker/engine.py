"""Authorization reconciliation engine.

The engine intersects typed approval validity windows, canonicalizes natural
persons across directories, enforces separation of duties, and emits maximal
continuous grant intervals.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .factory import build_approval, build_request
from .models import (
    Approval,
    ApprovalKind,
    AuthorizationRequest,
    Interval,
    merge_intervals,
    subtract_intervals,
)
from .registry import IdentityConflictError, PersonRegistry, UnresolvedIdentityError
from .results import (
    AuthorizationDecision,
    Finding,
    Grant,
    IndependenceReport,
    PersonApproval,
    RemediationOption,
    ResponsiblePair,
)


@dataclass
class _ResolvedApproval:
    approval: Approval
    person_id: str
    clipped: Interval[Any] | None = None
    identity_verified: bool = True


@dataclass
class _MutablePersonEvidence:
    person_id: str
    role: ApprovalKind
    approval_ids: set[str] = field(default_factory=set)
    identities: set[str] = field(default_factory=set)
    primary_approval_id: str | None = None
    primary_identity: str | None = None

    def add(self, assignment: PersonApproval) -> None:
        self.approval_ids.update(assignment.approval_ids)
        self.identities.update(assignment.identities)
        if (
            self.primary_approval_id is None
            or assignment.primary_approval_id < self.primary_approval_id
        ):
            self.primary_approval_id = assignment.primary_approval_id
            self.primary_identity = assignment.primary_identity

    def to_assignment(self) -> PersonApproval:
        approval_ids = tuple(sorted(self.approval_ids))
        identities = tuple(sorted(self.identities))
        primary_approval_id = self.primary_approval_id or approval_ids[0]
        if self.primary_identity is not None:
            primary_identity = self.primary_identity
        else:
            primary_identity = identities[0]
        return PersonApproval(
            person_id=self.person_id,
            role=self.role,
            approval_ids=approval_ids,
            identities=identities,
            primary_approval_id=primary_approval_id,
            primary_identity=primary_identity,
        )


@dataclass
class _MutableGrant:
    start: Any
    end: Any
    business: _MutablePersonEvidence
    custodian: _MutablePersonEvidence

    @property
    def key(self) -> tuple[str, str]:
        return self.business.person_id, self.custodian.person_id

    @classmethod
    def from_pair(cls, start: Any, end: Any, pair: ResponsiblePair) -> "_MutableGrant":
        business = _MutablePersonEvidence(
            pair.business_owner.person_id, ApprovalKind.BUSINESS_OWNER
        )
        custodian = _MutablePersonEvidence(
            pair.account_custodian.person_id, ApprovalKind.ACCOUNT_CUSTODIAN
        )
        business.add(pair.business_owner)
        custodian.add(pair.account_custodian)
        return cls(start=start, end=end, business=business, custodian=custodian)

    def extend(self, end: Any, pair: ResponsiblePair) -> None:
        self.end = end
        self.business.add(pair.business_owner)
        self.custodian.add(pair.account_custodian)

    def touches(self, start: Any) -> bool:
        return self.end == start

    def to_grant(self) -> Grant:
        pair = ResponsiblePair(
            business_owner=self.business.to_assignment(),
            account_custodian=self.custodian.to_assignment(),
        )
        return Grant(Interval(self.start, self.end), pair)


class AuthorizationChecker:
    """Stateful checker containing approvals and identity links."""

    def __init__(
        self,
        approvals: list[Approval | dict[str, Any]] | tuple[Any, ...] | None = None,
        *,
        registry: PersonRegistry | None = None,
        identity_links: list[Any] | tuple[Any, ...] | None = None,
        strict_identities: bool = True,
    ) -> None:
        self.registry = registry if registry is not None else PersonRegistry()
        for link in identity_links or ():
            self.registry.add(link)
        self.approvals = [build_approval(approval) for approval in (approvals or [])]
        self.strict_identities = strict_identities

    def add_approval(self, approval: Approval | dict[str, Any]) -> "AuthorizationChecker":
        self.approvals.append(build_approval(approval))
        return self

    def evaluate(
        self,
        payload: dict[str, Any] | AuthorizationRequest | None = None,
        **request_kwargs: Any,
    ) -> AuthorizationDecision:
        # A payload may carry its own strict_identities switch. Explicit
        # constructor configuration remains the default.
        strict = self.strict_identities
        if isinstance(payload, dict) and "strict_identities" in payload:
            strict = bool(payload["strict_identities"])

        request = build_request(payload, **request_kwargs)
        findings: list[Finding] = []

        scoped = [
            approval
            for approval in self.approvals
            if approval.scope.covers(
                project_id=request.project_id,
                service_id=request.service_id,
                jurisdiction_id=request.jurisdiction_id,
            )
        ]

        resolved_scoped: list[_ResolvedApproval] = []
        unresolved_seen: set[str] = set()
        identity_assurance_complete = True
        for approval in scoped:
            person_id, identity_verified, resolution_findings = self._resolve_person(
                approval, strict
            )
            if not identity_verified:
                identity_assurance_complete = False
            for finding in resolution_findings:
                if finding.code == "unresolved_identity" and finding.details.get("urn") in unresolved_seen:
                    continue
                if finding.code == "unresolved_identity":
                    unresolved_seen.add(finding.details.get("urn", ""))
                findings.append(finding)
            resolved_scoped.append(
                _ResolvedApproval(
                    approval,
                    person_id,
                    identity_verified=identity_verified,
                )
            )

        verified_scoped = [item for item in resolved_scoped if item.identity_verified]
        independence = build_independence_report(verified_scoped)
        if independence.status != "independent_pair_available":
            findings.append(
                Finding(
                    code=independence.status,
                    message=independence.message,
                    person_id=(
                        independence.same_person_ids[0]
                        if independence.same_person_ids
                        else None
                    ),
                    details=independence.to_dict(),
                )
            )

        timed: list[_ResolvedApproval] = []
        for item in resolved_scoped:
            clipped = item.approval.interval.intersection(request.interval)
            if clipped is None:
                findings.append(
                    Finding(
                        code="approval_outside_requested_interval",
                        message=(
                            f"批准 {item.approval.approval_id} 虽在管辖范围内，"
                            "但有效期与请求区间不相交"
                        ),
                        approval_id=item.approval.approval_id,
                        person_id=item.person_id,
                        details={
                            "approval_interval": item.approval.interval.to_dict(),
                            "requested_interval": request.interval.to_dict(),
                        },
                    )
                )
            else:
                item.clipped = clipped
                timed.append(item)

        interval_independence = build_independence_report(
            [item for item in timed if item.identity_verified]
        )

        business_records: dict[str, list[_ResolvedApproval]] = defaultdict(list)
        custodian_records: dict[str, list[_ResolvedApproval]] = defaultdict(list)
        for item in timed:
            # Fail closed: an identity not linked to a canonical natural person
            # cannot prove that the two duty holders are distinct humans.
            if not item.identity_verified:
                continue
            if item.approval.kind is ApprovalKind.BUSINESS_OWNER:
                business_records[item.person_id].append(item)
            else:
                custodian_records[item.person_id].append(item)

        # The same natural person's two role approvals are never a valid pair,
        # even when another independent pair also exists.
        for person_id in sorted(set(business_records) & set(custodian_records)):
            findings.append(
                Finding(
                    code="same_natural_person_pair_ignored",
                    message=(
                        f"同一自然人 {person_id!r} 同时承担业务负责人和账号保管人；"
                        "该组合违反职责分离，已忽略"
                    ),
                    person_id=person_id,
                    details={
                        "business_approval_ids": sorted(
                            item.approval.approval_id
                            for item in business_records[person_id]
                        ),
                        "custodian_approval_ids": sorted(
                            item.approval.approval_id
                            for item in custodian_records[person_id]
                        ),
                    },
                )
            )

        points = sorted(
            {request.interval.start, request.interval.end}
            | {item.clipped.start for item in timed}  # type: ignore[union-attr]
            | {item.clipped.end for item in timed}  # type: ignore[union-attr]
        )

        selected_builders: list[_MutableGrant] = []
        pair_current: dict[tuple[str, str], _MutableGrant] = {}
        pair_completed: list[Grant] = []

        for index in range(len(points) - 1):
            segment = Interval(points[index], points[index + 1])
            active = [
                item
                for item in timed
                if item.clipped is not None and item.clipped.contains(segment.start)
            ]
            active_business: dict[str, list[_ResolvedApproval]] = defaultdict(list)
            active_custodian: dict[str, list[_ResolvedApproval]] = defaultdict(list)
            for item in active:
                if not item.identity_verified:
                    continue
                if item.approval.kind is ApprovalKind.BUSINESS_OWNER:
                    active_business[item.person_id].append(item)
                else:
                    active_custodian[item.person_id].append(item)

            pairs: list[ResponsiblePair] = []
            for business_person in sorted(active_business):
                for custodian_person in sorted(active_custodian):
                    if business_person == custodian_person:
                        continue
                    business_assignment = make_person_approval(
                        business_person,
                        ApprovalKind.BUSINESS_OWNER,
                        active_business[business_person],
                    )
                    custodian_assignment = make_person_approval(
                        custodian_person,
                        ApprovalKind.ACCOUNT_CUSTODIAN,
                        active_custodian[custodian_person],
                    )
                    pairs.append(
                        ResponsiblePair(business_assignment, custodian_assignment)
                    )

            if not pairs:
                # A gap closes any run that was being tracked.
                for builder in pair_current.values():
                    pair_completed.append(builder.to_grant())
                pair_current.clear()
                continue

            pairs.sort(key=lambda pair: pair.key)
            selected_pair = pairs[0]
            # Prefer continuing the immediately selected pair when it remains
            # valid. This avoids gratuitous responsibility changes merely
            # because a lexicographically earlier alternative starts.
            if selected_builders:
                previous_key = selected_builders[-1].key
                for pair in pairs:
                    if pair.key == previous_key:
                        selected_pair = pair
                        break

            if selected_builders and selected_builders[-1].key == selected_pair.key:
                last = selected_builders[-1]
                if last.touches(segment.start):
                    last.extend(segment.end, selected_pair)
                else:
                    selected_builders.append(
                        _MutableGrant.from_pair(segment.start, segment.end, selected_pair)
                    )
            else:
                selected_builders.append(
                    _MutableGrant.from_pair(segment.start, segment.end, selected_pair)
                )

            active_keys = {pair.key for pair in pairs}
            for key in list(pair_current):
                builder = pair_current[key]
                if key not in active_keys:
                    pair_completed.append(builder.to_grant())
                    del pair_current[key]

            for pair in pairs:
                existing = pair_current.get(pair.key)
                if existing is None:
                    pair_current[pair.key] = _MutableGrant.from_pair(
                        segment.start, segment.end, pair
                    )
                elif existing.touches(segment.start):
                    existing.extend(segment.end, pair)
                else:
                    pair_completed.append(existing.to_grant())
                    pair_current[pair.key] = _MutableGrant.from_pair(
                        segment.start, segment.end, pair
                    )

        pair_completed.extend(builder.to_grant() for builder in pair_current.values())
        grants = tuple(builder.to_grant() for builder in selected_builders)
        feasible = tuple(
            sorted(
                pair_completed,
                key=lambda grant: (
                    grant.start,
                    grant.responsible.business_owner.person_id,
                    grant.responsible.account_custodian.person_id,
                ),
            )
        )

        continuous = tuple(merge_intervals([grant.interval for grant in grants]))
        gaps = tuple(subtract_intervals(request.interval, continuous))

        return AuthorizationDecision(
            service_id=request.service_id,
            project_id=request.project_id,
            requested_interval=request.interval,
            grants=grants,
            feasible_pair_intervals=feasible,
            continuous_coverage=continuous,
            gaps=gaps,
            independence=independence,
            interval_independence=interval_independence,
            findings=tuple(findings),
            strict_identities=strict,
            identity_assurance_complete=identity_assurance_complete,
        )

    def _resolve_person(
        self, approval: Approval, strict: bool
    ) -> tuple[str, bool, list[Finding]]:
        findings: list[Finding] = []
        registry_person: str | None = None
        try:
            registry_person = self.registry.person_for(approval.identity)
        except UnresolvedIdentityError:
            if strict and approval.person_id is None:
                raise
            if approval.person_id is None:
                fallback = f"unresolved:{approval.identity.urn}"
                findings.append(
                    Finding(
                        code="unresolved_identity",
                        message=(
                            f"身份 {approval.identity.urn!r} 未链接到自然人；"
                            "无法证明跨目录身份是否属于同一人，按失败关闭处理，"
                            "不将该批准用于开通"
                        ),
                        approval_id=approval.approval_id,
                        person_id=fallback,
                        details={"urn": approval.identity.urn},
                    )
                )
                return fallback, False, findings

        if approval.person_id is not None:
            if (
                registry_person is not None
                and registry_person != approval.person_id
            ):
                raise IdentityConflictError(
                    f"approval {approval.approval_id!r} declares person "
                    f"{approval.person_id!r}, but identity {approval.identity.urn!r} "
                    f"is registered to {registry_person!r}"
                )
            return approval.person_id, True, findings

        assert registry_person is not None
        return registry_person, True, findings


def make_person_approval(
    person_id: str,
    role: ApprovalKind,
    records: list[_ResolvedApproval],
) -> PersonApproval:
    ordered = sorted(records, key=lambda item: item.approval.approval_id)
    approval_ids = tuple(item.approval.approval_id for item in ordered)
    identities = tuple(sorted({item.approval.identity.urn for item in ordered}))
    primary = ordered[0].approval
    return PersonApproval(
        person_id=person_id,
        role=role,
        approval_ids=approval_ids,
        identities=identities,
        primary_approval_id=primary.approval_id,
        primary_identity=primary.identity.urn,
    )


def build_independence_report(
    items: list[_ResolvedApproval],
) -> IndependenceReport:
    business: dict[str, list[_ResolvedApproval]] = defaultdict(list)
    custodian: dict[str, list[_ResolvedApproval]] = defaultdict(list)
    for item in items:
        if item.approval.kind is ApprovalKind.BUSINESS_OWNER:
            business[item.person_id].append(item)
        else:
            custodian[item.person_id].append(item)

    business_ids = tuple(sorted(business))
    custodian_ids = tuple(sorted(custodian))
    same_ids = tuple(sorted(set(business_ids) & set(custodian_ids)))
    has_pair = any(business_person != custodian_person
                   for business_person in business_ids
                   for custodian_person in custodian_ids)

    if has_pair:
        return IndependenceReport(
            status="independent_pair_available",
            business_owner_person_ids=business_ids,
            account_custodian_person_ids=custodian_ids,
            same_person_ids=same_ids,
            missing_approval_types=(),
            requirement_groups=(),
            message="存在两名不同自然人分别承担业务负责人和账号保管人职责",
        )

    missing_types: list[ApprovalKind] = []
    groups: list[tuple[RemediationOption, ...]] = []

    if not business and not custodian:
        missing_types = [
            ApprovalKind.BUSINESS_OWNER,
            ApprovalKind.ACCOUNT_CUSTODIAN,
        ]
        groups = [
            (
                RemediationOption(
                    ApprovalKind.BUSINESS_OWNER,
                    None,
                    "项目/服务管辖范围内没有业务负责人批准",
                ),
            ),
            (
                RemediationOption(
                    ApprovalKind.ACCOUNT_CUSTODIAN,
                    None,
                    "项目/服务管辖范围内没有账号保管人批准",
                ),
            ),
        ]
        status = "missing_both_types"
        message = "管辖范围内未找到两类批准，需补充业务负责人和账号保管人批准"
    elif not business:
        missing_types = [ApprovalKind.BUSINESS_OWNER]
        distinct_from = custodian_ids[0] if len(custodian_ids) == 1 else None
        groups = [
            (
                RemediationOption(
                    ApprovalKind.BUSINESS_OWNER,
                    distinct_from,
                    "只有账号保管人批准；需补充独立的业务负责人批准",
                ),
            )
        ]
        status = "missing_business_owner"
        who = f"（须不同于 {distinct_from!r}）" if distinct_from else ""
        message = f"缺少业务负责人这一类独立批准{who}"
    elif not custodian:
        missing_types = [ApprovalKind.ACCOUNT_CUSTODIAN]
        distinct_from = business_ids[0] if len(business_ids) == 1 else None
        groups = [
            (
                RemediationOption(
                    ApprovalKind.ACCOUNT_CUSTODIAN,
                    distinct_from,
                    "只有业务负责人批准；需补充独立的账号保管人批准",
                ),
            )
        ]
        status = "missing_account_custodian"
        who = f"（须不同于 {distinct_from!r}）" if distinct_from else ""
        message = f"缺少账号保管人这一类独立批准{who}"
    else:
        # If both role sets are non-empty and no distinct pair exists, both sets
        # must be the same singleton natural person.
        person_id = same_ids[0] if same_ids else business_ids[0]
        status = "same_person_only"
        missing_types = []
        groups = [
            (
                RemediationOption(
                    ApprovalKind.ACCOUNT_CUSTODIAN,
                    person_id,
                    "可保留该自然人的业务负责人批准，由另一名自然人担任账号保管人",
                ),
                RemediationOption(
                    ApprovalKind.BUSINESS_OWNER,
                    person_id,
                    "可保留该自然人的账号保管人批准，由另一名自然人担任业务负责人",
                ),
            )
        ]
        message = (
            f"项目管辖范围内只找到自然人 {person_id!r} 的批准；两类职责不得由同一人承担。"
            "需补充一名独立责任人：账号保管人批准或业务负责人批准，二者补一类即可形成组合"
        )

    return IndependenceReport(
        status=status,
        business_owner_person_ids=business_ids,
        account_custodian_person_ids=custodian_ids,
        same_person_ids=same_ids,
        missing_approval_types=tuple(missing_types),
        requirement_groups=tuple(groups),
        message=message,
    )


def evaluate_authorization(
    payload: dict[str, Any] | None = None,
    *,
    approvals: list[Approval | dict[str, Any]] | tuple[Any, ...] | None = None,
    registry: PersonRegistry | None = None,
    identity_links: list[Any] | tuple[Any, ...] | None = None,
    strict_identities: bool | None = None,
    **request_kwargs: Any,
) -> AuthorizationDecision:
    """Functional one-call API.

    Approvals may be supplied explicitly or as ``payload["approvals"]``.
    Identity links may be supplied explicitly or as
    ``payload["identity_links"]``. Strict identity resolution defaults to
    ``True`` and may be disabled in the payload or via keyword argument.
    """

    payload_data = payload if isinstance(payload, dict) else {}
    effective_approvals = approvals
    if effective_approvals is None:
        effective_approvals = payload_data.get("approvals", [])

    effective_links = identity_links
    if effective_links is None:
        effective_links = payload_data.get("identity_links", [])

    if strict_identities is None:
        effective_strict = bool(payload_data.get("strict_identities", True))
    else:
        effective_strict = strict_identities

    checker = AuthorizationChecker(
        effective_approvals,
        registry=registry,
        identity_links=effective_links,
        strict_identities=effective_strict,
    )
    return checker.evaluate(payload, **request_kwargs)
