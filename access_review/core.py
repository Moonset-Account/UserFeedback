"""Segregation-of-duty review for the permission-provisioning service.

The module is intentionally dependency-free.  Its main entry point is
:func:`review_batch`, which accepts plain JSON-compatible dictionaries and
returns a JSON-compatible :class:`ReviewReport`.

The default policy implements the finance rule:

* the same user,
* in the same project,
* must not hold the duty that enters a receiving/payment account and the duty
  that approves that account

during overlapping validity intervals.  Personal/direct grants and inherited
team grants are both included in the final effective state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .time_windows import TimeWindow, window_from_grant

ENTER_DUTY = "payment_account:enter"
APPROVE_DUTY = "payment_account:approve"
GLOBAL_PROJECT = "*"

_REVOKE_OPS = {
    "revoke",
    "revoked",
    "revocation",
    "remove",
    "removed",
    "delete",
    "deleted",
    "deactivate",
    "disable",
}
_GRANT_OPS = {
    "grant",
    "granted",
    "add",
    "added",
    "create",
    "created",
    "assign",
    "assigned",
    "enable",
    "enabled",
    "open",
}

_ENTER_ALIASES = {
    ENTER_DUTY,
    "enter",
    "entry",
    "input",
    "create",
    "new",
    "payment_account_enter",
    "payment_account_entry",
    "payment_account_create",
    "enter_payment_account",
    "entry_payment_account",
    "create_payment_account",
    "input_payment_account",
    "payment_account_input",
    "receiving_account_enter",
    "receiving_account_entry",
    "receiving_account_create",
    "enter_receiving_account",
    "create_receiving_account",
    "collection_account_enter",
    "collection_account_create",
    "enter_collection_account",
    "create_collection_account",
    "payee_account_enter",
    "payee_account_create",
    "enter_payee_account",
    "create_payee_account",
    "录入",
    "录入收款账户",
    "录入收款账号",
    "收款账户录入",
    "收款账号录入",
    "新增收款账户",
    "创建收款账户",
    "登记收款账户",
}

_APPROVE_ALIASES = {
    APPROVE_DUTY,
    "approve",
    "approval",
    "approver",
    "authorize",
    "authorization",
    "payment_account_approve",
    "payment_account_approval",
    "approve_payment_account",
    "approval_payment_account",
    "receiving_account_approve",
    "receiving_account_approval",
    "approve_receiving_account",
    "collection_account_approve",
    "collection_account_approval",
    "approve_collection_account",
    "payee_account_approve",
    "approve_payee_account",
    "批准",
    "审批",
    "核准",
    "批准收款账户",
    "审批收款账户",
    "核准收款账户",
}


class ReviewInputError(ValueError):
    """Raised when request data is structurally or temporally invalid."""


@dataclass(frozen=True)
class DutyRule:
    rule_id: str
    first_duties: frozenset[str]
    second_duties: frozenset[str]
    first_label: str = "entry"
    second_label: str = "approval"
    description: str = ""

    @classmethod
    def payment_account_rule(cls) -> "DutyRule":
        return cls(
            rule_id="payment-account:entry-vs-approval",
            first_duties=frozenset({ENTER_DUTY}),
            second_duties=frozenset({APPROVE_DUTY}),
            first_label="entry",
            second_label="approval",
            description=(
                "The same person must not enter and approve a receiving "
                "account in the same project at the same time."
            ),
        )


@dataclass
class Role:
    role_id: str
    duties: list[Any] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)


@dataclass
class Grant:
    grant_id: str
    user_id: Optional[str]
    role_id: Optional[str]
    project_id: str
    window: TimeWindow
    source_type: str = "direct"  # direct | team
    team_id: Optional[str] = None
    batch_item_id: Optional[str] = None
    in_batch: bool = False
    role_ids: Optional[list[str]] = None
    duties: Optional[list[Any]] = None
    replaces: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Revocation:
    target_grant_id: Optional[str] = None
    revocation_id: Optional[str] = None
    user_id: Optional[str] = None
    role_id: Optional[str] = None
    project_id: Optional[str] = None
    source_type: Optional[str] = None
    team_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Membership:
    membership_id: str
    user_id: str
    team_id: str
    window: TimeWindow
    project_id: str = GLOBAL_PROJECT


class ReviewReport(dict):
    """Dictionary report with small convenience property accessors."""

    @property
    def status(self) -> str:
        return self.get("status", "unknown")

    @property
    def decision(self) -> str:
        return self.get("decision", "manual_review")

    @property
    def suspended(self) -> bool:
        return bool(self.get("suspended"))

    @property
    def blocked(self) -> bool:
        return bool(self.get("blocked", self.get("suspended")))

    @property
    def allowed(self) -> bool:
        return bool(self.get("allowed"))

    @property
    def conflicts(self) -> list[dict[str, Any]]:
        return list(self.get("conflicts", []))

    @property
    def blocking_conflicts(self) -> list[dict[str, Any]]:
        return list(self.get("blocking_conflicts", []))


class SodReviewer:
    """Review batches against one or more segregation-of-duties rules."""

    def __init__(
        self,
        rules: Optional[Iterable[DutyRule]] = None,
        *,
        extra_duty_aliases: Optional[dict[str, Iterable[str]]] = None,
        block_on_unrelated_conflicts: bool = True,
    ) -> None:
        self.rules = list(rules) if rules is not None else [DutyRule.payment_account_rule()]
        self.duty_aliases = _build_alias_map(extra_duty_aliases or {})
        self.block_on_unrelated_conflicts = block_on_unrelated_conflicts

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def review(self, payload: Any = None, **kwargs: Any) -> ReviewReport:
        data = self._payload_to_dict(payload, kwargs)
        warnings: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        default_project = _normalize_project(
            _first(data, ("project_id", "project", "project_code", "default_project_id"))
        )
        roles = self._normalize_roles(data.get("roles") or data.get("role_definitions") or {})

        try:
            memberships = self._normalize_memberships(
                _first_list(
                    data,
                    ("team_memberships", "memberships", "team_members", "user_teams"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise ReviewInputError(f"invalid team membership: {exc}") from exc

        existing_all, batch_all, raw_revocations, change_issues = self._extract_grants_and_revocations(
            data, default_project
        )
        existing_direct = [grant for grant in existing_all if grant.source_type != "team"]
        batch_direct = [grant for grant in batch_all if grant.source_type != "team"]
        inline_team_grants = [
            grant for grant in [*existing_all, *batch_all] if grant.source_type == "team"
        ]
        for issue in change_issues:
            if issue.get("code") == "UNRECOGNIZED_BATCH_ITEM":
                errors.append(issue)
            else:
                warnings.append(issue)

        try:
            context_team = self._normalize_grants(
                _first_list(data, ("team_grants", "team_role_grants", "group_grants")),
                in_batch=False,
                default_project=default_project,
                force_source_type="team",
            )
            existing_team = self._normalize_grants(
                _first_list(data, ("existing_team_grants", "current_team_grants")),
                in_batch=False,
                default_project=default_project,
                force_source_type="team",
            )
            batch_team = self._normalize_grants(
                _first_list(data, ("batch_team_grants", "requested_team_grants", "new_team_grants")),
                in_batch=True,
                default_project=default_project,
                force_source_type="team",
            )
        except (TypeError, ValueError) as exc:
            raise ReviewInputError(f"invalid team grant: {exc}") from exc

        team_grants = _dedupe_grants_by_id(
            existing_team + context_team + batch_team + inline_team_grants
        )

        try:
            revocations = [self._normalize_revocation(item) for item in raw_revocations]
        except (TypeError, ValueError) as exc:
            raise ReviewInputError(f"invalid revocation: {exc}") from exc

        for rev in revocations:
            if not rev.target_grant_id and not rev.has_selector():
                errors.append(
                    {
                        "code": "INVALID_REVOCATION",
                        "message": "revocation must identify a grant id or include a matching selector",
                        "revocation_id": rev.revocation_id,
                    }
                )

        explicit_revoked_ids: set[str] = set()
        for rev in revocations:
            if rev.target_grant_id:
                explicit_revoked_ids.add(str(rev.target_grant_id))
        for grant in batch_direct:
            explicit_revoked_ids.update(str(x) for x in grant.replaces if x)

        # Attribute revocations describe old/current assignments.  Apply them
        # before batch additions so a requested final grant is not accidentally
        # removed by a broad role-level selector.
        natural_revocations = [r for r in revocations if not r.target_grant_id and r.has_selector()]
        revoked_existing_ids: set[str] = set()
        for grant in existing_direct:
            if grant.grant_id in explicit_revoked_ids:
                revoked_existing_ids.add(grant.grant_id)
                continue
            if any(_revocation_matches_grant(rev, grant) for rev in natural_revocations):
                revoked_existing_ids.add(grant.grant_id)

        final_direct: dict[str, Grant] = {}
        revoked_batch_ids: set[str] = set()
        for grant in existing_direct:
            if grant.grant_id in revoked_existing_ids:
                continue
            final_direct[grant.grant_id] = grant
        for grant in batch_direct:
            if grant.grant_id in explicit_revoked_ids:
                revoked_batch_ids.add(grant.grant_id)
                continue
            # A same-ID batch grant is an upsert and replaces baseline state.
            final_direct[grant.grant_id] = grant

        final_team: dict[str, Grant] = {}
        revoked_team_ids: set[str] = set()
        for grant in team_grants:
            if grant.grant_id in explicit_revoked_ids:
                revoked_team_ids.add(grant.grant_id)
                continue
            if any(_revocation_matches_grant(rev, grant) for rev in natural_revocations):
                revoked_team_ids.add(grant.grant_id)
                continue
            final_team[grant.grant_id] = grant

        revocation_summary = [
            {
                "revocation_id": rev.revocation_id,
                "target_grant_id": rev.target_grant_id,
                "user_id": rev.user_id,
                "role_id": rev.role_id,
                "project_id": rev.project_id,
                "source_type": rev.source_type,
                "team_id": rev.team_id,
            }
            for rev in revocations
        ]

        instances, role_errors = self._build_duty_instances(
            list(final_direct.values()),
            list(final_team.values()),
            memberships,
            roles,
        )
        errors.extend(role_errors)

        conflicts = self._find_conflicts(instances)
        blocking = [c for c in conflicts if c["blocking"]]
        if self.block_on_unrelated_conflicts and conflicts:
            blocking = conflicts

        if errors:
            status = "error"
            decision = "manual_review"
            suspended = True
            allowed = False
        elif blocking:
            status = "suspended"
            decision = "suspend"
            suspended = True
            allowed = False
        else:
            status = "approved"
            decision = "approve"
            suspended = False
            allowed = True

        effective_sources = _summarize_instances(instances)
        report = ReviewReport(
            {
                "batch_id": data.get("batch_id") or data.get("id"),
                "status": status,
                "state": status,
                "decision": decision,
                "outcome": decision,
                "suspended": suspended,
                "blocked": suspended,
                "allowed": allowed,
                "approved": allowed,
                "requires_manual_review": suspended,
                "conflict_count": len(conflicts),
                "blocking_conflict_count": len(blocking),
                "conflicts": conflicts,
                "violations": conflicts,
                "blocking_conflicts": blocking,
                "blocking_violations": blocking,
                "effective_sources": effective_sources,
                "effective_grants": effective_sources,
                "changes": {
                    "granted": [_grant_summary(grant) for grant in batch_all],
                    "revocations": revocation_summary,
                    "revoked_existing_grant_ids": sorted(revoked_existing_ids),
                    "revoked_batch_grant_ids": sorted(revoked_batch_ids),
                    "revoked_team_grant_ids": sorted(revoked_team_ids),
                    "final_direct_grant_ids": sorted(final_direct),
                    "final_team_grant_ids": sorted(final_team),
                },
                "warnings": warnings,
                "errors": errors,
                "reviewer": "access_review.SodReviewer",
            }
        )
        return report

    # ------------------------------------------------------------------
    # Input normalization
    # ------------------------------------------------------------------
    def _payload_to_dict(self, payload: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        if payload is None:
            data: dict[str, Any] = {}
        elif isinstance(payload, dict):
            data = dict(payload)
        elif isinstance(payload, list):
            data = {"grants": payload}
        else:
            raise ReviewInputError("review_batch expects a dict, list, or None")

        # Convenience: {"batch": {"items": [...]}, "roles": ...}
        nested = data.get("batch") or data.get("request") or data.get("input")
        if isinstance(nested, dict):
            merged = dict(nested)
            merged.update({k: v for k, v in data.items() if k not in ("batch", "request", "input")})
            data = merged

        if kwargs:
            data.update(kwargs)
        return data

    def _normalize_roles(self, raw: Any) -> dict[str, Role]:
        roles: dict[str, Role] = {}
        if not raw:
            return roles

        if isinstance(raw, dict):
            iterator = raw.items()
            for role_id, spec in iterator:
                roles[str(role_id)] = self._normalize_role(role_id, spec)
        elif isinstance(raw, list):
            for spec in raw:
                if not isinstance(spec, dict):
                    raise ReviewInputError("role definitions must be objects")
                role_id = _first(spec, ("role_id", "id", "name", "code"))
                if role_id is None:
                    raise ReviewInputError("role definition is missing role_id/id")
                roles[str(role_id)] = self._normalize_role(role_id, spec)
        else:
            raise ReviewInputError("roles must be a mapping or list")
        return roles

    def _normalize_role(self, role_id: str, spec: Any) -> Role:
        if isinstance(spec, (list, tuple, set, frozenset)):
            return Role(role_id=str(role_id), duties=list(spec))
        if isinstance(spec, str):
            return Role(role_id=str(role_id), duties=[spec])
        if not isinstance(spec, dict):
            raise ReviewInputError(f"unsupported role definition for {role_id!r}")

        duties = _first(
            spec,
            ("duties", "responsibilities", "permissions", "entitlements", "actions"),
            default=[],
        )
        includes = _first(
            spec,
            ("includes", "included_roles", "inherits", "inherit_roles", "parent_roles"),
            default=[],
        )
        return Role(
            role_id=str(role_id),
            duties=list(duties) if isinstance(duties, (list, tuple, set, frozenset)) else [duties],
            includes=[str(x) for x in includes] if isinstance(includes, (list, tuple, set, frozenset)) else [str(includes)],
        )

    def _extract_grants_and_revocations(
        self, data: dict[str, Any], default_project: str
    ) -> tuple[list[Grant], list[Grant], list[Any], list[dict[str, Any]]]:
        existing_raw = _first_list(
            data,
            (
                "existing_grants",
                "current_grants",
                "baseline_grants",
                "grants_existing",
                "active_grants",
                "personal_grants",
                "direct_grants",
            ),
        )
        batch_raw = _first_list(
            data,
            (
                "grants",
                "batch_grants",
                "grant_requests",
                "requested_grants",
                "new_grants",
                "grant",
            ),
        )
        revocation_raw = list(
            _first_list(
                data,
                (
                    "revocations",
                    "revoke",
                    "revokes",
                    "revocation_requests",
                    "revoke_requests",
                    "revoked_grants",
                ),
            )
        )

        warnings: list[dict[str, Any]] = []
        for key in ("changes", "operations", "items", "batch_items", "entries", "grant_changes"):
            for item in _as_list(data.get(key)):
                if not isinstance(item, dict):
                    continue
                op = str(
                    _first(item, ("op", "operation", "action", "change_type", "type"), default="")
                ).strip().lower()
                if op in _REVOKE_OPS or "revoke" in op:
                    revocation_raw.append(item)
                elif op in _GRANT_OPS or "grant" in op or "assign" in op:
                    batch_raw.append(item)
                elif _first(item, ("target_grant_id", "revocation_id")) is not None:
                    revocation_raw.append(item)
                elif _first(item, ("role_id", "role", "role_ids", "roles", "duties", "responsibilities")) is not None:
                    batch_raw.append(item)
                else:
                    warnings.append({"code": "UNRECOGNIZED_BATCH_ITEM", "item": _safe_id(item)})

        existing = self._normalize_grants(
            existing_raw, in_batch=False, default_project=default_project
        )
        batch = self._normalize_grants(batch_raw, in_batch=True, default_project=default_project)
        return existing, batch, revocation_raw, warnings

    def _normalize_grants(
        self,
        raw_items: Iterable[Any],
        *,
        in_batch: bool,
        default_project: str,
        force_source_type: Optional[str] = None,
    ) -> list[Grant]:
        grants: list[Grant] = []
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                # A bare string can identify a grant in revocation lists, but
                # grant lists require objects.
                raise ReviewInputError(f"grant at index {index} must be an object")

            grant_id = _first(
                item,
                ("grant_id", "assignment_id", "entitlement_id"),
            )
            batch_item_id = _first(
                item,
                ("batch_item_id", "request_id", "item_id", "operation_id"),
            )
            if grant_id is None:
                grant_id = item.get("id")
            if batch_item_id is None:
                batch_item_id = item.get("id")
            if grant_id is None:
                grant_id = f"{'batch' if in_batch else 'grant'}:{index}:{_safe_id(item)}"
            grant_id = str(grant_id)

            user_id = _extract_user_id(item)
            team_id = _first_str(item, ("team_id", "teamId", "team", "group_id", "group"))
            role_values = _first(
                item,
                ("role_ids", "roles", "role_id", "roleId", "role", "role_name", "role_code"),
            )
            role_ids: list[str] = []
            for role_value in _as_list(role_values):
                if isinstance(role_value, dict):
                    nested = _first(role_value, ("id", "role_id", "code", "name"))
                    if nested is not None:
                        role_ids.append(str(nested))
                elif role_value is not None:
                    role_ids.append(str(role_value))
            role_id = role_ids[0] if role_ids else None

            project_id = _extract_project(item, default_project)
            source_type = force_source_type or _first_str(
                item, ("source_type", "principal_type", "grant_type")
            )
            if source_type is None:
                source_type = "team" if (team_id and not user_id) else "direct"
            source_type = _normalize_source_type(source_type)

            if source_type == "direct" and not user_id:
                raise ReviewInputError(f"direct grant {grant_id!r} is missing user_id")
            if source_type == "team" and not team_id:
                raise ReviewInputError(f"team grant {grant_id!r} is missing team_id")

            try:
                window = window_from_grant(item)
            except (TypeError, ValueError) as exc:
                raise ReviewInputError(f"invalid validity for grant {grant_id!r}: {exc}") from exc

            duties = _first(
                item,
                ("duties", "responsibilities", "permissions", "actions"),
            )
            if duties is not None and not isinstance(duties, (list, tuple, set, frozenset)):
                duties = [duties]

            replaces = _as_list(
                _first(
                    item,
                    (
                        "replaces",
                        "replaces_grant_ids",
                        "replaced_grant_ids",
                        "replaces_grant_id",
                        "old_grant_id",
                    ),
                )
            )

            grants.append(
                Grant(
                    grant_id=grant_id,
                    user_id=str(user_id) if user_id is not None else None,
                    role_id=role_id,
                    project_id=project_id,
                    window=window,
                    source_type=source_type,
                    team_id=str(team_id) if team_id is not None else None,
                    batch_item_id=str(batch_item_id) if batch_item_id is not None else grant_id,
                    in_batch=bool(in_batch or item.get("batch") or item.get("in_batch") or item.get("requested")),
                    role_ids=role_ids or None,
                    duties=list(duties) if duties is not None else None,
                    replaces=[str(x) for x in replaces],
                    raw=dict(item),
                )
            )
        return grants

    def _normalize_revocation(self, item: Any) -> Revocation:
        if isinstance(item, str):
            return Revocation(target_grant_id=item)
        if not isinstance(item, dict):
            raise ReviewInputError("revocation must be a grant id string or object")

        target = _first(
            item,
            (
                "target_grant_id",
                "grant_id",
                "revoked_grant_id",
                "target_id",
                "assignment_id",
            ),
        )
        revocation_id = _first(
            item, ("revocation_id", "revoke_id", "request_id", "batch_item_id", "item_id")
        )
        op = str(_first(item, ("op", "operation", "action", "change_type"), default="")).lower()
        if target is None and item.get("id") is not None:
            # In {"op":"revoke","id":"g1"}, id is the target.  In an object
            # with its own revocation_id/request_id, id may be that operation's
            # id and no target was supplied.
            if op in _REVOKE_OPS or "revoke" in op:
                target = item["id"]
            elif revocation_id is None:
                target = item["id"]
            else:
                revocation_id = revocation_id or item["id"]

        user_id = _extract_user_id(item)
        role_value = _first(item, ("role_id", "role", "role_name", "role_code"))
        role_id = str(role_value) if role_value is not None and not isinstance(role_value, dict) else None
        if isinstance(role_value, dict):
            role_id = str(_first(role_value, ("id", "role_id", "code", "name")))
        project_id = _extract_project(item, None)
        if project_id == GLOBAL_PROJECT and not _has_project_key(item):
            project_id = None

        source_type = _first_str(item, ("source_type", "principal_type", "grant_type"))
        team_id = _first_str(item, ("team_id", "team", "group_id", "group"))
        return Revocation(
            target_grant_id=str(target) if target is not None else None,
            revocation_id=str(revocation_id) if revocation_id is not None else None,
            user_id=str(user_id) if user_id is not None else None,
            role_id=role_id,
            project_id=project_id,
            source_type=_normalize_source_type(source_type) if source_type else None,
            team_id=team_id,
            raw=dict(item),
        )

    def _normalize_memberships(self, raw: Any) -> list[Membership]:
        memberships: list[Membership] = []
        if not raw:
            return memberships

        # Mapping form: {"team-a": ["u1", "u2"]}
        if isinstance(raw, dict):
            for team_id, members in raw.items():
                for index, member in enumerate(_as_list(members)):
                    memberships.append(
                        self._membership_from_value(member, team_id=team_id, index=index)
                    )
            return memberships

        for index, item in enumerate(_as_list(raw)):
            if isinstance(item, dict) and isinstance(item.get("members"), (list, tuple)):
                team_id = _first_str(item, ("team_id", "id", "team", "group_id"))
                for member_index, member in enumerate(item["members"]):
                    memberships.append(
                        self._membership_from_value(
                            member, team_id=team_id or "", index=member_index
                        )
                    )
            else:
                memberships.append(self._membership_from_value(item, team_id=None, index=index))
        return memberships

    def _membership_from_value(self, value: Any, *, team_id: Optional[str], index: int) -> Membership:
        if isinstance(value, str):
            if team_id is None:
                raise ReviewInputError("bare membership user requires a team_id")
            return Membership(
                membership_id=f"membership:{team_id}:{value}:{index}",
                user_id=value,
                team_id=str(team_id),
                window=TimeWindow.unbounded(),
            )
        if not isinstance(value, dict):
            raise ReviewInputError("membership must be a user id string or object")

        user_id = _extract_user_id(value)
        effective_team = _first_str(value, ("team_id", "teamId", "team", "group_id", "group")) or team_id
        if not user_id:
            raise ReviewInputError("team membership is missing user_id")
        if not effective_team:
            raise ReviewInputError("team membership is missing team_id")
        membership_id = _first(value, ("membership_id", "id", "assignment_id"))
        try:
            window = window_from_grant(value)
        except (TypeError, ValueError) as exc:
            raise ReviewInputError(f"invalid membership validity: {exc}") from exc
        project_id = _extract_project(value, GLOBAL_PROJECT)
        return Membership(
            membership_id=str(membership_id)
            if membership_id is not None
            else f"membership:{effective_team}:{user_id}:{index}",
            user_id=str(user_id),
            team_id=str(effective_team),
            window=window,
            project_id=project_id,
        )

    # ------------------------------------------------------------------
    # Effective entitlements and conflicts
    # ------------------------------------------------------------------
    def _build_duty_instances(
        self,
        direct_grants: list[Grant],
        team_grants: list[Grant],
        memberships: list[Membership],
        roles: dict[str, Role],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        instances: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        membership_index: dict[tuple[str, str], list[Membership]] = {}
        for membership in memberships:
            membership_index.setdefault((membership.team_id, membership.user_id), []).append(membership)

        for grant in direct_grants:
            duties, role_errors = self._duties_for_grant(grant, roles)
            errors.extend(role_errors)
            for duty in duties:
                instances.append(
                    self._make_instance(
                        duty=duty,
                        grant=grant,
                        source_id=f"direct:{grant.grant_id}",
                        source_type="direct",
                        window=grant.window,
                        grant_window=grant.window,
                        membership=None,
                        project_id=grant.project_id,
                    )
                )

        for grant in team_grants:
            duties, role_errors = self._duties_for_grant(grant, roles)
            errors.extend(role_errors)
            # A team grant can explicitly name a user in some compact payloads.
            candidate_users: list[tuple[str, Optional[Membership]]] = []
            if grant.user_id:
                explicit_matches = membership_index.get((grant.team_id or "", grant.user_id), [])
                if explicit_matches:
                    candidate_users = [(grant.user_id, m) for m in explicit_matches]
                elif not memberships:
                    # Compact payloads may name the inheriting user directly
                    # without a separate membership roster.
                    candidate_users = [(grant.user_id, None)]
                # If a roster was supplied and has no match, do not invent an
                # inheritance source.
            else:
                matches = [
                    m
                    for (team_id, _user_id), ms in membership_index.items()
                    if team_id == grant.team_id
                    for m in ms
                ]
                candidate_users = [(m.user_id, m) for m in matches]

            # If no roster was supplied but the grant itself has user_id, the
            # branch above handles it.  If a roster was supplied and has no
            # match, the team grant yields no effective entitlement.
            for user_id, membership in candidate_users:
                if membership is not None:
                    window = grant.window.intersection(membership.window)
                    if window is None:
                        continue
                    project_id = _combine_projects(grant.project_id, membership.project_id)
                    if project_id is None:
                        continue
                    membership_id = membership.membership_id
                    membership_window = membership.window
                else:
                    window = grant.window
                    project_id = grant.project_id
                    membership_id = None
                    membership_window = None

                source_id = (
                    f"team:{grant.grant_id}:{grant.team_id}:{membership_id or user_id}:{user_id}"
                )
                # dataclasses Grant user may be None for team grant; make a
                # shallow per-user source dictionary rather than mutating it.
                user_grant = Grant(**{**grant.__dict__, "user_id": user_id})
                for duty in duties:
                    instances.append(
                        self._make_instance(
                            duty=duty,
                            grant=user_grant,
                            source_id=source_id,
                            source_type="team",
                            window=window,
                            grant_window=grant.window,
                            membership=membership,
                            membership_id=membership_id,
                            membership_window=membership_window,
                            project_id=project_id,
                        )
                    )

        # Stable ordering makes reports and tests deterministic.
        instances.sort(
            key=lambda x: (
                x.get("user_id") or "",
                x.get("project_id") or "",
                x.get("duty") or "",
                x.get("source_id") or "",
            )
        )
        return instances, errors

    def _duties_for_grant(
        self, grant: Grant, roles: dict[str, Role]
    ) -> tuple[list[str], list[dict[str, Any]]]:
        raw_duties: list[Any] = []
        role_ids = grant.role_ids or ([grant.role_id] if grant.role_id is not None else [])
        if grant.duties is not None:
            raw_duties = list(grant.duties)
        elif role_ids:
            for role_id in role_ids:
                resolved, missing = self._resolve_role_duties(role_id, roles, set())
                if missing:
                    return [], [
                        {
                            "code": "UNKNOWN_ROLE",
                            "message": f"role {role_id!r} is not defined; cannot verify duties",
                            "role_id": role_id,
                            "grant_id": grant.grant_id,
                        }
                    ]
                raw_duties.extend(resolved)
        else:
            return [], [
                {
                    "code": "MISSING_ROLE_OR_DUTIES",
                    "message": "grant has neither role_id nor explicit duties",
                    "grant_id": grant.grant_id,
                }
            ]

        canonical: list[str] = []
        for duty in raw_duties:
            if isinstance(duty, dict):
                duty = _first(duty, ("id", "code", "name", "duty", "permission", "action"))
            if duty is None:
                continue
            canonical.append(self.canonicalize_duty(str(duty)))
        return sorted(set(canonical)), []

    def _resolve_role_duties(
        self, role_id: str, roles: dict[str, Role], seen: set[str]
    ) -> tuple[list[str], list[str]]:
        if role_id in seen:
            raise ReviewInputError(f"circular role inclusion involving {role_id!r}")
        role = roles.get(role_id)
        if role is None:
            return [], [role_id]

        seen = set(seen)
        seen.add(role_id)
        duties = list(role.duties)
        for included in role.includes:
            included_duties, missing = self._resolve_role_duties(included, roles, seen)
            if missing:
                return [], missing
            duties.extend(included_duties)
        return duties, []

    def canonicalize_duty(self, duty: str) -> str:
        normalized = _normalize_duty_name(duty)
        if normalized in self.duty_aliases:
            return self.duty_aliases[normalized]
        # Also try the unnormalized exact text for Chinese labels.
        if duty.strip() in self.duty_aliases:
            return self.duty_aliases[duty.strip()]
        return duty

    def _make_instance(
        self,
        *,
        duty: str,
        grant: Grant,
        source_id: str,
        source_type: str,
        window: TimeWindow,
        grant_window: TimeWindow,
        membership: Optional[Membership],
        project_id: str,
        membership_id: Optional[str] = None,
        membership_window: Optional[TimeWindow] = None,
    ) -> dict[str, Any]:
        return {
            "duty": duty,
            "source_instance_id": f"{source_id}:{duty}",
            "source_id": source_id,
            "grant_id": grant.grant_id,
            "batch_item_id": grant.batch_item_id,
            "in_batch": bool(grant.in_batch),
            "user_id": grant.user_id,
            "project_id": project_id,
            "role_id": grant.role_id,
            "role_ids": grant.role_ids or ([grant.role_id] if grant.role_id else []),
            "source_type": source_type,
            "type": source_type,
            "team_id": grant.team_id,
            "membership_id": membership_id or (membership.membership_id if membership else None),
            "validity": window.to_dict(),
            "valid_from": window.to_dict()["start"],
            "valid_to": window.to_dict()["end"],
            "start_inclusive": window.start_inclusive,
            "end_inclusive": window.end_inclusive,
            "grant_validity": grant_window.to_dict(),
            "membership_validity": membership_window.to_dict() if membership_window else (membership.window.to_dict() if membership else None),
        }

    def _find_conflicts(self, instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        for rule in self.rules:
            first = [i for i in instances if i["duty"] in rule.first_duties]
            second = [i for i in instances if i["duty"] in rule.second_duties]

            grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
            for a in first:
                for b in second:
                    if a["user_id"] != b["user_id"]:
                        continue
                    project_id = _resolve_conflict_project(a["project_id"], b["project_id"])
                    if project_id is None:
                        continue
                    window_a = _instance_window(a)
                    window_b = _instance_window(b)
                    overlap = window_a.intersection(window_b)
                    if overlap is None:
                        continue

                    key = (rule.rule_id, a["user_id"], project_id)
                    group = grouped.setdefault(
                        key,
                        {
                            "rule_id": rule.rule_id,
                            "code": "SEGREGATION_OF_DUTY_CONFLICT",
                            "type": "segregation_of_duty_conflict",
                            "conflict_id": f"SOD:{rule.rule_id}:{a['user_id']}:{project_id}",
                            "user_id": a["user_id"],
                            "user": a["user_id"],
                            "project_id": project_id,
                            "project": project_id,
                            "scope": "global" if project_id == GLOBAL_PROJECT else "project",
                            "entry_duty": ENTER_DUTY if rule.first_label == "entry" else sorted(rule.first_duties)[0],
                            "approval_duty": APPROVE_DUTY if rule.second_label == "approval" else sorted(rule.second_duties)[0],
                            "duties": sorted(set(rule.first_duties | rule.second_duties)),
                            "_first_sources": {},
                            "_second_sources": {},
                            "overlaps": [],
                            "batch_item_ids": set(),
                            "in_batch": False,
                        },
                    )

                    group["_first_sources"][a["source_id"]] = _source_summary(a)
                    group["_second_sources"][b["source_id"]] = _source_summary(b)
                    if a.get("in_batch") or b.get("in_batch"):
                        group["in_batch"] = True
                    for item_id in (a.get("batch_item_id"), b.get("batch_item_id")):
                        if item_id is not None:
                            group["batch_item_ids"].add(str(item_id))

                    overlap_dict = overlap.to_dict()
                    group["overlaps"].append(
                        {
                            **overlap_dict,
                            "entry_source_id": a["source_id"],
                            "approval_source_id": b["source_id"],
                            "entry_source_instance_id": a["source_instance_id"],
                            "approval_source_instance_id": b["source_instance_id"],
                            "entry_grant_id": a["grant_id"],
                            "approval_grant_id": b["grant_id"],
                            "entry_source_type": a["source_type"],
                            "approval_source_type": b["source_type"],
                        }
                    )

            for group in grouped.values():
                first_sources = list(group.pop("_first_sources").values())
                second_sources = list(group.pop("_second_sources").values())
                overlaps = _dedupe_dicts(group["overlaps"])
                overlaps.sort(key=lambda x: (x.get("start") is None, x.get("start"), x.get("end") is None, x.get("end")))
                periods = _dedupe_dicts(
                    [
                        {
                            "start": o["start"],
                            "end": o["end"],
                            "start_inclusive": o["start_inclusive"],
                            "end_inclusive": o["end_inclusive"],
                        }
                        for o in overlaps
                    ]
                )
                periods.sort(key=lambda x: (x.get("start") is None, x.get("start"), x.get("end") is None, x.get("end")))

                duty_sources = {
                    group["entry_duty"]: first_sources,
                    group["approval_duty"]: second_sources,
                }
                group["entry_sources"] = first_sources
                group["approval_sources"] = second_sources
                group["enter_sources"] = first_sources
                group["approve_sources"] = second_sources
                group["duty_sources"] = duty_sources
                group["responsibilities"] = [
                    {"duty": group["entry_duty"], "label": "entry", "sources": first_sources},
                    {"duty": group["approval_duty"], "label": "approval", "sources": second_sources},
                ]
                group["sources"] = {"entry": first_sources, "approval": second_sources}
                group["overlap_periods"] = periods
                group["overlapping_periods"] = periods
                group["conflict_periods"] = periods
                group["conflicting_periods"] = periods
                group["batch_item_ids"] = sorted(group["batch_item_ids"])
                group["batch_relevant"] = bool(group["in_batch"])
                group["blocking"] = bool(group["in_batch"]) or self.block_on_unrelated_conflicts
                group["recommended_action"] = (
                    "Suspend this batch for manual segregation-of-duties review; "
                    "do not enable the approval duty until one effective source "
                    "is revoked or its validity no longer overlaps."
                )
                conflicts.append(group)

        conflicts.sort(key=lambda c: (c["user_id"], c["project_id"], c["rule_id"]))
        return conflicts


# ----------------------------------------------------------------------
# Module-level convenience API
# ----------------------------------------------------------------------
_default_reviewer: Optional[SodReviewer] = None


def _get_default_reviewer() -> SodReviewer:
    global _default_reviewer
    if _default_reviewer is None:
        _default_reviewer = SodReviewer()
    return _default_reviewer


def review_batch(payload: Any = None, **kwargs: Any) -> ReviewReport:
    """Review a permission batch using the default finance SoD policy."""

    return _get_default_reviewer().review(payload, **kwargs)


def review(payload: Any = None, **kwargs: Any) -> ReviewReport:
    """Alias for :func:`review_batch`."""

    return review_batch(payload, **kwargs)


def check_batch(payload: Any = None, **kwargs: Any) -> ReviewReport:
    """Alias for :func:`review_batch`."""

    return review_batch(payload, **kwargs)


def review_permission_batch(payload: Any = None, **kwargs: Any) -> ReviewReport:
    """Descriptive alias for :func:`review_batch`."""

    return review_batch(payload, **kwargs)


def review_request(payload: Any = None, **kwargs: Any) -> ReviewReport:
    """Service-style alias for :func:`review_batch`."""

    return review_batch(payload, **kwargs)


def evaluate_batch(payload: Any = None, **kwargs: Any) -> ReviewReport:
    """Alias for :func:`review_batch`."""

    return review_batch(payload, **kwargs)


def review_access(payload: Any = None, **kwargs: Any) -> ReviewReport:
    """Domain-language alias for :func:`review_batch`."""

    return review_batch(payload, **kwargs)


def review_permissions(payload: Any = None, **kwargs: Any) -> ReviewReport:
    """Domain-language alias for :func:`review_batch`."""

    return review_batch(payload, **kwargs)


def review_grants(payload: Any = None, **kwargs: Any) -> ReviewReport:
    """Domain-language alias for :func:`review_batch`."""

    return review_batch(payload, **kwargs)


# Common class names integrators may look for.
AccessReviewer = SodReviewer
PermissionReviewer = SodReviewer
SoDReviewer = SodReviewer


# ----------------------------------------------------------------------
# Plain normalization helpers
# ----------------------------------------------------------------------
def _build_alias_map(extra: dict[str, Iterable[str]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for alias in _ENTER_ALIASES:
        mapping[_normalize_duty_name(alias)] = ENTER_DUTY
        mapping[alias] = ENTER_DUTY
    for alias in _APPROVE_ALIASES:
        mapping[_normalize_duty_name(alias)] = APPROVE_DUTY
        mapping[alias] = APPROVE_DUTY
    for canonical, aliases in extra.items():
        mapping[_normalize_duty_name(canonical)] = canonical
        mapping[canonical] = canonical
        for alias in aliases:
            mapping[_normalize_duty_name(str(alias))] = canonical
            mapping[str(alias)] = canonical
    mapping[ENTER_DUTY] = ENTER_DUTY
    mapping[APPROVE_DUTY] = APPROVE_DUTY
    return mapping


def _normalize_duty_name(value: str) -> str:
    text = str(value).strip().lower()
    # Preserve Chinese text and colons; normalize common separators.
    return text.replace("-", "_").replace(" ", "_")


def _first(data: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _first_str(data: dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    value = _first(data, keys)
    return str(value) if value is not None else None


def _first_list(data: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    for key in keys:
        if key in data and data[key] is not None:
            return _as_list(data[key])
    return []


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _extract_user_id(item: dict[str, Any]) -> Optional[str]:
    value = _first(
        item,
        (
            "user_id",
            "userId",
            "user",
            "subject_id",
            "subject",
            "principal_id",
            "employee_id",
            "username",
            "account",
            "account_id",
        ),
    )
    if isinstance(value, dict):
        value = _first(value, ("id", "user_id", "principal_id", "username", "name"))
    return str(value) if value is not None else None


def _has_project_key(item: dict[str, Any]) -> bool:
    return any(
        key in item
        for key in (
            "project_id",
            "projectId",
            "project",
            "project_code",
            "scope",
            "project_scope",
            "resource_scope",
        )
    )


def _extract_project(item: dict[str, Any], default: Optional[str]) -> str:
    value = _first(
        item,
        ("project_id", "projectId", "project", "project_code", "scope_id", "project_scope", "resource_scope"),
    )
    if value is None:
        scope = item.get("scope")
        if scope is not None:
            value = scope
    return _normalize_project(value, default if default is not None else GLOBAL_PROJECT)


def _normalize_project(value: Any, default: Optional[str] = GLOBAL_PROJECT) -> str:
    if value is None:
        return default if default is not None else GLOBAL_PROJECT
    if isinstance(value, dict):
        scope_type = str(_first(value, ("type", "scope_type", "kind"), default="")).lower()
        nested = _first(value, ("project_id", "id", "code", "project_code", "name"))
        if nested is None:
            if scope_type in ("global", "all", "organization", "tenant", "*"):
                return GLOBAL_PROJECT
            return GLOBAL_PROJECT
        value = nested
    if isinstance(value, (list, tuple, set)):
        # Multiple scopes are expanded by callers in more advanced payloads;
        # for scalar normalization use the first concrete project.
        value = next(iter(value), None)
        if value is None:
            return GLOBAL_PROJECT
    text = str(value).strip()
    if not text or text.lower() in ("*", "all", "global", "any", "organization", "tenant"):
        return GLOBAL_PROJECT
    return text


def _normalize_source_type(value: str) -> str:
    text = str(value).strip().lower()
    if text in ("personal", "user", "direct", "individual"):
        return "direct"
    if text in ("team", "group", "role_group", "inherited"):
        return "team"
    return text


def _revocation_matches_grant(rev: Revocation, grant: Grant) -> bool:
    if rev.target_grant_id and rev.target_grant_id == grant.grant_id:
        return True
    if rev.user_id is not None and rev.user_id != grant.user_id:
        return False
    grant_role_ids = grant.role_ids or ([grant.role_id] if grant.role_id is not None else [])
    if rev.role_id is not None and rev.role_id not in grant_role_ids:
        return False
    if rev.project_id is not None and rev.project_id != grant.project_id:
        # A global selector can match a concrete grant, but a concrete selector
        # does not match a different project.
        if not (rev.project_id == GLOBAL_PROJECT):
            return False
    if rev.team_id is not None and rev.team_id != grant.team_id:
        return False
    if rev.source_type is not None and rev.source_type != grant.source_type:
        return False
    return rev.has_selector()


# Add method to Revocation after dataclass definition.
def _revocation_has_selector(self: Revocation) -> bool:
    return any(
        value is not None
        for value in (self.user_id, self.role_id, self.project_id, self.source_type, self.team_id)
    )


Revocation.has_selector = _revocation_has_selector  # type: ignore[attr-defined]


def _dedupe_grants_by_id(grants: list[Grant]) -> list[Grant]:
    result: dict[str, Grant] = {}
    for grant in grants:
        result[grant.grant_id] = grant
    return list(result.values())


def _combine_projects(grant_project: str, membership_project: str) -> Optional[str]:
    if membership_project == GLOBAL_PROJECT:
        return grant_project
    if grant_project == GLOBAL_PROJECT:
        return membership_project
    if grant_project == membership_project:
        return grant_project
    return None


def _resolve_conflict_project(left: str, right: str) -> Optional[str]:
    if left == right:
        return left
    if left == GLOBAL_PROJECT:
        return right
    if right == GLOBAL_PROJECT:
        return left
    return None


def _instance_window(instance: dict[str, Any]) -> TimeWindow:
    return TimeWindow.from_value(instance["validity"])


def _source_summary(instance: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "source_id",
        "grant_id",
        "batch_item_id",
        "in_batch",
        "type",
        "source_type",
        "user_id",
        "project_id",
        "role_id",
        "role_ids",
        "team_id",
        "membership_id",
        "validity",
        "valid_from",
        "valid_to",
        "grant_validity",
        "membership_validity",
    )
    return {key: instance[key] for key in keys if key in instance}


def _summarize_instances(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for instance in instances:
        key = (instance["source_instance_id"], instance["duty"])
        if key in seen:
            continue
        seen.add(key)
        result.append({"duty": instance["duty"], **_source_summary(instance)})
    return result


def _grant_summary(grant: Grant) -> dict[str, Any]:
    return {
        "grant_id": grant.grant_id,
        "batch_item_id": grant.batch_item_id,
        "user_id": grant.user_id,
        "role_id": grant.role_id,
        "role_ids": grant.role_ids or ([grant.role_id] if grant.role_id else []),
        "project_id": grant.project_id,
        "source_type": grant.source_type,
        "team_id": grant.team_id,
        "in_batch": grant.in_batch,
        "validity": grant.window.to_dict(),
        "replaces": grant.replaces,
    }


def _safe_id(item: dict[str, Any]) -> str:
    value = _first(item, ("id", "grant_id", "request_id", "batch_item_id"))
    return str(value) if value is not None else "unidentified"


def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in items:
        key = repr(sorted(item.items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
