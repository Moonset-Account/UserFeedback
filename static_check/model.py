"""Data models for the error-flow disclosure checker.

The models are intentionally plain and JSON-friendly.  They describe the
symbolic error values and the reports produced by :mod:`static_check.analysis`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Literal

Severity = Literal["error", "warning"]
ResponseKind = Literal["error", "normal"]
StateKind = Literal["error", "normal"]


@dataclass(frozen=True)
class DiagnosticUse:
    """One concrete propagation of a diagnostic identifier.

    ``diagnostic_id`` is the declarable identifier.  ``source`` is the declared
    or inferred source label, while ``origin_node`` is the graph node that
    introduced this use.  ``route`` records the nodes through which it has
    propagated, including its origin and the current node.
    """

    diagnostic_id: str
    source: str
    origin_node: str
    route: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        """Short alias used by many specifications."""

        return self.diagnostic_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.diagnostic_id,
            "source": self.source,
            "origin_node": self.origin_node,
            "route": list(self.route),
            "locations": list(self.locations),
        }


@dataclass(frozen=True)
class ErrorValue:
    """A symbolic error value.

    ``cause`` represents exception wrapping.  ``members`` represents an
    aggregate/multi-error.  The tree is retained during analysis so that a
    public outer code cannot act as justification for an undisclosed inner
    context.
    """

    code: str | None
    diagnostics: tuple[DiagnosticUse, ...] = ()
    cause: "ErrorValue | None" = None
    members: tuple["ErrorValue", ...] = ()
    origin_node: str | None = None
    aggregate: bool = False
    label: str | None = None
    code_declared_public: bool = False

    def all_diagnostics(self, recursive: bool = True) -> tuple[DiagnosticUse, ...]:
        values = list(self.diagnostics)
        if recursive:
            if self.cause is not None:
                values.extend(self.cause.all_diagnostics(recursive=True))
            for member in self.members:
                values.extend(member.all_diagnostics(recursive=True))
        # Preserve order while de-duplicating the exact same propagation use.
        seen: set[DiagnosticUse] = set()
        result: list[DiagnosticUse] = []
        for item in values:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return tuple(result)

    def all_codes(self, recursive: bool = True) -> tuple[str, ...]:
        result: list[str] = []
        if self.code is not None:
            result.append(self.code)
        if recursive:
            if self.cause is not None:
                result.extend(self.cause.all_codes(True))
            for member in self.members:
                result.extend(member.all_codes(True))
        return tuple(result)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "cause": self.cause.to_dict() if self.cause else None,
            "members": [item.to_dict() for item in self.members],
            "origin_node": self.origin_node,
            "aggregate": self.aggregate,
            "label": self.label,
            "code_declared_public": self.code_declared_public,
        }


@dataclass(frozen=True)
class PropState:
    """State carried by a graph edge or emitted by a node."""

    kind: StateKind
    error: ErrorValue | None = None
    diagnostics: tuple[DiagnosticUse, ...] = ()
    route: tuple[str, ...] = ()

    @staticmethod
    def normal(
        diagnostics: tuple[DiagnosticUse, ...] = (),
        route: tuple[str, ...] = (),
    ) -> "PropState":
        return PropState(kind="normal", diagnostics=tuple(diagnostics), route=route)

    @staticmethod
    def error_state(error: ErrorValue, route: tuple[str, ...] = ()) -> "PropState":
        return PropState(kind="error", error=error, route=route)


@dataclass(frozen=True)
class Finding:
    """A single static disclosure or graph-integrity finding."""

    code: str
    message: str
    severity: Severity = "error"
    exit_id: str | None = None
    node_id: str | None = None
    diagnostic_id: str | None = None
    error_code: str | None = None
    field_name: str | None = None
    caller: str | None = None
    source: str | None = None
    route: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["route"] = list(self.route)
        return data


@dataclass(frozen=True)
class DiagnosticReport:
    diagnostic: DiagnosticUse
    allowed: bool
    reasons: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return self.diagnostic.diagnostic_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnostic": self.diagnostic.to_dict(),
            "allowed": self.allowed,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ErrorReport:
    """Disclosure result for one :class:`ErrorValue` tree."""

    code: str | None
    code_allowed: bool
    diagnostics: tuple[DiagnosticReport, ...] = ()
    cause: "ErrorReport | None" = None
    members: tuple["ErrorReport", ...] = ()
    aggregate: bool = False
    origin_node: str | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def allowed_diagnostics(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.diagnostics if item.allowed)

    @property
    def blocked_diagnostics(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.diagnostics if not item.allowed)

    @property
    def admissible(self) -> bool:
        """True when this complete error member can pass unchanged."""

        if not self.code_allowed:
            return False
        if any(not item.allowed for item in self.diagnostics):
            return False
        if self.cause is not None and not self.cause.admissible:
            return False
        if self.aggregate:
            # Every retained aggregate member must pass independently.
            return all(member.admissible for member in self.members)
        return True

    @property
    def independently_passable(self) -> bool:
        """Alias emphasizing the aggregate-member rule."""

        return self.admissible

    @property
    def retained_members(self) -> tuple["ErrorReport", ...]:
        if not self.aggregate:
            return ()
        return tuple(member for member in self.members if member.admissible)

    def safe_projection(self) -> dict[str, Any] | None:
        """Project the error using only independently allowed content.

        For an aggregate, non-admissible members are omitted as whole members,
        matching the rule that only independently passing members may be
        retained.  For ordinary errors, blocked diagnostic leaves are omitted.
        A public outer code is retained; a blocked code is replaced by ``None``.
        """

        code = self.code if self.code_allowed else None
        if self.aggregate:
            kept = [
                member.safe_projection()
                for member in self.members
                if member.admissible
            ]
            if code is None and not kept:
                return None
            result: dict[str, Any] = {"aggregate": True, "members": kept}
            if code is not None:
                result["code"] = code
            return result

        diag_ids = [item.id for item in self.diagnostics if item.allowed]
        cause = self.cause.safe_projection() if self.cause is not None else None
        if code is None and not diag_ids and cause is None:
            return None
        result = {"code": code, "diagnostics": diag_ids}
        if self.origin_node is not None:
            result["origin_node"] = self.origin_node
        if cause is not None:
            result["cause"] = cause
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "code_allowed": self.code_allowed,
            "admissible": self.admissible,
            "allowed_diagnostics": list(self.allowed_diagnostics),
            "blocked_diagnostics": list(self.blocked_diagnostics),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "cause": self.cause.to_dict() if self.cause else None,
            "members": [item.to_dict() for item in self.members],
            "retained_member_count": len(self.retained_members),
            "aggregate": self.aggregate,
            "origin_node": self.origin_node,
            "findings": [item.to_dict() for item in self.findings],
            "safe_projection": self.safe_projection(),
        }


@dataclass(frozen=True)
class FieldReport:
    name: str
    licensed: bool
    diagnostics: tuple[DiagnosticReport, ...] = ()
    missing_sources: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()
    safe_value: Any = None

    @property
    def allowed_diagnostics(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.diagnostics if item.allowed)

    @property
    def blocked_diagnostics(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.diagnostics if not item.allowed)

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "licensed": self.licensed,
            "ok": self.ok,
            "allowed_diagnostics": list(self.allowed_diagnostics),
            "blocked_diagnostics": list(self.blocked_diagnostics),
            "missing_sources": list(self.missing_sources),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "findings": [item.to_dict() for item in self.findings],
            "safe_value": self.safe_value,
        }


@dataclass(frozen=True)
class ResponseScenario:
    """One possible propagated state arriving at a response exit."""

    kind: ResponseKind
    route: tuple[str, ...]
    error: ErrorReport | None = None
    fields: tuple[FieldReport, ...] = ()
    safe_response: dict[str, Any] = field(default_factory=dict)
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        if any(item.severity == "error" for item in self.findings):
            return False
        if self.error is not None and any(
            item.severity == "error" for item in self.error.findings
        ):
            return False
        return all(field_report.ok for field_report in self.fields)

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity == "error")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity == "warning")

    @property
    def allowed_diagnostics(self) -> tuple[str, ...]:
        result: list[str] = []
        if self.error is not None:
            result.extend(self.error.allowed_diagnostics)
        for field_report in self.fields:
            result.extend(field_report.allowed_diagnostics)
        return tuple(dict.fromkeys(result))

    @property
    def blocked_diagnostics(self) -> tuple[str, ...]:
        result: list[str] = []
        if self.error is not None:
            result.extend(self.error.blocked_diagnostics)
        for field_report in self.fields:
            result.extend(field_report.blocked_diagnostics)
        return tuple(dict.fromkeys(result))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "route": list(self.route),
            "ok": self.ok,
            "error": self.error.to_dict() if self.error else None,
            "fields": {item.name: item.to_dict() for item in self.fields},
            "safe_response": self.safe_response,
            "findings": [item.to_dict() for item in self.findings],
            "allowed_diagnostics": list(self.allowed_diagnostics),
            "blocked_diagnostics": list(self.blocked_diagnostics),
        }


@dataclass(frozen=True)
class ExitReport:
    exit_id: str
    reachable: bool
    kind: ResponseKind | None = None
    scenarios: tuple[ResponseScenario, ...] = ()
    permitted_fields: tuple[str, ...] | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            self.reachable
            and not any(item.severity == "error" for item in self.findings)
            and all(scenario.ok for scenario in self.scenarios)
        )

    @property
    def allowed_diagnostics(self) -> tuple[str, ...]:
        values: list[str] = []
        for scenario in self.scenarios:
            values.extend(scenario.allowed_diagnostics)
        return tuple(dict.fromkeys(values))

    @property
    def blocked_diagnostics(self) -> tuple[str, ...]:
        values: list[str] = []
        for scenario in self.scenarios:
            values.extend(scenario.blocked_diagnostics)
        return tuple(dict.fromkeys(values))

    @property
    def safe_responses(self) -> tuple[dict[str, Any], ...]:
        return tuple(scenario.safe_response for scenario in self.scenarios)

    @property
    def safe_response(self) -> dict[str, Any] | None:
        """Convenience for the common one-scenario case."""

        responses = self.safe_responses
        return responses[0] if len(responses) == 1 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_id": self.exit_id,
            "reachable": self.reachable,
            "kind": self.kind,
            "ok": self.ok,
            "permitted_fields": list(self.permitted_fields)
            if self.permitted_fields is not None
            else None,
            "scenarios": [item.to_dict() for item in self.scenarios],
            "safe_responses": list(self.safe_responses),
            "safe_response": self.safe_response,
            "allowed_diagnostics": list(self.allowed_diagnostics),
            "blocked_diagnostics": list(self.blocked_diagnostics),
            "findings": [item.to_dict() for item in self.findings],
        }


@dataclass(frozen=True)
class ReverseSlice:
    """Semantic backward slice for one response exit."""

    exit_id: str
    nodes: tuple[str, ...]
    diagnostics: tuple[DiagnosticUse, ...]
    allowed_diagnostics: tuple[DiagnosticUse, ...]
    blocked_diagnostics: tuple[DiagnosticUse, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_id": self.exit_id,
            "nodes": list(self.nodes),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "allowed_diagnostics": [item.to_dict() for item in self.allowed_diagnostics],
            "blocked_diagnostics": [item.to_dict() for item in self.blocked_diagnostics],
        }


@dataclass
class AuditReport:
    caller: str | None
    exits: list[ExitReport]
    findings: list[Finding] = field(default_factory=list)
    graph_errors: list[Finding] = field(default_factory=list)

    @property
    def reachable_exits(self) -> list[ExitReport]:
        return [exit_report for exit_report in self.exits if exit_report.reachable]

    @property
    def allowed_diagnostics(self) -> tuple[str, ...]:
        values: list[str] = []
        for exit_report in self.reachable_exits:
            values.extend(exit_report.allowed_diagnostics)
        return tuple(dict.fromkeys(values))

    @property
    def blocked_diagnostics(self) -> tuple[str, ...]:
        values: list[str] = []
        for exit_report in self.reachable_exits:
            values.extend(exit_report.blocked_diagnostics)
        return tuple(dict.fromkeys(values))

    @property
    def public_diagnostics(self) -> tuple[str, ...]:
        return self.allowed_diagnostics

    @property
    def ok(self) -> bool:
        graph_has_error = any(item.severity == "error" for item in self.graph_errors)
        return not graph_has_error and all(
            item.ok for item in self.reachable_exits
        )

    # Common aliases for callers/tests expecting a validation-style result.
    @property
    def valid(self) -> bool:
        return self.ok

    @property
    def passed(self) -> bool:
        return self.ok

    @property
    def violations(self) -> list[Finding]:
        return [
            finding
            for finding in self.all_findings
            if finding.severity == "error"
        ]

    @property
    def errors(self) -> list[Finding]:
        return self.violations

    @property
    def all_findings(self) -> list[Finding]:
        # The analyzer puts every canonical finding here.  Nested report
        # objects also retain local finding tuples for convenient navigation.
        findings = list(self.graph_errors)
        findings.extend(self.findings)
        result: list[Finding] = []
        for item in findings:
            if item not in result:
                result.append(item)
        return result

    @property
    def safe_responses(self) -> dict[str, list[dict[str, Any]]]:
        return {
            exit_report.exit_id: list(exit_report.safe_responses)
            for exit_report in self.reachable_exits
        }

    def exit(self, exit_id: str) -> ExitReport:
        for item in self.exits:
            if item.exit_id == exit_id:
                return item
        raise KeyError(exit_id)

    def findings_for(self, exit_id: str) -> list[Finding]:
        return [item for item in self.all_findings if item.exit_id == exit_id]

    def codes(self) -> set[str]:
        return {item.code for item in self.all_findings}

    def to_dict(self) -> dict[str, Any]:
        return {
            "caller": self.caller,
            "ok": self.ok,
            "allowed_diagnostics": list(self.allowed_diagnostics),
            "blocked_diagnostics": list(self.blocked_diagnostics),
            "exits": [item.to_dict() for item in self.exits],
            "safe_responses": self.safe_responses,
            "findings": [item.to_dict() for item in self.all_findings],
            "violation_codes": sorted(self.codes()),
        }
