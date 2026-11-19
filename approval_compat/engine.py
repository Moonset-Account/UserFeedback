"""Does a fixed forwarding route let a legacy sender mint consent?

The rule that matters
---------------------
Field *numbers* survive every hop that does not actively drop them.  So when a
number is ordinary (legacy-writable) in the origin version and approval-bearing
in the destination version, whatever the legacy sender wrote there is read as
consent unless some hop severs those exact bytes.  "Unknown field" handling is
not a boundary; it is a conveyor belt.

A destination approval number is accepted only when the value that arrives is

* absent, and the caller is told the gate is never fed,
* rebuilt from a *trusted* input, with the legacy bytes never forwarded, or
* written by a version that is itself an authorized approval sender (reported
  as a pre-existing channel, not as a new escalation).

Everything else blocks the release.  Every finding names the version that first
gave the number its approval meaning, the origin of the bytes, and each hop
that touched them.

``RouteReport.ok`` therefore means "this route opens no new path from a legacy
sender's ordinary bytes to an approval decision".

Hop knowledge model
-------------------
A hop knows the numbers its own schema declares (``known_numbers``); it
re-serialises them, so the trust level is unchanged.  Every other number is an
unknown field to that hop: forwarded byte-for-byte by default, or severed when
``unknown_bytes="dropped"``.  ``known_numbers=None`` means the hop knows no
field at all -- the usual opaque relay.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Iterable, List, Optional, Sequence, Tuple

from .model import APPROVAL_MEANINGS, HopTransform, MessageVersion, Route, SchemaError

# Finding severities.
BLOCK = "block"
WARN = "warn"
INFO = "info"

# Simulation states for one field number.
ABSENT = "absent"                            # no bytes arrive
SENDER_BYTES = "sender_bytes"                # traceable to a legacy sender
HOP_WRITTEN = "hop_written_unauthenticated"  # rewritten without authority
TRUSTED = "trusted"                          # rebuilt from a trusted input
DUPLICATE = "duplicate_carrier"              # legacy bytes and a new value coexist

_SOURCE_LABEL = {
    "trusted": "trusted input",
    "trusted_input": "trusted input",
    "untrusted": "untrusted input",
}


@dataclass(frozen=True)
class SourceRef:
    """One version's declaration of one field, cited in diagnostics."""

    version: str
    number: int
    meaning: str
    encoding: str

    def describe(self) -> str:
        return (
            f"{self.version}: field {self.number} "
            f"(meaning={self.meaning!r}, encoding={self.encoding})"
        )

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "field": self.number,
            "meaning": self.meaning,
            "encoding": self.encoding,
        }


@dataclass(frozen=True)
class Violation:
    """One finding; always names where the permission meaning changed."""

    code: str
    severity: str
    field: int
    message: str
    first_permission_change: Optional[str] = None
    origin_source: Optional[SourceRef] = None
    approval_source: Optional[SourceRef] = None
    taint_path: Tuple[str, ...] = ()
    trusted_rebuild_at: Tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        return self.severity == BLOCK

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "field": self.field,
            "message": self.message,
            "first_permission_change": self.first_permission_change,
            "origin_source": (
                None if self.origin_source is None else self.origin_source.as_dict()
            ),
            "approval_source": (
                None
                if self.approval_source is None
                else self.approval_source.as_dict()
            ),
            "taint_path": list(self.taint_path),
            "trusted_rebuild_at": list(self.trusted_rebuild_at),
        }


@dataclass(frozen=True)
class FieldTrace:
    """Per-number history along the route; attached to every report."""

    field: int
    origin: Optional[SourceRef]
    destination: Optional[SourceRef]
    steps: Tuple[Tuple[str, str, str], ...]  # (hop, action, state_after)
    final_state: str

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "origin": None if self.origin is None else self.origin.as_dict(),
            "destination": (
                None if self.destination is None else self.destination.as_dict()
            ),
            "final_state": self.final_state,
            "steps": [
                {"hop": hop, "action": action, "state_after": state}
                for hop, action, state in self.steps
            ],
        }


@dataclass(frozen=True)
class RouteReport:
    route: str
    ok: bool
    violations: Tuple[Violation, ...]
    traces: Tuple[FieldTrace, ...]
    timeline: Tuple[str, ...]

    @property
    def blocking_violations(self) -> Tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.blocking)

    def find(self, code: str) -> Tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.code == code)

    def first_permission_change(self) -> Optional[Violation]:
        """Earliest finding reporting a version that promoted a field."""

        def position(v: Violation) -> int:
            tag = v.first_permission_change
            if tag in self.timeline:
                return self.timeline.index(tag)
            return len(self.timeline)

        candidates = [v for v in self.violations if v.first_permission_change]
        return min(candidates, key=position) if candidates else None

    def as_dict(self) -> dict:
        return {
            "route": self.route,
            "ok": self.ok,
            "timeline": list(self.timeline),
            "violations": [v.as_dict() for v in self.violations],
            "traces": [t.as_dict() for t in self.traces],
        }

    def format_text(self) -> str:
        head = "PASS" if self.ok else "FAIL"
        lines = [
            f"[{head}] route {self.route}",
            f"  versions: {' -> '.join(self.timeline)}",
        ]
        if not self.violations:
            lines.append("  no findings")
        for v in self.violations:
            lines.append(
                f"  - {v.severity.upper():5s} {v.code} field {v.field}: {v.message}"
            )
            if v.first_permission_change:
                lines.append(
                    f"          first permission change: {v.first_permission_change}"
                )
            if v.origin_source:
                lines.append(
                    f"          legacy source:  {v.origin_source.describe()}"
                )
            if v.approval_source:
                lines.append(
                    f"          approval at:     {v.approval_source.describe()}"
                )
            if v.taint_path:
                lines.append(
                    f"          carried by:      {' -> '.join(v.taint_path)}"
                )
            if v.trusted_rebuild_at:
                lines.append(
                    f"          trusted rebuild: {', '.join(v.trusted_rebuild_at)}"
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-number semantics of the version timeline
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _History:
    """What the version timeline says about one field number."""

    first_change: Optional[str]      # version where approval meaning appears
    approval: Optional[SourceRef]    # first version declaring it as approval
    origin: Optional[SourceRef]      # declaration in the route's origin version


def _history(versions: Sequence[MessageVersion], number: int) -> _History:
    first_change: Optional[str] = None
    approval: Optional[SourceRef] = None
    had_definition = False
    was_approval = False
    for version in versions:
        f = version.field(number)
        if f is None:
            continue
        if f.is_approval:
            if approval is None:
                approval = SourceRef(version.version, number, f.meaning, f.encoding)
            if first_change is None and had_definition and not was_approval:
                first_change = version.version
        had_definition = True
        was_approval = f.is_approval
    origin = None
    if versions:
        f = versions[0].field(number)
        if f is not None:
            origin = SourceRef(versions[0].version, number, f.meaning, f.encoding)
    return _History(
        first_change=first_change,
        approval=approval,
        origin=origin,
    )


def _sender_writable(route: Route, number: int) -> Tuple[bool, Optional[SourceRef]]:
    """Can a client on this path put bytes into ``number``, and as whom?

    Returns ``(writable, declaring_version)``.  When several senders declare the
    number, the *ordinary* declaration wins: a route that is fed by both a
    legacy and a new writer must be judged by its weakest writer.  An approval
    declaration is only returned when every declaring sender treats the number
    as approval.
    """
    declarations = []
    for sender in route.effective_senders:
        f = sender.field(number)
        if f is not None:
            declarations.append(SourceRef(sender.version, number, f.meaning, f.encoding))
    for ref in declarations:
        if ref.meaning not in APPROVAL_MEANINGS:
            return True, ref
    if declarations:
        return True, declarations[0]
    return route.legacy_unknown_field_bytes, None


# ---------------------------------------------------------------------------
# Simulation of the hops
# ---------------------------------------------------------------------------

@dataclass
class _State:
    state: str = ABSENT
    steps: List[Tuple[str, str, str]] = dc_field(default_factory=list)
    taint_path: List[str] = dc_field(default_factory=list)
    trusted_at: List[str] = dc_field(default_factory=list)

    def apply(self, hop: HopTransform, number: int) -> None:
        if number in hop.drop:
            self.state = ABSENT
            self.steps.append((hop.hop, "drop", self.state))
            return
        if number in hop.regenerate:
            replaced = hop.can_decode(number)
            if hop.is_trusted_rebuild(number):
                if replaced or self.state == ABSENT:
                    self.state = TRUSTED
                    self.trusted_at.append(hop.hop)
                    self.steps.append((hop.hop, "regenerate(trusted)", self.state))
                else:
                    # The hop could not decode the legacy value, so appending a
                    # second copy leaves the legacy bytes on the wire.
                    self.state = DUPLICATE
                    self.taint_path.append(f"{hop.hop}:regenerate-cannot-decode")
                    self.steps.append(
                        (hop.hop, "regenerate(trusted)+legacy-kept", self.state)
                    )
            else:
                label = _SOURCE_LABEL.get(
                    hop.rebuilt_from.get(number, "untrusted"), "untrusted input"
                )
                if replaced or self.state == ABSENT:
                    self.state = HOP_WRITTEN
                    self.taint_path.append(f"{hop.hop}:regenerate({label})")
                    self.steps.append(
                        (hop.hop, f"regenerate({label})", self.state)
                    )
                else:
                    self.state = DUPLICATE
                    self.taint_path.append(
                        f"{hop.hop}:regenerate({label})+legacy-kept"
                    )
                    self.steps.append(
                        (hop.hop, f"regenerate({label})+legacy-kept", self.state)
                    )
            return
        if number in hop.passthrough:
            self.steps.append((hop.hop, "passthrough", self.state))
            return
        if hop.treats_as_unknown(number):
            if hop.unknown_bytes == "dropped":
                self.state = ABSENT
                self.steps.append((hop.hop, "unknown(dropped)", self.state))
            else:
                if self.state != ABSENT:
                    self.taint_path.append(f"{hop.hop}:unknown-preserved")
                self.steps.append((hop.hop, "unknown(preserved)", self.state))
            return
        # The hop knows the field and re-serialises it: same value, same trust.
        self.steps.append((hop.hop, "reserialize", self.state))


def _simulate(route: Route, number: int, writable: bool) -> _State:
    st = _State()
    if writable:
        st.state = SENDER_BYTES
        st.steps.append(("origin", "sender-writable", st.state))
    for hop in route.hops:
        st.apply(hop, number)
    return st


def _all_numbers(route: Route) -> List[int]:
    numbers = set(route.origin.numbers) | set(route.destination.numbers)
    for version in route.versions:
        numbers |= set(version.numbers)
    for hop in route.hops:
        numbers |= hop.regenerate | hop.drop
        if hop.known_numbers is not None:
            numbers |= hop.known_numbers
    return sorted(numbers)


# ---------------------------------------------------------------------------
# Public checks
# ---------------------------------------------------------------------------

def _violation(
    code: str,
    severity: str,
    number: int,
    message: str,
    history: _History,
    st: Optional[_State] = None,
) -> Violation:
    return Violation(
        code=code,
        severity=severity,
        field=number,
        message=message,
        first_permission_change=history.first_change,
        origin_source=history.origin,
        approval_source=history.approval,
        taint_path=tuple(st.taint_path) if st else (),
        trusted_rebuild_at=tuple(st.trusted_at) if st else (),
    )


def check_route(route: Route) -> RouteReport:
    versions = route.versions
    violations: List[Violation] = []
    traces: List[FieldTrace] = []

    for number in _all_numbers(route):
        history = _history(versions, number)
        writable, writer = _sender_writable(route, number)
        st = _simulate(route, number, writable)
        traces.append(
            FieldTrace(
                field=number,
                origin=history.origin,
                destination=_ref(route.destination, number),
                steps=tuple(st.steps),
                final_state=st.state,
            )
        )

        dest_field = route.destination.field(number)
        dest_is_approval = dest_field is not None and dest_field.is_approval

        # --- encoding ------------------------------------------------------
        # Any version that declares this number as approval through a mechanism
        # a legacy writer chooses (oneof branch, repeated element) undermines
        # the gate.  At the destination it blocks; at an intermediate version it
        # is still reported because consumers pinned there are affected.
        for version in versions:
            declared = version.field(number)
            if declared is None or not declared.is_approval:
                continue
            if declared.encoding == "normal":
                continue
            at_destination = version.version == route.destination.version
            severity = BLOCK if at_destination else WARN
            if at_destination and st.state == TRUSTED:
                severity = INFO
            violations.append(
                _violation(
                    "weak_approval_encoding",
                    severity,
                    number,
                    (
                        f"{version.version} carries approval for field {number} "
                        f"as {declared.encoding!r}; the writer chooses the branch "
                        f"or element, and therefore the answer, whenever the "
                        f"bytes are not replaced by a trusted rebuild"
                    ),
                    history,
                    st,
                )
            )

        # --- intermediate promotion ---------------------------------------
        if not dest_is_approval:
            if history.first_change is not None:
                violations.append(
                    _violation(
                        "intermediate_permission_change",
                        WARN,
                        number,
                        (
                            f"field {number} gains an approval meaning in "
                            f"{history.first_change} but the destination does not "
                            f"read it as approval; consumers pinned to "
                            f"{history.first_change} still accept sender bytes as "
                            f"consent"
                        ),
                        history,
                    )
                )
            continue

        # --- destination reads approval; what arrives? ---------------------
        if st.state == ABSENT:
            violations.append(
                _violation(
                    "approval_field_not_carried",
                    WARN,
                    number,
                    (
                        f"route never delivers approval field {number}; the gate "
                        f"is unfed, which is safe only if the reader treats "
                        f"absence as 'not approved'"
                    ),
                    history,
                )
            )
            continue

        if st.state == TRUSTED:
            violations.append(
                _violation(
                    "trusted_rebuild",
                    INFO,
                    number,
                    (
                        f"approval field {number} is rebuilt from a trusted input; "
                        f"legacy bytes were never forwarded"
                    ),
                    history,
                    st,
                )
            )
            continue

        if st.state == DUPLICATE:
            violations.append(
                _violation(
                    "duplicate_approval_carrier",
                    BLOCK,
                    number,
                    (
                        f"a hop in {route.name} writes field {number} without "
                        f"being able to decode it, so the legacy bytes stay on "
                        f"the wire next to the new value; whether the reader "
                        f"sees consent is decided by merge order, not by the "
                        f"trusted source"
                    ),
                    history,
                    st,
                )
            )
            continue

        if st.state == HOP_WRITTEN:
            violations.append(
                _violation(
                    "unauthenticated_approval_value",
                    BLOCK,
                    number,
                    (
                        f"field {number} is regenerated by a hop without a trusted "
                        f"source, yet read as approval at "
                        f"{route.destination.version}"
                    ),
                    history,
                    st,
                )
            )
            continue

        # SENDER_BYTES: sender-controlled bytes reach an approval meaning.
        approval_at = (
            history.approval.version
            if history.approval is not None
            else route.destination.version
        )
        if writer is not None and writer.meaning in APPROVAL_MEANINGS:
            code, severity = "preexisting_approval_channel", WARN
            message = (
                f"field {number} is already approval-bearing in "
                f"{writer.version}; this path neither creates nor removes the "
                f"sender's ability to write consent"
            )
        elif writer is not None:
            code, severity = "legacy_ordinary_promoted", BLOCK
            message = (
                f"field {number} is ordinary ({writer.meaning!r}) in "
                f"{writer.version} and approval-bearing in {approval_at}; a "
                f"legacy sender writes it, intermediate hops forward the bytes "
                f"as an unknown field, and the destination reads consent"
            )
        else:
            code, severity = "unauthenticated_approval_value", BLOCK
            message = (
                f"field {number} arrives with sender-controlled bytes although no "
                f"version on this path declares it before the approval meaning"
            )

        violations.append(_violation(code, severity, number, message, history, st))

    ok = not any(v.blocking for v in violations)
    return RouteReport(
        route=route.name,
        ok=ok,
        violations=tuple(violations),
        traces=tuple(traces),
        timeline=tuple(v.version for v in versions),
    )


def _ref(version: MessageVersion, number: int) -> Optional[SourceRef]:
    f = version.field(number)
    if f is None:
        return None
    return SourceRef(version.version, f.number, f.meaning, f.encoding)


def check_routes(routes: Iterable[Route]) -> Tuple[RouteReport, ...]:
    reports = tuple(check_route(r) for r in routes)
    if not reports:
        raise SchemaError("no routes supplied")
    return reports


def summarize(reports: Sequence[RouteReport]) -> dict:
    return {
        "ok": all(r.ok for r in reports),
        "routes": [
            {
                "route": r.route,
                "ok": r.ok,
                "blocking": [v.code for v in r.blocking_violations],
                "warnings": [v.code for v in r.violations if v.severity == WARN],
            }
            for r in reports
        ],
    }
