"""Event-order adjudication based on auditable evidence and source removal.

The module intentionally uses only the Python standard library.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

DEFAULT_SCOPE = "global"
ALL_SCOPES = "*"

OUTCOME_SAME = "same_event"
OUTCOME_BEFORE = "first_before_second"
OUTCOME_AFTER = "second_before_first"
OUTCOME_CONFLICT = "conflicting_evidence"
OUTCOME_UNKNOWN = "unknown"


class EventOrderError(ValueError):
    """Base class for invalid adjudication input."""


class ValidationError(EventOrderError):
    """Raised when an evidence record is malformed."""


class DuplicateEvidenceError(EventOrderError):
    """Raised when two different records reuse an evidence identifier."""


class EvidenceNotFoundError(EventOrderError):
    """Raised when an operation targets an unknown evidence identifier."""


def _nonempty_str(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{what} must be a non-empty string, got {value!r}")
    return value.strip()


def _as_tuple_events(events: Iterable[Any] | None) -> tuple[str, ...]:
    if events is None:
        return ()
    if isinstance(events, (str, bytes)):
        raise ValidationError("an event sequence must be an iterable of event IDs, not a string")
    result = tuple(_nonempty_str(item, "event ID") for item in events)
    for event in result:
        if result.count(event) > 1:
            raise ValidationError(
                f"event {event!r} occurs more than once in one source sequence; "
                "concrete events in an internal order must be distinct"
            )
    return result


@dataclass(frozen=True)
class Claim:
    """A single directed ordering claim: :attr:`before` precedes :attr:`after`."""

    before: str
    after: str
    evidence_id: str
    source: str
    kind: str
    scope: str = DEFAULT_SCOPE
    segment: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before,
            "after": self.after,
            "evidence_id": self.evidence_id,
            "source": self.source,
            "kind": self.kind,
            "scope": self.scope,
            "segment": self.segment,
        }


@dataclass(frozen=True)
class ChainStep:
    before: str
    after: str
    evidence_id: str
    source: str
    kind: str
    scope: str = DEFAULT_SCOPE
    segment: int | None = None

    @classmethod
    def from_claim(cls, claim: Claim) -> "ChainStep":
        return cls(
            before=claim.before,
            after=claim.after,
            evidence_id=claim.evidence_id,
            source=claim.source,
            kind=claim.kind,
            scope=claim.scope,
            segment=claim.segment,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before,
            "after": self.after,
            "evidence_id": self.evidence_id,
            "source": self.source,
            "kind": self.kind,
            "scope": self.scope,
            "segment": self.segment,
        }


@dataclass(frozen=True)
class EvidenceChain:
    """An ordered path from :attr:`before` to :attr:`after`."""

    before: str
    after: str
    steps: tuple[ChainStep, ...] = ()

    @property
    def evidence_ids(self) -> list[str]:
        """Unique evidence IDs in first-use order."""

        seen: set[str] = set()
        result: list[str] = []
        for step in self.steps:
            if step.evidence_id not in seen:
                seen.add(step.evidence_id)
                result.append(step.evidence_id)
        return result

    @property
    def step_evidence_ids(self) -> list[str]:
        """Evidence ID for every graph step, including repeated IDs."""

        return [step.evidence_id for step in self.steps]

    @property
    def source_ids(self) -> list[str]:
        """Unique source IDs in first-use order."""

        seen: set[str] = set()
        result: list[str] = []
        for step in self.steps:
            if step.source not in seen:
                seen.add(step.source)
                result.append(step.source)
        return result

    @property
    def is_empty_identity(self) -> bool:
        return self.before == self.after and not self.steps

    def render(self) -> str:
        if self.is_empty_identity:
            return f"{self.before} == {self.after} (same event)"
        parts = [self.before]
        for step in self.steps:
            parts.append(f"--[{step.evidence_id}/{step.source}]--> {step.after}")
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": self.before,
            "after": self.after,
            "steps": [step.to_dict() for step in self.steps],
            "evidence_ids": self.evidence_ids,
            "source_ids": self.source_ids,
            "rendered": self.render(),
        }


@dataclass(frozen=True)
class Retraction:
    """An auditable withdrawal of one evidence record."""

    target_evidence_id: str
    retraction_id: str | None = None
    reason: str | None = None
    recorded_at: str | None = None
    recorded_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "retraction_id": self.retraction_id,
            "target_evidence_id": self.target_evidence_id,
            "reason": self.reason,
            "recorded_at": self.recorded_at,
            "recorded_by": self.recorded_by,
        }


@dataclass(frozen=True)
class Conflict:
    """Two active evidence records making opposite claims in one scope."""

    conflict_id: str
    scope: str
    event_a: str
    event_b: str
    first_evidence_id: str
    second_evidence_id: str
    first_source: str
    second_source: str
    first_direction: str
    second_direction: str

    @property
    def evidence_ids(self) -> list[str]:
        return [self.first_evidence_id, self.second_evidence_id]

    @property
    def source_ids(self) -> list[str]:
        return [self.first_source, self.second_source]

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "scope": self.scope,
            "event_a": self.event_a,
            "event_b": self.event_b,
            "evidence_ids": self.evidence_ids,
            "source_ids": self.source_ids,
            "first_evidence_id": self.first_evidence_id,
            "second_evidence_id": self.second_evidence_id,
            "first_source": self.first_source,
            "second_source": self.second_source,
            "first_direction": self.first_direction,
            "second_direction": self.second_direction,
        }


@dataclass(frozen=True)
class SourceRemovalTest:
    """Result of temporarily ignoring all evidence from one source."""

    source: str
    still_has_evidence: bool
    outcome_without_source: str
    forward_chain: EvidenceChain | None = None
    reverse_chain: EvidenceChain | None = None
    remaining_chain: EvidenceChain | None = None
    critical_for_adjudicated_conclusion: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "still_has_evidence": self.still_has_evidence,
            "outcome_without_source": self.outcome_without_source,
            "remaining_chain": self.remaining_chain.to_dict() if self.remaining_chain else None,
            "forward_chain": self.forward_chain.to_dict() if self.forward_chain else None,
            "reverse_chain": self.reverse_chain.to_dict() if self.reverse_chain else None,
            "critical_for_adjudicated_conclusion": self.critical_for_adjudicated_conclusion,
        }


@dataclass(frozen=True)
class OrderVerdict:
    """The answer to an ordering query, including audit and robustness data."""

    first: str
    second: str
    outcome: str
    scope: str | None
    forward_chain: EvidenceChain | None
    reverse_chain: EvidenceChain | None
    conflicts: tuple[Conflict, ...] = ()
    removal_tests: dict[str, SourceRemovalTest] = field(default_factory=dict)
    critical_sources: tuple[str, ...] = ()
    forward_critical_sources: tuple[str, ...] = ()
    reverse_critical_sources: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def relation(self) -> str:
        """Convenience alias for :attr:`outcome`."""

        return self.outcome

    @property
    def chain(self) -> EvidenceChain | None:
        """Chain for a definitive outcome; ``None`` for conflict/unknown."""

        if self.outcome in (OUTCOME_BEFORE, OUTCOME_SAME):
            return self.forward_chain
        if self.outcome == OUTCOME_AFTER:
            return self.reverse_chain
        return None

    @property
    def evidence_ids(self) -> list[str]:
        chain = self.chain
        return chain.evidence_ids if chain else []

    @property
    def source_ids(self) -> list[str]:
        chain = self.chain
        return chain.source_ids if chain else []

    @property
    def dependent_sources(self) -> list[str]:
        """Alias for :attr:`critical_sources`."""

        return list(self.critical_sources)

    @property
    def dependent_source_ids(self) -> list[str]:
        """Alias for :attr:`critical_sources` using the common ``*_ids`` name."""

        return list(self.critical_sources)

    @property
    def required_sources(self) -> list[str]:
        """Alias for :attr:`critical_sources`."""

        return list(self.critical_sources)

    @property
    def evidence_chain(self) -> EvidenceChain | None:
        """Descriptive alias for :attr:`chain`."""

        return self.chain

    @property
    def is_order_known(self) -> bool:
        """Whether a definitive, non-conflicted order was found."""

        return self.outcome in (OUTCOME_BEFORE, OUTCOME_AFTER, OUTCOME_SAME)

    @property
    def has_evidence(self) -> bool:
        """Whether at least one directional evidence chain exists."""

        return self.forward_chain is not None or self.reverse_chain is not None

    @property
    def conflict_ids(self) -> list[str]:
        return [conflict.conflict_id for conflict in self.conflicts]

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts) or self.outcome == OUTCOME_CONFLICT

    def to_dict(self) -> dict[str, Any]:
        return {
            "first": self.first,
            "second": self.second,
            "outcome": self.outcome,
            "relation": self.relation,
            "scope": self.scope,
            "chain": self.chain.to_dict() if self.chain else None,
            "evidence_ids": self.evidence_ids,
            "source_ids": self.source_ids,
            "critical_sources": list(self.critical_sources),
            "dependent_sources": list(self.dependent_sources),
            "dependent_source_ids": list(self.dependent_source_ids),
            "required_sources": list(self.required_sources),
            "is_order_known": self.is_order_known,
            "has_evidence": self.has_evidence,
            "conflict_ids": self.conflict_ids,
            "forward_chain": self.forward_chain.to_dict() if self.forward_chain else None,
            "reverse_chain": self.reverse_chain.to_dict() if self.reverse_chain else None,
            "conflicts": [item.to_dict() for item in self.conflicts],
            "removal_tests": {key: value.to_dict() for key, value in self.removal_tests.items()},
            "forward_critical_sources": list(self.forward_critical_sources),
            "reverse_critical_sources": list(self.reverse_critical_sources),
            "reasons": list(self.reasons),
        }


@dataclass
class EvidenceRecord:
    id: str
    source: str
    kind: str
    scope: str = DEFAULT_SCOPE
    before: str | None = None
    after: str | None = None
    events: tuple[str, ...] = ()
    initiation_event: str | None = None
    receipt_event: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def claims(self) -> list[Claim]:
        if self.kind == "sequence":
            return [
                Claim(
                    before=self.events[i],
                    after=self.events[i + 1],
                    evidence_id=self.id,
                    source=self.source,
                    kind=self.kind,
                    scope=self.scope,
                    segment=i,
                )
                for i in range(len(self.events) - 1)
            ]
        if self.before is None or self.after is None:
            return []
        return [
            Claim(
                before=self.before,
                after=self.after,
                evidence_id=self.id,
                source=self.source,
                kind=self.kind,
                scope=self.scope,
            )
        ]


class OrderAdjudicator:
    """Adjudicate event order from source logs and linking observations."""

    def __init__(self, *, strict_unknown_retractions: bool = False) -> None:
        self.strict_unknown_retractions = strict_unknown_retractions
        self.sources: dict[str, dict[str, Any]] = {}
        self.evidence: dict[str, EvidenceRecord] = {}
        self.retractions: list[Retraction] = []
        self.orphan_retractions: list[Retraction] = []
        self._retracted_ids: set[str] = set()
        self._retraction_by_target: dict[str, Retraction] = {}

    # ------------------------------------------------------------------
    # Construction / ingestion
    # ------------------------------------------------------------------
    def add_source(self, source_id: str, *, independent: bool = True, **metadata: Any) -> None:
        source_id = _nonempty_str(source_id, "source_id")
        if source_id in self.sources:
            # Idempotent re-registration, while allowing metadata completion.
            self.sources[source_id].update(metadata)
            self.sources[source_id]["independent"] = independent
            return
        self.sources[source_id] = {"independent": independent, **metadata}

    def add_sequence(
        self,
        source: str,
        events: Iterable[str],
        evidence_id: str | None = None,
        *,
        scope: str = DEFAULT_SCOPE,
        **metadata: Any,
    ) -> EvidenceRecord:
        source = _nonempty_str(source, "source")
        event_tuple = _as_tuple_events(events)
        eid = evidence_id or self._generate_id("SEQ")
        eid = _nonempty_str(eid, "evidence_id")
        self.add_source(source)
        record = EvidenceRecord(
            id=eid,
            source=source,
            kind="sequence",
            scope=_nonempty_str(scope, "scope"),
            events=event_tuple,
            metadata=dict(metadata),
        )
        self._store_evidence(record)
        return record

    def add_order_evidence(
        self,
        evidence_id: str,
        source: str,
        before_event: str | None = None,
        after_event: str | None = None,
        *,
        before: str | None = None,
        after: str | None = None,
        earlier_event: str | None = None,
        later_event: str | None = None,
        scope: str = DEFAULT_SCOPE,
        kind: str = "order",
        initiation_event: str | None = None,
        receipt_event: str | None = None,
        **metadata: Any,
    ) -> EvidenceRecord:
        left = before_event or before or earlier_event
        right = after_event or after or later_event
        left = _nonempty_str(left, "before_event/initiation_event")
        right = _nonempty_str(right, "after_event/receipt_event")
        eid = _nonempty_str(evidence_id, "evidence_id")
        source = _nonempty_str(source, "source")
        if left == right:
            raise ValidationError(
                f"evidence {eid!r} cannot order event {left!r} before itself"
            )
        self.add_source(source)
        record = EvidenceRecord(
            id=eid,
            source=source,
            kind=kind,
            scope=_nonempty_str(scope, "scope"),
            before=left,
            after=right,
            initiation_event=initiation_event,
            receipt_event=receipt_event,
            metadata=dict(metadata),
        )
        self._store_evidence(record)
        return record

    def add_observation(
        self,
        evidence_id: str | None = None,
        *,
        source: str,
        initiation_event: str | None = None,
        receipt_event: str | None = None,
        send_event: str | None = None,
        sent_event: str | None = None,
        origin_event: str | None = None,
        receive_event: str | None = None,
        received_event: str | None = None,
        scope: str = DEFAULT_SCOPE,
        **metadata: Any,
    ) -> EvidenceRecord:
        start = initiation_event or send_event or sent_event or origin_event
        finish = receipt_event or receive_event or received_event
        eid = evidence_id or self._generate_id("OBS")
        return self.add_order_evidence(
            eid,
            source,
            before_event=start,
            after_event=finish,
            scope=scope,
            kind="observation",
            initiation_event=start,
            receipt_event=finish,
            **metadata,
        )

    def add_evidence_record(self, record: Mapping[str, Any] | EvidenceRecord) -> EvidenceRecord:
        if isinstance(record, EvidenceRecord):
            return self._store_evidence(record)
        if not isinstance(record, Mapping):
            raise ValidationError("evidence record must be a mapping or EvidenceRecord")
        return self._add_mapping_record(record)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OrderAdjudicator":
        if not isinstance(payload, Mapping):
            raise ValidationError("payload must be a mapping")
        adj = cls(
            strict_unknown_retractions=bool(
                payload.get("strict_unknown_retractions", payload.get("strict", False))
            )
        )

        raw_sources = payload.get("sources", []) or []
        if isinstance(raw_sources, Mapping):
            source_items: Iterable[Any] = raw_sources.items()
            for source_id, value in source_items:
                adj.add_source(str(source_id))
                if isinstance(value, (list, tuple)):
                    adj.add_sequence(
                        str(source_id),
                        value,
                        f"AUTO-SEQ-{source_id}",
                    )
                elif isinstance(value, Mapping):
                    events = value.get("events") or value.get("event_order") or value.get("order")
                    if events is not None:
                        eid = (
                            value.get("id")
                            or value.get("evidence_id")
                            or f"AUTO-SEQ-{source_id}"
                        )
                        adj.add_sequence(str(source_id), events, str(eid))
                    adj.sources[str(source_id)].update(
                        {k: v for k, v in value.items() if k not in {"events", "event_order", "order", "id", "evidence_id"}}
                    )
                else:
                    raise ValidationError(
                        f"source {source_id!r} must map to an event list or mapping"
                    )
        else:
            for source in raw_sources:
                if isinstance(source, Mapping):
                    source_id = (
                        source.get("id") or source.get("source_id") or source.get("name")
                    )
                    if source_id is None:
                        raise ValidationError(f"source record has no id: {source!r}")
                    adj.add_source(
                        source_id,
                        independent=bool(source.get("independent", True)),
                        **{
                            k: v
                            for k, v in source.items()
                            if k
                            not in {"id", "source_id", "name", "independent", "events", "event_order", "order", "evidence_id"}
                        },
                    )
                    events = source.get("events") or source.get("event_order") or source.get("order")
                    if events is not None:
                        eid = source.get("evidence_id")
                        adj.add_sequence(str(source_id), events, str(eid) if eid else None)
                else:
                    adj.add_source(str(source))

        for key in ("sequences", "source_orders", "event_orders"):
            for item in payload.get(key, []) or []:
                adj._add_mapping_record(item, default_kind="sequence")

        for key in ("observations",):
            for item in payload.get(key, []) or []:
                adj._add_mapping_record(item, default_kind="observation")

        for key in ("orders", "order_evidence"):
            for item in payload.get(key, []) or []:
                adj._add_mapping_record(item, default_kind="order")

        for item in payload.get("evidence", []) or []:
            adj._add_mapping_record(item)

        for item in payload.get("retractions", payload.get("withdrawals", [])) or []:
            adj.add_retraction_record(item)

        return adj

    def _add_mapping_record(
        self, item: Mapping[str, Any], default_kind: str | None = None
    ) -> EvidenceRecord:
        if not isinstance(item, Mapping):
            raise ValidationError(f"evidence record must be a mapping, got {item!r}")
        eid = (
            item.get("id")
            or item.get("evidence_id")
            or item.get("eid")
            or self._generate_id("EVD")
        )
        source = item.get("source") or item.get("source_id") or item.get("reported_by")
        if source is None:
            raise ValidationError(f"evidence {eid!r} has no source")
        kind = str(item.get("type") or item.get("kind") or default_kind or "order").lower()
        aliases = {
            "seq": "sequence",
            "source_order": "sequence",
            "log_order": "sequence",
            "obs": "observation",
            "message_observation": "observation",
            "order_evidence": "order",
            "direct_order": "order",
        }
        kind = aliases.get(kind, kind)
        scope = item.get("scope") or item.get("range") or DEFAULT_SCOPE

        if kind == "sequence":
            events = (
                item.get("events")
                or item.get("event_order")
                or item.get("order")
                or item.get("sequence")
            )
            return self.add_sequence(
                str(source), events or [], str(eid), scope=str(scope)
            )

        if kind == "observation":
            start = (
                item.get("initiation_event")
                or item.get("initiate_event")
                or item.get("initiation")
                or item.get("send_event")
                or item.get("sent_event")
                or item.get("origin_event")
                or item.get("from_event")
            )
            finish = (
                item.get("receipt_event")
                or item.get("receive_event")
                or item.get("received_event")
                or item.get("receiving_event")
                or item.get("to_event")
                or item.get("destination_event")
            )
            return self.add_observation(
                str(eid),
                source=str(source),
                initiation_event=start,
                receipt_event=finish,
                scope=str(scope),
            )

        before = (
            item.get("before_event")
            or item.get("before")
            or item.get("earlier_event")
            or item.get("first_event")
            or item.get("start_event")
        )
        after = (
            item.get("after_event")
            or item.get("after")
            or item.get("later_event")
            or item.get("second_event")
            or item.get("end_event")
        )
        return self.add_order_evidence(
            str(eid), str(source), before, after, scope=str(scope), kind=kind
        )

    def _store_evidence(self, record: EvidenceRecord) -> EvidenceRecord:
        if record.id in self.evidence:
            old = self.evidence[record.id]
            if old != record:
                raise DuplicateEvidenceError(
                    f"evidence ID {record.id!r} was reused by conflicting records"
                )
            return old
        self.evidence[record.id] = record
        self.add_source(record.source)
        return record

    def _generate_id(self, prefix: str) -> str:
        i = 1
        while True:
            candidate = f"{prefix}-{i:04d}"
            if candidate not in self.evidence:
                return candidate
            i += 1

    # ------------------------------------------------------------------
    # Retractions
    # ------------------------------------------------------------------
    def retract_evidence(
        self,
        target_evidence_id: str,
        retraction_id: str | None = None,
        *,
        reason: str | None = None,
        recorded_at: str | None = None,
        recorded_by: str | None = None,
    ) -> Retraction:
        target = _nonempty_str(target_evidence_id, "target_evidence_id")
        retraction = Retraction(
            target_evidence_id=target,
            retraction_id=retraction_id,
            reason=reason,
            recorded_at=recorded_at,
            recorded_by=recorded_by,
        )
        if target not in self.evidence:
            if self.strict_unknown_retractions:
                raise EvidenceNotFoundError(
                    f"retraction {retraction_id!r} targets unknown evidence {target!r}"
                )
            # Lenient mode accepts records in either input order.  Mark the ID
            # now so evidence added later in a streaming/interleaved workflow
            # is still born retracted.
            self.orphan_retractions.append(retraction)
            self._retracted_ids.add(target)
            self._retraction_by_target.setdefault(target, retraction)
            return retraction

        # The first effective withdrawal is retained. Additional correction
        # records are still auditable in ``retractions``.
        self.retractions.append(retraction)
        self._retracted_ids.add(target)
        self._retraction_by_target.setdefault(target, retraction)
        return retraction

    # Friendly alias.
    retract = retract_evidence

    def add_retraction_record(self, item: Any) -> Retraction:
        if isinstance(item, str):
            return self.retract_evidence(item)
        if not isinstance(item, Mapping):
            raise ValidationError("retraction must be an evidence ID or mapping")
        target = (
            item.get("evidence_id")
            or item.get("target_evidence_id")
            or item.get("target_id")
            or item.get("target")
            or item.get("evidence")
        )
        if target is None:
            # A record shaped as {"id": "E1", "retracted": true} can use id.
            if item.get("retracted") is True and item.get("id") is not None:
                target = item.get("id")
            else:
                raise ValidationError(f"retraction has no target evidence ID: {item!r}")
        rid = item.get("retraction_id") or item.get("withdrawal_id")
        # Do not mistake the target's evidence ID for a separate retraction ID
        # unless an explicit retraction_id/withdrawal_id is present.
        return self.retract_evidence(
            str(target),
            str(rid) if rid is not None else None,
            reason=item.get("reason"),
            recorded_at=item.get("at") or item.get("recorded_at"),
            recorded_by=item.get("by") or item.get("recorded_by"),
        )

    def is_active(self, evidence_id: str) -> bool:
        return evidence_id in self.evidence and evidence_id not in self._retracted_ids

    @property
    def active_evidence_ids(self) -> list[str]:
        return [eid for eid in self.evidence if eid not in self._retracted_ids]

    @property
    def retracted_evidence_ids(self) -> list[str]:
        return list(self._retracted_ids)

    # ------------------------------------------------------------------
    # Claims and graph
    # ------------------------------------------------------------------
    def active_claims(
        self,
        *,
        scope: str | None = None,
        excluded_sources: Iterable[str] = (),
        excluded_evidence: Iterable[str] = (),
    ) -> list[Claim]:
        excluded_source_set = set(excluded_sources)
        excluded_evidence_set = set(excluded_evidence) | self._retracted_ids
        claims: list[Claim] = []
        for eid, record in self.evidence.items():
            if eid in excluded_evidence_set:
                continue
            if record.source in excluded_source_set:
                continue
            for claim in record.claims():
                if scope is not None and scope != ALL_SCOPES and claim.scope != scope:
                    continue
                claims.append(claim)
        return claims

    def known_events(self, *, scope: str | None = None) -> set[str]:
        events: set[str] = set()
        for record in self.evidence.values():
            if scope is not None and scope != ALL_SCOPES and record.scope != scope:
                continue
            events.update(record.events)
            if record.before:
                events.add(record.before)
            if record.after:
                events.add(record.after)
        return events

    def source_ids(self, *, active_only: bool = False) -> list[str]:
        if active_only:
            result: list[str] = []
            seen: set[str] = set()
            for eid, record in self.evidence.items():
                if eid in self._retracted_ids:
                    continue
                if record.source not in seen:
                    seen.add(record.source)
                    result.append(record.source)
            return result
        return list(self.sources.keys())

    def _adjacency(self, claims: Iterable[Claim]) -> dict[str, list[Claim]]:
        graph: dict[str, list[Claim]] = {}
        for claim in claims:
            graph.setdefault(claim.before, []).append(claim)
        for edges in graph.values():
            edges.sort(key=lambda c: (c.evidence_id, c.segment is None, c.segment or 0, c.after))
        return graph

    def find_chain(
        self,
        first: str,
        second: str,
        *,
        scope: str | None = None,
        excluded_sources: Iterable[str] = (),
        excluded_evidence: Iterable[str] = (),
        allow_cross_scope: bool = False,
    ) -> EvidenceChain | None:
        first = _nonempty_str(first, "first event")
        second = _nonempty_str(second, "second event")
        if first == second:
            return EvidenceChain(first, second, ())

        if scope is not None and scope != ALL_SCOPES:
            claims = self.active_claims(
                scope=scope,
                excluded_sources=excluded_sources,
                excluded_evidence=excluded_evidence,
            )
            return self._find_chain_in_claims(first, second, claims)

        claims = self.active_claims(
            scope=ALL_SCOPES,
            excluded_sources=excluded_sources,
            excluded_evidence=excluded_evidence,
        )
        if allow_cross_scope:
            return self._find_chain_in_claims(first, second, claims)

        # A scope is an evidence/timeline partition.  When no explicit scope is
        # requested, choose the shortest chain contained in one scope rather
        # than silently composing unrelated scopes.
        candidates: list[EvidenceChain] = []
        for claim_scope in sorted({claim.scope for claim in claims}):
            scoped_claims = [claim for claim in claims if claim.scope == claim_scope]
            chain = self._find_chain_in_claims(first, second, scoped_claims)
            if chain is not None:
                candidates.append(chain)
        if not candidates:
            return None
        return min(candidates, key=lambda chain: (len(chain.steps), tuple(chain.evidence_ids)))

    def _find_chain_in_claims(
        self,
        first: str,
        second: str,
        claims: list[Claim],
    ) -> EvidenceChain | None:
        graph = self._adjacency(claims)
        queue: deque[tuple[str, tuple[ChainStep, ...]]] = deque([(first, ())])
        visited = {first}

        while queue:
            current, steps = queue.popleft()
            for claim in graph.get(current, ()):  # sorted deterministic edges
                if claim.after in visited:
                    continue
                next_steps = steps + (ChainStep.from_claim(claim),)
                if claim.after == second:
                    return EvidenceChain(first, second, next_steps)
                visited.add(claim.after)
                queue.append((claim.after, next_steps))
        return None

    # ------------------------------------------------------------------
    # Conflict detection
    # ------------------------------------------------------------------
    def conflicts(self, *, scope: str | None = None) -> list[Conflict]:
        # Conflict detection always uses all active claims. If an explicit
        # scope is supplied, restrict both grouping and returned records.
        claims = self.active_claims(scope=ALL_SCOPES)
        grouped: dict[tuple[str, str, str], dict[str, list[Claim]]] = {}
        for claim in claims:
            a, b = sorted((claim.before, claim.after))
            key = (claim.scope, a, b)
            directions = grouped.setdefault(key, {})
            directions.setdefault(f"{claim.before}->{claim.after}", []).append(claim)

        result: list[Conflict] = []
        for (claim_scope, a, b), directions in grouped.items():
            if scope is not None and scope != ALL_SCOPES and claim_scope != scope:
                continue
            forward = directions.get(f"{a}->{b}", [])
            reverse = directions.get(f"{b}->{a}", [])
            if not forward or not reverse:
                continue
            for left_claim in forward:
                for right_claim in reverse:
                    e1, e2 = sorted((left_claim.evidence_id, right_claim.evidence_id))
                    # Keep direction details aligned with sorted IDs.
                    if left_claim.evidence_id == e1:
                        first_claim, second_claim = left_claim, right_claim
                    else:
                        first_claim, second_claim = right_claim, left_claim
                    cid = f"CONF:{claim_scope}:{a}<>{b}:{e1}:{e2}"
                    result.append(
                        Conflict(
                            conflict_id=cid,
                            scope=claim_scope,
                            event_a=a,
                            event_b=b,
                            first_evidence_id=first_claim.evidence_id,
                            second_evidence_id=second_claim.evidence_id,
                            first_source=first_claim.source,
                            second_source=second_claim.source,
                            first_direction=f"{first_claim.before} before {first_claim.after}",
                            second_direction=f"{second_claim.before} before {second_claim.after}",
                        )
                    )
        result.sort(key=lambda c: c.conflict_id)
        return result

    def conflicted_evidence_ids(self, *, scope: str | None = None) -> list[str]:
        ids = {eid for conflict in self.conflicts(scope=scope) for eid in conflict.evidence_ids}
        return sorted(ids)

    def _relevant_conflicts(
        self,
        first: str,
        second: str,
        forward: EvidenceChain | None,
        reverse: EvidenceChain | None,
        query_scope: str | None,
    ) -> tuple[Conflict, ...]:
        chain_evidence = set()
        for chain in (forward, reverse):
            if chain:
                chain_evidence.update(chain.evidence_ids)
        relevant: list[Conflict] = []
        seen: set[str] = set()
        pair = {first, second}
        for conflict in self.conflicts(scope=query_scope):
            involves_pair = {conflict.event_a, conflict.event_b} == pair
            involves_chain_evidence = any(
                eid in chain_evidence for eid in conflict.evidence_ids
            )
            if (involves_pair or involves_chain_evidence) and conflict.conflict_id not in seen:
                seen.add(conflict.conflict_id)
                relevant.append(conflict)
        return tuple(relevant)

    # ------------------------------------------------------------------
    # Query / adjudication
    # ------------------------------------------------------------------
    def query(
        self,
        first: str,
        second: str,
        *,
        scope: str | None = None,
        test_source_removal: bool = True,
    ) -> OrderVerdict:
        first = _nonempty_str(first, "first event")
        second = _nonempty_str(second, "second event")

        # ``scope=None`` includes every scope.  Pass an explicit scope to
        # restrict both graph search and conflict reporting to that timeline.
        search_scope = ALL_SCOPES if scope is None else scope
        forward = self.find_chain(first, second, scope=search_scope)
        reverse = self.find_chain(second, first, scope=search_scope)

        reasons: list[str] = []
        if first == second:
            outcome = OUTCOME_SAME
            forward = EvidenceChain(first, second, ())
            reverse = EvidenceChain(second, first, ())
            reasons.append("queried identifiers denote the same event")
        elif forward and reverse:
            outcome = OUTCOME_CONFLICT
            reasons.append("active evidence supports both ordering directions")
        elif forward:
            outcome = OUTCOME_BEFORE
            reasons.append("found a directed evidence chain from first event to second event")
        elif reverse:
            outcome = OUTCOME_AFTER
            reasons.append("found a directed evidence chain from second event to first event")
        else:
            outcome = OUTCOME_UNKNOWN
            reasons.append("no directed chain of active evidence connects the two events")

        conflicts = self._relevant_conflicts(first, second, forward, reverse, search_scope)

        removal_tests: dict[str, SourceRemovalTest] = {}
        forward_critical: list[str] = []
        reverse_critical: list[str] = []
        critical: list[str] = []

        if test_source_removal:
            for source in self.source_ids():
                if first == second:
                    f_without = EvidenceChain(first, second, ())
                    r_without = EvidenceChain(second, first, ())
                else:
                    f_without = self.find_chain(
                        first, second, scope=search_scope, excluded_sources={source}
                    )
                    r_without = self.find_chain(
                        second, first, scope=search_scope, excluded_sources={source}
                    )
                outcome_without = self._classify(f_without, r_without, first == second)
                remaining = f_without or r_without
                critical_for_conclusion = False

                if forward is not None and f_without is None and first != second:
                    forward_critical.append(source)
                if reverse is not None and r_without is None and first != second:
                    reverse_critical.append(source)

                if outcome == OUTCOME_BEFORE:
                    critical_for_conclusion = f_without is None
                    if critical_for_conclusion:
                        critical.append(source)
                elif outcome == OUTCOME_AFTER:
                    critical_for_conclusion = r_without is None
                    if critical_for_conclusion:
                        critical.append(source)
                elif outcome == OUTCOME_SAME:
                    critical_for_conclusion = False

                removal_tests[source] = SourceRemovalTest(
                    source=source,
                    still_has_evidence=bool(f_without or r_without),
                    outcome_without_source=outcome_without,
                    forward_chain=f_without,
                    reverse_chain=r_without,
                    remaining_chain=remaining,
                    critical_for_adjudicated_conclusion=critical_for_conclusion,
                )

        return OrderVerdict(
            first=first,
            second=second,
            outcome=outcome,
            scope=scope,
            forward_chain=forward,
            reverse_chain=reverse,
            conflicts=conflicts,
            removal_tests=dict(sorted(removal_tests.items())),
            critical_sources=tuple(sorted(critical)),
            forward_critical_sources=tuple(sorted(forward_critical)),
            reverse_critical_sources=tuple(sorted(reverse_critical)),
            reasons=tuple(reasons),
        )

    @staticmethod
    def _classify(
        forward: EvidenceChain | None,
        reverse: EvidenceChain | None,
        same: bool = False,
    ) -> str:
        if same:
            return OUTCOME_SAME
        if forward and reverse:
            return OUTCOME_CONFLICT
        if forward:
            return OUTCOME_BEFORE
        if reverse:
            return OUTCOME_AFTER
        return OUTCOME_UNKNOWN

    def adjudicate(self, first: str, second: str, **kwargs: Any) -> OrderVerdict:
        return self.query(first, second, **kwargs)

    # Common domain-language aliases.
    add_source_order = add_sequence
    add_order_record = add_order_evidence
    add_observation_record = add_observation
    withdraw_evidence = retract_evidence
    list_conflicts = conflicts
    answer = query


def build_adjudicator(payload: Mapping[str, Any] | None = None, **kwargs: Any) -> OrderAdjudicator:
    """Build an adjudicator from a dictionary and optional record kwargs."""

    merged: dict[str, Any] = dict(payload or {})
    for key, value in kwargs.items():
        if value is not None:
            merged[key] = value
    return OrderAdjudicator.from_dict(merged)


def adjudicate_order(
    first: str, second: str, payload: Mapping[str, Any] | None = None, **kwargs: Any
) -> dict[str, Any]:
    """Functional wrapper returning a JSON-friendly verdict dictionary."""

    adj = build_adjudicator(payload, **kwargs)
    return adj.query(first, second).to_dict()
