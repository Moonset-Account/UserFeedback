"""Static release checks over a parsed :class:`~release_check.model.Spec`.

For every candidate implementation the checker walks the interfaces whose
contract binds it (its declared ``implements`` plus all ancestors) and, for
every method reachable through those entries, compares the implementation
against the contract *per caller identity*.

A violation is recorded, precise down to entry and identity, when:

``missing-method``     an identity may legally call an entry but the
                       implementation does not provide the method at all;
``rejects-identity``   the method exists but the implementation refuses an
                       identity the entry admits;
``overexposure``       the implementation may return fields outside the
                       visibility range the entry grants that identity.

Returning a *subset* of the allowed fields is not a violation: the result
still falls inside the range.  Only fields beyond the range are rejected.
Passing ``exact=True`` additionally flags a granted field that never comes
back (``reduced-visibility``), which is a regression signal, not a range
violation.

Diamond inheritance
-------------------
When an entry inherits the same method over several branches, all branch
contracts bind at once.  :meth:`Spec.resolve_method` intersects them, so an
identity is callable only where every branch admits it and may observe only
the common fields.  Every finding carries the ``origins`` that produced the
limit, so a conflict points back at the exact entry and identity constraining
it.

Undecided entries
-----------------
An entry that inherits from a public interface exposing the method without any
declaration is *pending*: the effective contract is unknown, so it is reported
as undecided and neither accepted nor rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Implementation, Interface, Spec

__all__ = [
    "Finding",
    "PendingEntry",
    "Report",
    "check",
    "check_substitution",
]


@dataclass(frozen=True)
class Finding:
    """One violation, precise down to entry and identity."""

    code: str
    implementation: str
    interface: str
    method: str
    identity: str
    message: str
    allowed: tuple[str, ...] = ()
    actual: tuple[str, ...] = ()
    origins: tuple[str, ...] = ()

    @property
    def entry(self) -> str:
        return f"{self.interface}.{self.method}"

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "implementation": self.implementation,
            "interface": self.interface,
            "method": self.method,
            "entry": self.entry,
            "identity": self.identity,
            "message": self.message,
            "allowed": list(self.allowed),
            "actual": list(self.actual),
            "origins": list(self.origins),
        }


@dataclass(frozen=True)
class PendingEntry:
    """An entry that cannot be decided because a public interface is silent."""

    interface: str
    method: str
    missing: tuple[str, ...]

    @property
    def entry(self) -> str:
        return f"{self.interface}.{self.method}"

    def to_dict(self) -> dict[str, object]:
        return {
            "interface": self.interface,
            "method": self.method,
            "entry": self.entry,
            "missing": list(self.missing),
        }


@dataclass
class Report:
    """Result of a full release check."""

    findings: list[Finding] = field(default_factory=list)
    pending: list[PendingEntry] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.findings:
            return "rejected"
        if self.pending:
            return "pending"
        return "ready"

    @property
    def ok(self) -> bool:
        """True only when the release is fully decidable and clean."""

        return self.status == "ready"

    def sort(self) -> None:
        self.findings.sort(
            key=lambda item: (
                item.implementation,
                item.interface,
                item.method,
                item.identity,
                item.code,
            )
        )
        self.pending.sort(key=lambda item: (item.interface, item.method))

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "findings": [item.to_dict() for item in self.findings],
            "pending": [item.to_dict() for item in self.pending],
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bound_interfaces(spec: Spec, implementation: Implementation) -> list[str]:
    """Public interfaces whose contracts bind ``implementation``.

    That is every public interface the implementation declares plus all of
    their ancestors: a client holding an ancestor reference can still call the
    same object, so an ancestor contract binds just as much.  Non-public
    interfaces are skipped: no outside caller can hold one, so a call through
    them is not a call the release has to preserve.
    """

    names: set[str] = set()
    for declared in implementation.implements:
        names.update(spec.closure(declared))
    return sorted(name for name in names if spec.interfaces[name].public)


def _public_interfaces(spec: Spec) -> list[str]:
    return sorted(name for name, node in spec.interfaces.items() if node.public)


def _check_identity(
    implementation: Implementation,
    interface: Interface,
    method: str,
    identity: str,
    allowed: tuple[str, ...],
    origins: tuple[str, ...],
    findings: list[Finding],
    *,
    exact: bool = False,
) -> None:
    body = implementation.methods.get(method)
    if body is None:
        findings.append(
            Finding(
                code="missing-method",
                implementation=implementation.name,
                interface=interface.name,
                method=method,
                identity=identity,
                message=(
                    f"{interface.name}.{method} admits {identity!r} "
                    f"but {implementation.name!r} does not implement {method!r}"
                ),
                allowed=allowed,
                origins=origins,
            )
        )
        return

    if identity not in body.accepts:
        findings.append(
            Finding(
                code="rejects-identity",
                implementation=implementation.name,
                interface=interface.name,
                method=method,
                identity=identity,
                message=(
                    f"{interface.name}.{method} admits {identity!r} but "
                    f"{implementation.name!r} rejects that identity"
                ),
                allowed=allowed,
                origins=origins,
            )
        )
        return

    actual = tuple(sorted(body.return_fields(identity)))
    excess = sorted(set(actual) - set(allowed))
    if excess:
        findings.append(
            Finding(
                code="overexposure",
                implementation=implementation.name,
                interface=interface.name,
                method=method,
                identity=identity,
                message=(
                    f"{implementation.name!r} may return "
                    f"{', '.join(excess)} to {identity!r} through "
                    f"{interface.name}.{method}, outside the visible range"
                ),
                allowed=allowed,
                actual=actual,
                origins=origins,
            )
        )
        return

    if not exact:
        # A result that stays inside the range is legal: the declaration is a
        # ceiling, not a promise of every field.  Only strict mode objects.
        return

    dropped = sorted(set(allowed) - set(actual))
    if dropped:
        findings.append(
            Finding(
                code="reduced-visibility",
                implementation=implementation.name,
                interface=interface.name,
                method=method,
                identity=identity,
                message=(
                    f"{implementation.name!r} returns no "
                    f"{', '.join(dropped)} to {identity!r} although "
                    f"{interface.name}.{method} grants it"
                ),
                allowed=allowed,
                actual=actual,
                origins=origins,
            )
        )


def _collect_pending(
    spec: Spec, report: Report, interfaces: list[str] | None = None
) -> None:
    """Record every public entry whose contract is undecided.

    Undecidedness is a property of the interface graph, not of whichever
    implementation happens to be scanned, so it is collected independently of
    the candidates.  ``interfaces`` narrows the collection to a subgraph; by
    default the whole graph is scanned.
    """

    targets = _public_interfaces(spec) if interfaces is None else interfaces
    seen = {(entry.interface, entry.method) for entry in report.pending}
    for interface_name in targets:
        if not spec.interfaces[interface_name].public:
            continue
        for method in sorted(spec.exposed_methods(interface_name)):
            if (interface_name, method) in seen:
                continue
            resolution = spec.resolve_method(interface_name, method)
            if resolution.pending:
                seen.add((interface_name, method))
                report.pending.append(
                    PendingEntry(interface_name, method, resolution.missing)
                )


def _check_implementation(
    spec: Spec,
    implementation: Implementation,
    interfaces: list[str],
    report: Report,
    *,
    exact: bool = False,
) -> None:
    for interface_name in interfaces:
        interface = spec.interfaces[interface_name]
        for method in sorted(spec.exposed_methods(interface_name)):
            resolution = spec.resolve_method(interface_name, method)
            if resolution.pending:
                # Undecided: reported by _collect_pending, never judged here.
                continue
            for identity in sorted(resolution.resolved):
                _check_identity(
                    implementation,
                    interface,
                    method,
                    identity,
                    resolution.resolved[identity],
                    resolution.origins[identity],
                    report.findings,
                    exact=exact,
                )


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------


def check(spec: Spec, *, exact: bool = False) -> Report:
    """Run every check over ``spec`` and return a sorted :class:`Report`.

    ``exact`` additionally requires each admitted identity to receive every
    field the entry grants it.  The default treats the declaration as a
    ceiling, which is what "the result must stay inside the visible range"
    asks for; strict mode is for callers that also want to catch regressions
    where a field silently disappears.
    """

    report = Report()
    _collect_pending(spec, report)
    for implementation in sorted(spec.implementations, key=lambda item: item.name):
        _check_implementation(
            spec,
            implementation,
            _bound_interfaces(spec, implementation),
            report,
            exact=exact,
        )
    report.sort()
    return report


def check_substitution(
    spec: Spec, interface_name: str, replacement: str, *, exact: bool = False
) -> Report:
    """Check ``replacement`` against the contract reachable through one entry.

    A call that is legal through ``interface_name`` today must still succeed
    after the implementation is swapped, and its result must stay inside the
    range the entry grants that identity.  Only the entries reachable through
    ``interface_name`` are evaluated, which is the narrow question a rollout
    of a single implementation asks.
    """

    report = Report()
    if interface_name not in spec.interfaces:
        raise KeyError(f"unknown interface {interface_name!r}")
    candidates = {item.name: item for item in spec.implementations}
    if replacement not in candidates:
        raise KeyError(f"unknown implementation {replacement!r}")

    reachable = [
        name for name in sorted(spec.closure(interface_name))
        if spec.interfaces[name].public
    ]
    _collect_pending(spec, report, reachable)
    _check_implementation(
        spec, candidates[replacement], reachable, report, exact=exact
    )
    report.sort()
    return report
