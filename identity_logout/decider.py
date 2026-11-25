"""Stateful evidence decider for ancestor-session logout completion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .errors import EventConflictError, LogoutDecisionError
from .model import (
    CompletionReceipt,
    Event,
    LogoutDecision,
    MissingEvidence,
    RevocationBarrier,
)
from .normalization import barrier_from_obj, event_from_obj, watermarks_from_obj


@dataclass(slots=True)
class _SessionState:
    session_id: str
    parent_id: str | None = None
    children: set[str] = field(default_factory=set)
    derivation_event_id: str | None = None
    close_event_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    transfer_ids: list[str] = field(default_factory=list)
    endpoints: set[str] = field(default_factory=set)
    origin_endpoint: str | None = None

    @property
    def close_event_id(self) -> str | None:
        return self.close_event_ids[0] if self.close_event_ids else None


@dataclass(slots=True)
class _TransferState:
    transfer_id: str
    session_id: str | None = None
    send_event_id: str | None = None
    from_endpoint: str | None = None
    to_endpoint: str | None = None
    receipt_event_ids: list[str] = field(default_factory=list)
    receipt_natural_keys: set[str] = field(default_factory=set)

    @property
    def acknowledged(self) -> bool:
        return bool(self.receipt_event_ids)


class LogoutDecider:
    """Accumulate out-of-order logout evidence and decide completion.

    The decider is monotonic: evidence can be added in any order and late
    derivation/transfer events are correlated with previously buffered closes
    and receipts.
    """

    def __init__(
        self,
        events: Iterable[Event | Mapping[str, Any]] | None = None,
        watermarks: Mapping[str, Any] | Iterable[Any] | None = None,
        barrier: int | Mapping[str, int] | RevocationBarrier | None = None,
    ) -> None:
        self.events: dict[str, Event] = {}
        self.ingestion_order: list[str] = []
        self.sessions: dict[str, _SessionState] = {}
        self.transfers: dict[str, _TransferState] = {}
        self.watermarks: dict[str, int] = {}
        self.barrier = barrier_from_obj(barrier)

        self.duplicate_event_ids: list[str] = []
        self.duplicate_receipt_event_ids: list[str] = []
        self.pending_receipt_event_ids: list[str] = []

        # event id -> transfer id for transfer-send events
        self._transfer_send_index: dict[str, str] = {}

        if events is not None:
            self.ingest_events(events)
        if watermarks is not None:
            self.update_watermarks(watermarks)

    # ------------------------------------------------------------------ ingest

    def ingest_events(
        self, events: Iterable[Event | Mapping[str, Any]]
    ) -> list[Event]:
        """Ingest many events; return newly accepted normalized events."""

        accepted: list[Event] = []
        for item in events:
            event = self.ingest(item)
            if event is not None:
                accepted.append(event)
        return accepted

    def ingest(self, item: Event | Mapping[str, Any]) -> Event | None:
        """Ingest one event.

        Returns the normalized event for a new event and ``None`` for an exact
        duplicate event ID.  Conflicting reuse of an event ID raises
        :class:`EventConflictError`.
        """

        event = event_from_obj(item)

        if event.event_id in self.events:
            existing = self.events[event.event_id]
            if self._immutable_core(existing) != self._immutable_core(event):
                raise EventConflictError(
                    f"event {event.event_id!r} was reused with conflicting evidence"
                )
            if event.event_id not in self.duplicate_event_ids:
                self.duplicate_event_ids.append(event.event_id)
            return None

        self.events[event.event_id] = event
        self.ingestion_order.append(event.event_id)
        self._apply(event)
        self._resolve_pending_receipts()
        return event

    @staticmethod
    def _immutable_core(event: Event) -> tuple[Any, ...]:
        return (
            event.event_id,
            event.kind,
            event.session_id,
            event.issuer,
            event.sequence,
            event.predecessor_id,
            event.predecessor_endpoint,
            event.parent_session_id,
            event.child_session_id,
            event.transfer_id,
            event.from_endpoint,
            event.to_endpoint,
            event.receipt_id,
            event.acknowledges_transfer_id,
        )

    def update_watermarks(
        self,
        watermarks: Mapping[str, Any] | Iterable[Any],
    ) -> dict[str, int]:
        """Update endpoint watermarks, never moving an existing mark backward."""

        updates = watermarks_from_obj(watermarks)
        for endpoint, sequence in updates.items():
            self.watermarks[endpoint] = max(
                self.watermarks.get(endpoint, 0), sequence
            )
        return dict(self.watermarks)

    def set_barrier(
        self,
        barrier: int | Mapping[str, int] | RevocationBarrier,
        root_session_id: str | None = None,
    ) -> RevocationBarrier:
        """Set or replace the revocation barrier."""

        self.barrier = barrier_from_obj(barrier, root_session_id)
        return self.barrier

    # ----------------------------------------------------------------- apply

    def _session(self, session_id: str) -> _SessionState:
        state = self.sessions.get(session_id)
        if state is None:
            state = _SessionState(session_id=session_id)
            self.sessions[session_id] = state
        return state

    def _apply(self, event: Event) -> None:
        session_id = event.primary_session_id
        state: _SessionState | None = None
        if session_id is not None:
            state = self._session(session_id)
            if event.event_id not in state.event_ids:
                state.event_ids.append(event.event_id)
            if event.issuer:
                state.endpoints.add(event.issuer)
                if state.origin_endpoint is None:
                    state.origin_endpoint = event.issuer
            if event.from_endpoint:
                state.endpoints.add(event.from_endpoint)
            if event.to_endpoint:
                state.endpoints.add(event.to_endpoint)

        kind = event.kind.value

        if kind == "derivation":
            self._apply_derivation(event, state)
        elif kind == "transfer":
            self._apply_transfer(event, state)
        elif kind == "transfer_receipt":
            self._apply_receipt(event)
        elif kind == "close":
            if state is not None and event.event_id not in state.close_event_ids:
                state.close_event_ids.append(event.event_id)
            # A close may explicitly carry the transfer receipt.  A plain
            # close does NOT acknowledge a transfer.
            if event.acknowledges_transfer_id:
                self._apply_receipt(event)

    def _apply_derivation(self, event: Event, child_state: _SessionState | None) -> None:
        child_id = event.child_session_id or event.session_id
        if child_id is None:
            raise EventConflictError(f"derivation {event.event_id!r} has no child session")
        child_state = child_state or self._session(child_id)

        parent_id = event.parent_session_id
        if child_state.parent_id is not None and parent_id is not None:
            if child_state.parent_id != parent_id:
                raise EventConflictError(
                    f"session {child_id!r} has conflicting parents: "
                    f"{child_state.parent_id!r} and {parent_id!r}"
                )
        elif parent_id is not None:
            child_state.parent_id = parent_id

        if child_state.derivation_event_id is None:
            child_state.derivation_event_id = event.event_id
        elif child_state.derivation_event_id != event.event_id:
            # Duplicate/alternative derivation evidence for one child.  The
            # parent conflict check above protects the graph; retain the first
            # event as canonical but keep both IDs in event_ids.
            pass

        if parent_id is not None:
            parent_state = self._session(parent_id)
            parent_state.children.add(child_id)

    def _apply_transfer(self, event: Event, state: _SessionState | None) -> None:
        transfer_id = event.transfer_id or event.event_id
        transfer = self.transfers.get(transfer_id)
        if transfer is None:
            transfer = _TransferState(transfer_id=transfer_id)
            self.transfers[transfer_id] = transfer

        if transfer.send_event_id is not None and transfer.send_event_id != event.event_id:
            # Repeated delivery with a new event ID but same natural transfer
            # ID is tolerated when both copies state the same routing facts.
            if (
                transfer.session_id
                and event.session_id
                and transfer.session_id != event.session_id
            ) or (
                transfer.to_endpoint
                and event.to_endpoint
                and transfer.to_endpoint != event.to_endpoint
            ):
                raise EventConflictError(
                    f"transfer {transfer_id!r} was reused with conflicting routing"
                )
        transfer.send_event_id = event.event_id
        transfer.session_id = transfer.session_id or event.session_id
        transfer.from_endpoint = transfer.from_endpoint or event.from_endpoint or event.issuer
        transfer.to_endpoint = transfer.to_endpoint or event.to_endpoint

        self._transfer_send_index[event.event_id] = transfer_id
        if state is not None and transfer_id not in state.transfer_ids:
            state.transfer_ids.append(transfer_id)
        if transfer.session_id and transfer.session_id in self.sessions:
            session_state = self.sessions[transfer.session_id]
            if transfer_id not in session_state.transfer_ids:
                session_state.transfer_ids.append(transfer_id)

    def _resolve_transfer_key(self, event: Event) -> str | None:
        explicit = event.transfer_id or event.acknowledges_transfer_id
        if explicit:
            return explicit
        if event.predecessor_id:
            if event.predecessor_id in self._transfer_send_index:
                return self._transfer_send_index[event.predecessor_id]
            if event.predecessor_id in self.transfers:
                return event.predecessor_id
        return None

    def _apply_receipt(self, event: Event) -> None:
        key = self._resolve_transfer_key(event)
        if key is None:
            if event.event_id not in self.pending_receipt_event_ids:
                self.pending_receipt_event_ids.append(event.event_id)
            return

        transfer = self.transfers.get(key)
        if transfer is None:
            transfer = _TransferState(transfer_id=key)
            self.transfers[key] = transfer

        if event.session_id and not transfer.session_id:
            transfer.session_id = event.session_id
        if event.from_endpoint and not transfer.from_endpoint:
            transfer.from_endpoint = event.from_endpoint
        if event.to_endpoint and not transfer.to_endpoint:
            transfer.to_endpoint = event.to_endpoint
        ack_endpoint = event.issuer or event.to_endpoint
        if ack_endpoint and not transfer.to_endpoint:
            transfer.to_endpoint = ack_endpoint

        natural_keys: set[str] = set()
        if event.receipt_id:
            natural_keys.add(f"receipt-id:{event.receipt_id}")
        if ack_endpoint:
            natural_keys.add(f"endpoint-ack:{key}:{ack_endpoint}")
        natural_keys.add(f"delivery:{event.event_id}")

        already = event.event_id in transfer.receipt_event_ids or any(
            natural_key in transfer.receipt_natural_keys
            for natural_key in natural_keys
        )
        if already:
            if event.event_id not in self.duplicate_receipt_event_ids:
                self.duplicate_receipt_event_ids.append(event.event_id)
            return

        transfer.receipt_event_ids.append(event.event_id)
        transfer.receipt_natural_keys.update(natural_keys)

        if transfer.session_id:
            state = self._session(transfer.session_id)
            if event.event_id not in state.event_ids:
                state.event_ids.append(event.event_id)
            if key not in state.transfer_ids:
                state.transfer_ids.append(key)
            if ack_endpoint:
                state.endpoints.add(ack_endpoint)

        if event.event_id in self.pending_receipt_event_ids:
            self.pending_receipt_event_ids.remove(event.event_id)

    def _resolve_pending_receipts(self) -> None:
        for event_id in list(self.pending_receipt_event_ids):
            event = self.events[event_id]
            key = self._resolve_transfer_key(event)
            if key is None:
                continue
            self.pending_receipt_event_ids.remove(event_id)
            self._apply_receipt(event)

    # -------------------------------------------------------------- graph math

    def _descendant_ids(self, root_session_id: str) -> tuple[str, ...]:
        if root_session_id not in self.sessions:
            return ()
        seen: set[str] = set()
        stack = list(self.sessions[root_session_id].children)
        while stack:
            session_id = stack.pop()
            if session_id in seen:
                continue
            seen.add(session_id)
            state = self.sessions.get(session_id)
            if state is not None:
                stack.extend(state.children)
        return tuple(sorted(seen))

    def _find_causal_cycle(self, root_session_id: str, descendants: tuple[str, ...]) -> list[str]:
        scope = {root_session_id, *descendants}
        for start in sorted(scope):
            path: list[str] = []
            position: dict[str, int] = {}
            current: str | None = start
            while current is not None and current in scope:
                if current in position:
                    cycle_start = position[current]
                    return path[cycle_start:] + [current]
                position[current] = len(path)
                path.append(current)
                state = self.sessions.get(current)
                current = state.parent_id if state else None
        return []

    def _custodian_endpoint(self, session_id: str) -> str | None:
        state = self.sessions[session_id]
        for transfer_id in reversed(state.transfer_ids):
            transfer = self.transfers.get(transfer_id)
            if transfer and transfer.to_endpoint:
                return transfer.to_endpoint
        return state.origin_endpoint

    def orphan_session_ids(self, root_session_id: str | None = None) -> tuple[str, ...]:
        """Sessions known only from an early close/receipt, without a parent edge.

        If ``root_session_id`` is supplied, that root itself is excluded.
        """

        orphans = [
            sid
            for sid, state in self.sessions.items()
            if state.parent_id is None
            and state.derivation_event_id is None
            and sid != root_session_id
        ]
        return tuple(sorted(orphans))

    # -------------------------------------------------------------- evaluation

    def evaluate(
        self,
        root_session_id: str,
        barrier: int | Mapping[str, int] | RevocationBarrier | None = None,
        *,
        require_root_closed: bool = False,
        strict_causality: bool = True,
        conservative_unlinked_predecessors: bool = True,
    ) -> LogoutDecision:
        """Evaluate whether a completion receipt can be issued for ``root``."""

        used_barrier = barrier_from_obj(barrier, root_session_id) if barrier is not None else self.barrier
        if used_barrier is not None and used_barrier.root_session_id is None:
            used_barrier = RevocationBarrier(
                root_session_id=root_session_id,
                positions=dict(used_barrier.positions),
                default_sequence=used_barrier.default_sequence,
                barrier_id=used_barrier.barrier_id,
            )

        root_state = self._session(root_session_id)
        root_known = bool(root_state.event_ids or root_state.children)
        descendants = self._descendant_ids(root_session_id)
        scope = {root_session_id, *descendants}

        missing: list[MissingEvidence] = []

        cycle = self._find_causal_cycle(root_session_id, descendants)
        if cycle:
            missing.append(
                MissingEvidence(
                    kind="causal_cycle",
                    session_id=cycle[0],
                    detail="causal parent cycle: " + " -> ".join(cycle),
                )
            )

        if require_root_closed and root_state.close_event_id is None:
            missing.append(
                MissingEvidence(
                    kind="open_root_session",
                    session_id=root_session_id,
                    endpoint=self._custodian_endpoint(root_session_id),
                    detail="root session has no close event",
                )
            )

        open_descendants: list[str] = []
        closed_descendants: list[str] = []
        for session_id in descendants:
            state = self.sessions[session_id]
            if state.close_event_id is None:
                open_descendants.append(session_id)
                missing.append(
                    MissingEvidence(
                        kind="open_descendant",
                        session_id=session_id,
                        endpoint=self._custodian_endpoint(session_id),
                        detail="descendant session has no correlated close event",
                    )
                )
            else:
                closed_descendants.append(session_id)

        relevant_transfers = [
            transfer
            for transfer in self.transfers.values()
            if transfer.session_id in scope
        ]
        relevant_transfers.sort(key=lambda item: item.transfer_id)
        in_flight: list[str] = []
        for transfer in relevant_transfers:
            if not transfer.acknowledged:
                in_flight.append(transfer.transfer_id)
                missing.append(
                    MissingEvidence(
                        kind="in_flight_transfer",
                        transfer_id=transfer.transfer_id,
                        session_id=transfer.session_id,
                        endpoint=transfer.to_endpoint or transfer.from_endpoint,
                        detail="transfer was sent but target has not signed for it",
                    )
                )

        scope_event_ids: set[str] = set()
        relevant_endpoints: set[str] = set()
        for session_id in scope:
            state = self.sessions.get(session_id)
            if state is None:
                continue
            scope_event_ids.update(state.event_ids)
            relevant_endpoints.update(state.endpoints)

        for transfer in relevant_transfers:
            if transfer.from_endpoint:
                relevant_endpoints.add(transfer.from_endpoint)
            if transfer.to_endpoint:
                relevant_endpoints.add(transfer.to_endpoint)
            scope_event_ids.add(transfer.send_event_id) if transfer.send_event_id else None
            scope_event_ids.update(transfer.receipt_event_ids)

        if used_barrier is not None:
            relevant_endpoints.update(used_barrier.positions.keys())

        reported_predecessor_gaps: set[str] = set()
        if strict_causality:
            for event_id in sorted(scope_event_ids):
                event = self.events.get(event_id)
                if event is None or not event.predecessor_id:
                    continue
                predecessor = event.predecessor_id
                if predecessor in self.events or predecessor in reported_predecessor_gaps:
                    continue
                reported_predecessor_gaps.add(predecessor)
                endpoint = event.predecessor_endpoint or event.issuer
                if endpoint is None and event.kind.value == "transfer":
                    endpoint = event.from_endpoint
                missing.append(
                    MissingEvidence(
                        kind="missing_causal_predecessor",
                        event_id=predecessor,
                        session_id=event.primary_session_id,
                        endpoint=endpoint,
                        detail=(
                            f"event {event.event_id!r} depends on "
                            f"predecessor {predecessor!r}, which has not been ingested"
                        ),
                    )
                )
                if endpoint:
                    relevant_endpoints.add(endpoint)

            # A close/transfer can arrive before the derivation that connects
            # it to this root.  If its missing predecessor is owned by an
            # endpoint that is explicitly relevant to this barrier, fail
            # closed and name that endpoint rather than issuing a vacuous
            # receipt.  Events from endpoints outside the barrier do not block
            # this root, so unrelated backlogs remain isolated.
            if conservative_unlinked_predecessors and used_barrier is not None:
                for event_id in sorted(self.events):
                    event = self.events[event_id]
                    predecessor = event.predecessor_id
                    if (
                        not predecessor
                        or predecessor in self.events
                        or predecessor in reported_predecessor_gaps
                        or event_id in scope_event_ids
                    ):
                        continue
                    endpoint = event.predecessor_endpoint or event.issuer
                    if endpoint is None and event.from_endpoint:
                        endpoint = event.from_endpoint
                    if endpoint is None:
                        continue
                    if used_barrier.required_sequence(endpoint) is None:
                        continue
                    reported_predecessor_gaps.add(predecessor)
                    relevant_endpoints.add(endpoint)
                    missing.append(
                        MissingEvidence(
                            kind="unlinked_causal_predecessor",
                            event_id=predecessor,
                            session_id=event.primary_session_id,
                            endpoint=endpoint,
                            detail=(
                                f"early event {event.event_id!r} is causally incomplete "
                                f"without {predecessor!r}; its relation to "
                                f"{root_session_id!r} cannot yet be excluded"
                            ),
                        )
                    )

        # Barrier evidence is evaluated after graph/causal evidence so an
        # endpoint inferred from a late event is included.
        if used_barrier is None:
            missing.append(
                MissingEvidence(
                    kind="barrier_not_configured",
                    session_id=root_session_id,
                    detail="no revocation barrier coordinates were supplied",
                )
            )
        else:
            for endpoint in sorted(relevant_endpoints):
                required = used_barrier.required_sequence(endpoint)
                if required is None:
                    missing.append(
                        MissingEvidence(
                            kind="barrier_position_missing",
                            endpoint=endpoint,
                            required=None,
                            actual=self.watermarks.get(endpoint),
                            detail="endpoint is relevant but has no barrier coordinate",
                        )
                    )
                    continue
                actual = self.watermarks.get(endpoint)
                actual_number = actual if actual is not None else 0
                if actual_number < required:
                    missing.append(
                        MissingEvidence(
                            kind="watermark_behind_barrier",
                            endpoint=endpoint,
                            required=required,
                            actual=actual,
                            detail=(
                                f"endpoint {endpoint!r} watermark "
                                f"{actual if actual is not None else 'missing'} "
                                f"is behind barrier {required}"
                            ),
                        )
                    )

        # Stable output ordering while preserving all missing items.
        missing.sort(key=lambda item: (item.kind, item.endpoint or "", item.session_id or "", item.transfer_id or "", item.event_id or ""))
        relevant_endpoint_tuple = tuple(sorted(relevant_endpoints))
        relevant_watermarks = {
            endpoint: self.watermarks[endpoint]
            for endpoint in relevant_endpoint_tuple
            if endpoint in self.watermarks
        }

        receipt: CompletionReceipt | None = None
        complete = not missing
        if complete:
            digest = self._evidence_digest(
                root_session_id,
                sorted(scope_event_ids),
                closed_descendants,
                [transfer.transfer_id for transfer in relevant_transfers],
                relevant_endpoint_tuple,
                relevant_watermarks,
            )
            receipt = CompletionReceipt(
                receipt_id=f"logout-receipt:{root_session_id}:{digest[:16]}",
                root_session_id=root_session_id,
                descendant_count=len(descendants),
                closed_session_ids=tuple(closed_descendants),
                transfer_ids=tuple(sorted(transfer.transfer_id for transfer in relevant_transfers)),
                relevant_endpoints=relevant_endpoint_tuple,
                watermarks=relevant_watermarks,
                evidence_digest=digest,
            )

        return LogoutDecision(
            root_session_id=root_session_id,
            complete=complete,
            root_known=root_known,
            descendant_count=len(descendants),
            closed_descendant_ids=tuple(closed_descendants),
            open_descendant_ids=tuple(open_descendants),
            in_flight_transfer_ids=tuple(in_flight),
            relevant_endpoints=relevant_endpoint_tuple,
            watermarks=relevant_watermarks,
            missing=tuple(missing),
            receipt=receipt,
        )

    @staticmethod
    def _evidence_digest(
        root_session_id: str,
        event_ids: list[str],
        closed_ids: list[str],
        transfer_ids: list[str],
        endpoints: tuple[str, ...],
        watermarks: Mapping[str, int],
    ) -> str:
        material = json.dumps(
            {
                "root": root_session_id,
                "events": event_ids,
                "closed": closed_ids,
                "transfers": transfer_ids,
                "endpoints": list(endpoints),
                "watermarks": dict(sorted(watermarks.items())),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def issue_completion_receipt(
        self,
        root_session_id: str,
        barrier: int | Mapping[str, int] | RevocationBarrier | None = None,
        **kwargs: Any,
    ) -> CompletionReceipt:
        """Evaluate and return the receipt, raising if evidence is incomplete."""

        decision = self.evaluate(root_session_id, barrier, **kwargs)
        if decision.receipt is None:
            raise CompletionNotReady(decision)
        return decision.receipt


class CompletionNotReady(LogoutDecisionError):
    """Raised when a caller asks for a receipt before evidence is complete."""

    def __init__(self, decision: LogoutDecision) -> None:
        self.decision = decision
        endpoints = ", ".join(decision.missing_endpoints) or "none named"
        super().__init__(
            f"logout completion for {decision.root_session_id!r} is not ready; "
            f"missing: {', '.join(decision.reason_codes)}; "
            f"endpoints owing evidence: {endpoints}"
        )


def decide_logout_completion(
    root_session_id: str,
    events: Iterable[Event | Mapping[str, Any]] | None = None,
    watermarks: Mapping[str, Any] | Iterable[Any] | None = None,
    barrier: int | Mapping[str, int] | RevocationBarrier | None = None,
    **kwargs: Any,
) -> LogoutDecision:
    """One-shot functional API.

    Build a fresh :class:`LogoutDecider`, ingest synthetic or observed evidence,
    update watermarks, and evaluate ``root_session_id``.
    """

    decider = LogoutDecider(events=events, watermarks=watermarks, barrier=barrier)
    return decider.evaluate(root_session_id, **kwargs)
