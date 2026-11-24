"""Two-layer error-budget ledger: authoritative service facts and incident splits."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Literal

from .allocation import (
    allocate_with_pending,
    diff_allocations,
    largest_remainder_allocation,
    normalize_amount,
    normalize_weights,
    validate_identifier,
)
from .errors import (
    DuplicateRequestError,
    IncidentNotFoundError,
    InvalidWeightsError,
    LedgerInvariantError,
    RequestNotFoundError,
    RevocationExceedsBalanceError,
    ValidationError,
)

_UNSET = object()
ReallocatePolicy = bool | Literal["auto"]


@dataclass(frozen=True)
class LedgerEntry:
    """One signed account movement.

    Positive amounts increase a deduction balance; negative amounts reverse or
    reduce it.  Service and pending entries use ``service``/``period``; incident
    entries use ``incident_id``.
    """

    sequence: int
    request_id: Hashable
    layer: Literal["service", "incident", "pending"]
    delta: int
    reason: str
    service: str | None = None
    period: str | None = None
    incident_id: Hashable | None = None
    version: Hashable | None = None


@dataclass(frozen=True)
class Reconciliation:
    service_total: int
    incident_total: int
    pending_total: int
    request_count: int

    @property
    def distribution_total(self) -> int:
        return self.incident_total + self.pending_total

    @property
    def difference(self) -> int:
        return self.service_total - self.distribution_total

    @property
    def balanced(self) -> bool:
        return self.difference == 0


@dataclass
class _RequestState:
    request_id: Hashable
    service: str
    period: str
    amount: int
    allocation: dict[Hashable, int] = field(default_factory=dict)
    pending: int = 0
    weights: dict[Hashable, Fraction] | None = None
    billing_revision: int = 0
    attribution_revision: int = 0
    versions: list[dict[str, Any]] = field(default_factory=list)


class ErrorBudgetLedger:
    """Append-only delta ledger with a service layer and an incident/pending layer.

    Every successful public operation leaves ``sum(incident balances) +
    pending balance == sum(service charges)``.
    """

    def __init__(self) -> None:
        self._requests: dict[Hashable, _RequestState] = {}
        self._entries: list[LedgerEntry] = []

    # ------------------------------------------------------------------
    # Basic access
    # ------------------------------------------------------------------
    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    def _next_sequence(self) -> int:
        return len(self._entries) + 1

    def _append(
        self,
        request_id: Hashable,
        layer: Literal["service", "incident", "pending"],
        delta: int,
        reason: str,
        *,
        service: str | None = None,
        period: str | None = None,
        incident_id: Hashable | None = None,
        version: Hashable | None = None,
    ) -> LedgerEntry:
        entry = LedgerEntry(
            sequence=self._next_sequence(),
            request_id=request_id,
            layer=layer,
            delta=delta,
            reason=reason,
            service=service,
            period=period,
            incident_id=incident_id,
            version=version,
        )
        self._entries.append(entry)
        return entry

    def _get_request(self, request_id: Any) -> _RequestState:
        request_id = validate_identifier(request_id)
        try:
            return self._requests[request_id]
        except KeyError as exc:
            raise RequestNotFoundError(f"unknown request: {request_id!r}") from exc

    @staticmethod
    def _validate_text(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field_name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _has_positive_weight(weights: Mapping[Hashable, Fraction] | None) -> bool:
        return bool(weights) and any(weight > 0 for weight in weights.values())

    def get_request(self, request_id: Any) -> dict[str, Any]:
        state = self._get_request(request_id)
        return {
            "request_id": state.request_id,
            "service": state.service,
            "period": state.period,
            "amount": state.amount,
            "allocation": dict(state.allocation),
            "pending": state.pending,
            "weights": dict(state.weights) if state.weights is not None else None,
            "billing_revision": state.billing_revision,
            "attribution_revision": state.attribution_revision,
            "versions": [dict(item) for item in state.versions],
        }

    def list_requests(self) -> list[dict[str, Any]]:
        return [self.get_request(request_id) for request_id in sorted(self._requests, key=str)]

    # ------------------------------------------------------------------
    # Authoritative billing fact
    # ------------------------------------------------------------------
    def register_request(
        self,
        request_id: Hashable,
        service: str,
        period: str,
        amount: Any,
        *,
        weights: Mapping[Any, Any] | Iterable[tuple[Any, Any]] | None = None,
        version: Hashable | None = None,
    ) -> dict[str, Any]:
        """Register a confirmed request and charge its service exactly once."""

        request_id = validate_identifier(request_id)
        if request_id in self._requests:
            raise DuplicateRequestError(f"request already registered: {request_id!r}")

        service_name = self._validate_text(service, "service")
        billing_period = self._validate_text(period, "period")
        units = normalize_amount(amount)
        normalized_weights = normalize_weights(weights)
        target_allocation, target_pending = allocate_with_pending(units, normalized_weights)
        effective_weights = (
            normalized_weights if self._has_positive_weight(normalized_weights) else None
        )

        state = _RequestState(
            request_id=request_id,
            service=service_name,
            period=billing_period,
            amount=units,
            weights=effective_weights,
        )
        self._requests[request_id] = state

        # The one authoritative service charge for this request.
        self._append(
            request_id,
            "service",
            units,
            "confirmed_charge",
            service=service_name,
            period=billing_period,
            version=version,
        )

        reason = "initial_attribution" if effective_weights is not None else "missing_attribution"
        self._transition_distribution(
            state,
            target_allocation,
            target_pending,
            reason=reason,
            version=version,
        )
        state.attribution_revision = 1 if effective_weights is not None else 0
        if effective_weights is not None:
            state.versions.append(
                {
                    "revision": 1,
                    "version": version,
                    "weights": dict(effective_weights),
                    "allocation": dict(target_allocation),
                    "pending": target_pending,
                    "reason": reason,
                }
            )

        self.assert_reconciled()
        return self.get_request(request_id)

    # A domain-language alias.
    confirm_charge = register_request

    def correct_billing(
        self,
        request_id: Hashable,
        *,
        amount: Any = _UNSET,
        service: Any = _UNSET,
        period: Any = _UNSET,
        reallocate: ReallocatePolicy = "auto",
    ) -> dict[str, Any]:
        """Correct an authoritative billing fact.

        This is the only operation that posts a correction to a service account.
        In ``"auto"`` mode an active weight map causes the new amount to be
        reallocated by the same largest-remainder rule; otherwise the delta is
        preserved outside existing incident balances (added to pending, with
        pending used first and pro-rata clawback only for a decrease larger
        than pending).
        """

        state = self._get_request(request_id)
        old_service = state.service
        old_period = state.period
        old_amount = state.amount
        old_pending = state.pending

        new_service = (
            old_service
            if service is _UNSET
            else self._validate_text(service, "service")
        )
        new_period = (
            old_period if period is _UNSET else self._validate_text(period, "period")
        )
        new_amount = old_amount if amount is _UNSET else normalize_amount(amount)

        # Resolve and validate the distribution target before appending any
        # entries so a rejected correction cannot leave a partial posting.
        should_reallocate = self._resolve_reallocate_policy(reallocate, state)
        if should_reallocate:
            target_allocation = largest_remainder_allocation(new_amount, state.weights)
            target_pending = new_amount - sum(target_allocation.values())
        else:
            target_allocation, target_pending = self._preserve_distribution_for_amount(
                state, new_amount
            )

        account_changed = (new_service, new_period) != (old_service, old_period)

        # Service-layer correction.  An account-key change is posted gross so
        # both old and new service/period buckets have an auditable movement.
        if account_changed:
            if old_amount:
                self._append(
                    state.request_id,
                    "service",
                    -old_amount,
                    "billing_correction_account_reversal",
                    service=old_service,
                    period=old_period,
                )
            if new_amount:
                self._append(
                    state.request_id,
                    "service",
                    new_amount,
                    "billing_correction_account_repost",
                    service=new_service,
                    period=new_period,
                )
        else:
            service_delta = new_amount - old_amount
            if service_delta:
                self._append(
                    state.request_id,
                    "service",
                    service_delta,
                    "billing_correction",
                    service=old_service,
                    period=old_period,
                )

        self._append_incident_deltas(
            state,
            target_allocation,
            reason="billing_correction",
        )

        if account_changed:
            if old_pending:
                self._append(
                    state.request_id,
                    "pending",
                    -old_pending,
                    "billing_correction_pending_move",
                    service=old_service,
                    period=old_period,
                )
            if target_pending:
                self._append(
                    state.request_id,
                    "pending",
                    target_pending,
                    "billing_correction_pending_move",
                    service=new_service,
                    period=new_period,
                )
        else:
            pending_delta = target_pending - old_pending
            if pending_delta:
                self._append(
                    state.request_id,
                    "pending",
                    pending_delta,
                    "billing_correction",
                    service=old_service,
                    period=old_period,
                )

        state.service = new_service
        state.period = new_period
        state.amount = new_amount
        state.allocation = self._merge_allocation(state, target_allocation)
        state.pending = target_pending
        state.billing_revision += 1
        self._assert_request_invariant(state)
        self.assert_reconciled()
        return self.get_request(state.request_id)

    @staticmethod
    def _resolve_reallocate_policy(
        policy: ReallocatePolicy,
        state: _RequestState,
    ) -> bool:
        if policy == "auto":
            return ErrorBudgetLedger._has_positive_weight(state.weights)
        if policy is True:
            if not ErrorBudgetLedger._has_positive_weight(state.weights):
                raise InvalidWeightsError(
                    "cannot reallocate a correction without a positive active weight map"
                )
            return True
        if policy is False:
            return False
        raise ValidationError("reallocate must be True, False, or 'auto'")

    @staticmethod
    def _preserve_distribution_for_amount(
        state: _RequestState,
        new_amount: int,
    ) -> tuple[dict[Hashable, int], int]:
        allocation = dict(state.allocation)
        pending = state.pending
        reduction = state.amount - new_amount

        if reduction <= 0:
            return allocation, pending + (-reduction)

        from_pending = min(pending, reduction)
        pending -= from_pending
        reduction -= from_pending

        if reduction > 0:
            clawback_weights = {
                incident_id: balance
                for incident_id, balance in allocation.items()
                if balance > 0
            }
            reductions = largest_remainder_allocation(reduction, clawback_weights)
            for incident_id, removed in reductions.items():
                current = allocation.get(incident_id, 0)
                if removed > current:
                    raise LedgerInvariantError("pro-rata correction would make an incident negative")
                allocation[incident_id] = current - removed

        return allocation, pending

    # ------------------------------------------------------------------
    # Attribution
    # ------------------------------------------------------------------
    def attribute(
        self,
        request_id: Hashable,
        weights: Mapping[Any, Any] | Iterable[tuple[Any, Any]] | None = None,
        *,
        version: Hashable | None = None,
        reason: str = "attribution_revision",
    ) -> dict[str, Any]:
        """Replace the complete attribution version and post only account deltas."""

        state = self._get_request(request_id)
        normalized_weights = normalize_weights(weights)
        target_allocation, target_pending = allocate_with_pending(
            state.amount, normalized_weights
        )
        effective_weights = (
            normalized_weights if self._has_positive_weight(normalized_weights) else None
        )

        self._transition_distribution(
            state,
            target_allocation,
            target_pending,
            reason=reason,
            version=version,
        )
        state.weights = effective_weights
        state.attribution_revision += 1
        state.versions.append(
            {
                "revision": state.attribution_revision,
                "version": version,
                "weights": dict(effective_weights) if effective_weights else None,
                "allocation": dict(target_allocation),
                "pending": target_pending,
                "reason": reason,
            }
        )
        self.assert_reconciled()
        return self.get_request(state.request_id)

    # Readable aliases for a complete new attribution version.
    apply_attribution = attribute
    reattribute = attribute

    def attribute_pending(
        self,
        request_id: Hashable,
        weights: Mapping[Any, Any] | Iterable[tuple[Any, Any]],
        *,
        version: Hashable | None = None,
    ) -> dict[str, Any]:
        """Attribute only the current pending pool without changing old splits."""

        state = self._get_request(request_id)
        normalized_weights = normalize_weights(weights)
        if not self._has_positive_weight(normalized_weights):
            raise InvalidWeightsError("at least one positive weight is required")

        pool = state.pending
        extra = largest_remainder_allocation(pool, normalized_weights)

        target_allocation = dict(state.allocation)
        for incident_id, units in extra.items():
            target_allocation[incident_id] = target_allocation.get(incident_id, 0) + units

        self._transition_distribution(
            state,
            target_allocation,
            0,
            reason="pending_attribution",
            version=version,
        )

        if pool > 0:
            # The resulting split is a combination of attribution actions, not
            # one complete weight formula for the whole request.
            state.weights = None
        state.attribution_revision += 1
        state.versions.append(
            {
                "revision": state.attribution_revision,
                "version": version,
                "weights": dict(normalized_weights),
                "allocation": dict(target_allocation),
                "pending": 0,
                "reason": "pending_attribution",
            }
        )
        self.assert_reconciled()
        return self.get_request(state.request_id)

    def revoke_attribution(
        self,
        request_id: Hashable,
        incident_ids: Hashable | Iterable[Hashable] | None = None,
        *,
        units: Mapping[Hashable, Any] | None = None,
        all_incidents: bool = False,
        redistribute: bool = False,
        version: Hashable | None = None,
    ) -> dict[str, Any]:
        """Revoke part or all of an attribution.

        Default behavior posts the negative difference only to the selected
        incident accounts and moves the revoked units to pending.  Other
        incident balances remain untouched.

        With ``redistribute=True`` the selected ids are removed from the active
        weight map and the full current amount is allocated again among the
        remaining incidents.  All resulting incident deltas are posted.
        """

        state = self._get_request(request_id)

        if redistribute:
            if units is not None:
                raise ValidationError("units cannot be combined with redistribute=True")
            remove_ids = self._revocation_ids(incident_ids)
            if all_incidents:
                new_weights: dict[Hashable, Fraction] = {}
            else:
                if not remove_ids:
                    raise ValidationError(
                        "provide incident_ids or all_incidents=True for redistribution"
                    )
                source_weights = state.weights
                if not self._has_positive_weight(source_weights):
                    # A manual split can still be redistributed using current
                    # positive balances as exact relative weights.
                    source_weights = {
                        incident_id: Fraction(balance)
                        for incident_id, balance in state.allocation.items()
                        if balance > 0
                    }
                else:
                    source_weights = dict(source_weights)  # type: ignore[arg-type]

                new_weights = dict(source_weights)
                for incident_id in remove_ids:
                    if incident_id not in new_weights:
                        raise IncidentNotFoundError(
                            f"incident {incident_id!r} is not in the active attribution"
                        )
                    del new_weights[incident_id]

            return self.attribute(
                state.request_id,
                new_weights,
                version=version,
                reason="partial_revocation_redistribute",
            )

        revocation = self._build_revocation(
            state,
            incident_ids=incident_ids,
            units=units,
            all_incidents=all_incidents,
        )

        target_allocation = dict(state.allocation)
        revoked_total = 0
        for incident_id, remove_units in revocation.items():
            current = target_allocation.get(incident_id, 0)
            if remove_units > current:
                raise RevocationExceedsBalanceError(
                    f"cannot revoke {remove_units} units from {incident_id!r}; "
                    f"balance is {current}"
                )
            target_allocation[incident_id] = current - remove_units
            revoked_total += remove_units

        target_pending = state.pending + revoked_total
        self._transition_distribution(
            state,
            target_allocation,
            target_pending,
            reason="partial_revocation",
            version=version,
        )
        # The old complete weight map no longer describes the split after a
        # direct balance revocation.
        state.weights = None
        state.attribution_revision += 1
        state.versions.append(
            {
                "revision": state.attribution_revision,
                "version": version,
                "revoked": dict(revocation),
                "allocation": dict(target_allocation),
                "pending": target_pending,
                "reason": "partial_revocation",
            }
        )
        self.assert_reconciled()
        return self.get_request(state.request_id)

    @staticmethod
    def _revocation_ids(
        incident_ids: Hashable | Iterable[Hashable] | None,
    ) -> list[Hashable]:
        if incident_ids is None:
            return []
        if isinstance(incident_ids, (str, bytes)) or not isinstance(incident_ids, Iterable):
            return [validate_identifier(incident_ids)]
        return [validate_identifier(incident_id) for incident_id in incident_ids]

    def _build_revocation(
        self,
        state: _RequestState,
        *,
        incident_ids: Hashable | Iterable[Hashable] | None,
        units: Mapping[Hashable, Any] | None,
        all_incidents: bool,
    ) -> dict[Hashable, int]:
        if all_incidents:
            if incident_ids is not None or units is not None:
                raise ValidationError("all_incidents cannot be combined with incident_ids/units")
            return {
                incident_id: balance
                for incident_id, balance in state.allocation.items()
                if balance > 0
            }

        if units is not None:
            if incident_ids is not None:
                raise ValidationError("provide either incident_ids or units, not both")
            revocation: dict[Hashable, int] = {}
            for raw_id, raw_units in units.items():
                incident_id = validate_identifier(raw_id)
                if incident_id not in state.allocation:
                    raise IncidentNotFoundError(
                        f"incident {incident_id!r} is not attributed to request "
                        f"{state.request_id!r}"
                    )
                remove_units = normalize_amount(raw_units)
                current = state.allocation.get(incident_id, 0)
                if remove_units > current:
                    raise RevocationExceedsBalanceError(
                        f"cannot revoke {remove_units} units from {incident_id!r}; "
                        f"balance is {current}"
                    )
                if remove_units:
                    revocation[incident_id] = remove_units
            return revocation

        ids = self._revocation_ids(incident_ids)
        if not ids:
            raise ValidationError(
                "provide incident_ids, units, or all_incidents=True to revoke"
            )

        revocation = {}
        for incident_id in ids:
            if incident_id not in state.allocation:
                raise IncidentNotFoundError(
                    f"incident {incident_id!r} is not attributed to request "
                    f"{state.request_id!r}"
                )
            balance = state.allocation[incident_id]
            if balance > 0:
                revocation[incident_id] = balance
        return revocation

    # ------------------------------------------------------------------
    # Internal distribution posting
    # ------------------------------------------------------------------
    def _append_incident_deltas(
        self,
        state: _RequestState,
        target_allocation: Mapping[Hashable, int],
        *,
        reason: str,
        version: Hashable | None = None,
    ) -> list[LedgerEntry]:
        deltas = diff_allocations(state.allocation, target_allocation)
        posted: list[LedgerEntry] = []
        for incident_id, delta in deltas.items():
            posted.append(
                self._append(
                    state.request_id,
                    "incident",
                    delta,
                    reason,
                    incident_id=incident_id,
                    version=version,
                )
            )
        return posted

    def _transition_distribution(
        self,
        state: _RequestState,
        target_allocation: Mapping[Hashable, int],
        target_pending: int,
        *,
        reason: str,
        version: Hashable | None = None,
    ) -> None:
        self._append_incident_deltas(
            state,
            target_allocation,
            reason=reason,
            version=version,
        )
        pending_delta = target_pending - state.pending
        if pending_delta:
            self._append(
                state.request_id,
                "pending",
                pending_delta,
                reason,
                service=state.service,
                period=state.period,
                version=version,
            )
        state.allocation = self._merge_allocation(state, target_allocation)
        state.pending = target_pending
        self._assert_request_invariant(state)

    @staticmethod
    def _merge_allocation(
        state: _RequestState,
        target_allocation: Mapping[Hashable, int],
    ) -> dict[Hashable, int]:
        # Keep previously known incidents in the snapshot with an explicit zero
        # balance after they are removed from a newer complete weight version.
        merged = {incident_id: 0 for incident_id in state.allocation}
        merged.update(target_allocation)
        return merged

    @staticmethod
    def _assert_request_invariant(state: _RequestState) -> None:
        if state.amount < 0 or state.pending < 0:
            raise LedgerInvariantError("negative request amount or pending balance")
        if any(balance < 0 for balance in state.allocation.values()):
            raise LedgerInvariantError("negative incident allocation")
        total = sum(state.allocation.values()) + state.pending
        if total != state.amount:
            raise LedgerInvariantError(
                f"request {state.request_id!r} is out of balance: "
                f"allocations={sum(state.allocation.values())}, "
                f"pending={state.pending}, amount={state.amount}"
            )

    # ------------------------------------------------------------------
    # Balances and reconciliation
    # ------------------------------------------------------------------
    def service_balances(self) -> dict[tuple[str, str], int]:
        balances: defaultdict[tuple[str, str], int] = defaultdict(int)
        for entry in self._entries:
            if entry.layer == "service":
                assert entry.service is not None and entry.period is not None
                balances[(entry.service, entry.period)] += entry.delta
        return dict(balances)

    def pending_balances(self) -> dict[tuple[str, str], int]:
        balances: defaultdict[tuple[str, str], int] = defaultdict(int)
        for entry in self._entries:
            if entry.layer == "pending":
                assert entry.service is not None and entry.period is not None
                balances[(entry.service, entry.period)] += entry.delta
        for state in self._requests.values():
            balances.setdefault((state.service, state.period), 0)
        return dict(balances)

    def incident_balances(self) -> dict[Hashable, int]:
        balances: defaultdict[Hashable, int] = defaultdict(int)
        for entry in self._entries:
            if entry.layer == "incident":
                assert entry.incident_id is not None
                balances[entry.incident_id] += entry.delta
        for state in self._requests.values():
            for incident_id in state.allocation:
                balances.setdefault(incident_id, 0)
        return dict(balances)

    def service_balance(self, service: str, period: str) -> int:
        return self.service_balances().get((service, period), 0)

    def pending_balance(self, service: str, period: str) -> int:
        return self.pending_balances().get((service, period), 0)

    def incident_balance(self, incident_id: Hashable) -> int:
        return self.incident_balances().get(incident_id, 0)

    def reconciliation(self) -> Reconciliation:
        service_total = sum(entry.delta for entry in self._entries if entry.layer == "service")
        incident_total = sum(
            entry.delta for entry in self._entries if entry.layer == "incident"
        )
        pending_total = sum(entry.delta for entry in self._entries if entry.layer == "pending")
        return Reconciliation(
            service_total=service_total,
            incident_total=incident_total,
            pending_total=pending_total,
            request_count=len(self._requests),
        )

    totals = reconciliation

    def assert_reconciled(self) -> Reconciliation:
        service_by_request: defaultdict[Hashable, int] = defaultdict(int)
        incident_by_request: defaultdict[Hashable, int] = defaultdict(int)
        pending_by_request: defaultdict[Hashable, int] = defaultdict(int)

        for entry in self._entries:
            if entry.layer == "service":
                service_by_request[entry.request_id] += entry.delta
            elif entry.layer == "incident":
                incident_by_request[entry.request_id] += entry.delta
            else:
                pending_by_request[entry.request_id] += entry.delta

        for state in self._requests.values():
            self._assert_request_invariant(state)
            request_id = state.request_id
            allocation_total = sum(state.allocation.values())

            if service_by_request[request_id] != state.amount:
                raise LedgerInvariantError(
                    f"request {request_id!r} service entries do not match its billing amount"
                )
            if incident_by_request[request_id] != allocation_total:
                raise LedgerInvariantError(
                    f"request {request_id!r} incident entries do not match its allocation state"
                )
            if pending_by_request[request_id] != state.pending:
                raise LedgerInvariantError(
                    f"request {request_id!r} pending entries do not match its pending state"
                )

        result = self.reconciliation()
        if not result.balanced:
            raise LedgerInvariantError(
                "layers do not reconcile: "
                f"service={result.service_total}, "
                f"incidents={result.incident_total}, "
                f"pending={result.pending_total}, "
                f"difference={result.difference}"
            )

        negative_service = {
            key: value for key, value in self.service_balances().items() if value < 0
        }
        negative_pending = {
            key: value for key, value in self.pending_balances().items() if value < 0
        }
        negative_incidents = {
            key: value for key, value in self.incident_balances().items() if value < 0
        }
        if negative_service or negative_pending or negative_incidents:
            raise LedgerInvariantError(
                f"negative balances: service={negative_service}, "
                f"pending={negative_pending}, incidents={negative_incidents}"
            )
        return result

    # ------------------------------------------------------------------
    # Interleaved event ingestion
    # ------------------------------------------------------------------
    def handle_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one synthetic/event-dictionary command."""

        if not isinstance(event, Mapping):
            raise ValidationError("event must be a mapping")
        event_type = event.get("type")
        request_id = event.get("request_id")

        if event_type in {"register", "registered", "charge", "confirmed_charge"}:
            return self.register_request(
                request_id,
                event["service"],
                event["period"],
                event["amount"],
                weights=event.get("weights"),
                version=event.get("version"),
            )
        if event_type in {"attribute", "attribution", "apply_attribution"}:
            return self.attribute(
                request_id,
                event.get("weights"),
                version=event.get("version"),
            )
        if event_type == "attribute_pending":
            return self.attribute_pending(
                request_id,
                event["weights"],
                version=event.get("version"),
            )
        if event_type in {"revoke", "revoke_attribution"}:
            return self.revoke_attribution(
                request_id,
                event.get("incident_ids"),
                units=event.get("units"),
                all_incidents=event.get("all_incidents", False),
                redistribute=event.get("redistribute", False),
                version=event.get("version"),
            )
        if event_type in {"correct_billing", "billing_correction", "correction"}:
            kwargs: dict[str, Any] = {}
            if "amount" in event:
                kwargs["amount"] = event["amount"]
            if "service" in event:
                kwargs["service"] = event["service"]
            if "period" in event:
                kwargs["period"] = event["period"]
            if "reallocate" in event:
                kwargs["reallocate"] = event["reallocate"]
            return self.correct_billing(request_id, **kwargs)

        raise ValidationError(f"unknown event type: {event_type!r}")


def settle_events(
    events: Iterable[Mapping[str, Any]],
    ledger: ErrorBudgetLedger | None = None,
) -> tuple[ErrorBudgetLedger, list[dict[str, Any]]]:
    """Apply an interleaved list of synthetic events and return the ledger."""

    ledger = ledger or ErrorBudgetLedger()
    snapshots = [ledger.handle_event(event) for event in events]
    ledger.assert_reconciled()
    return ledger, snapshots
