"""Account + source two-layer sliding-window quota adjudicator.

The adjudicator is an in-process object: callers pass the clock value, tenant,
canonical account name, source address, and request id on every call. A request
is admitted only when *both* layers have room; a denial consumes nothing.

Determinism notes:

* Window membership is half-open: a request admitted at ``t`` frees its slot at
  exactly ``t + window_seconds``.
* Retries are explicit: every decision is recorded under ``(tenant,
  request_id)``. Replaying the same id inside the dedup TTL returns the first
  decision verbatim (marked ``deduplicated``) and consumes nothing; replaying it
  with a different account or source raises :class:`RequestIdConflict`.
* State is per tenant: counters for account and source layers are keyed by
  ``(tenant, account)`` / ``(tenant, source)``.
* One internal lock guards all state, so concurrent callers cannot both spend
  the last unit of a layer.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .config import LayerConfig, QuotaConfig, require_finite_number
from .errors import ClockRegression, RequestIdConflict, ValidationError
from .identity import (
    canonical_source_address,
    require_canonical_account,
    require_request_id,
    require_tenant,
)
from .window import RollingWindow

ACCOUNT_LAYER = "account"
SOURCE_LAYER = "source"


@dataclass(frozen=True)
class LayerStatus:
    """Per-layer state observed while making one decision."""

    layer: str
    limit: int
    window_seconds: float
    used: int
    remaining: int
    blocked: bool
    retry_at: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "limit": self.limit,
            "window_seconds": self.window_seconds,
            "used": self.used,
            "remaining": self.remaining,
            "blocked": self.blocked,
            "retry_at": self.retry_at,
        }


@dataclass(frozen=True)
class Decision:
    """The immutable outcome of one adjudication (or a deduplicated replay)."""

    allowed: bool
    tenant: str
    account: str
    source: str
    request_id: str
    decided_at: float
    reason: str
    blocked_layers: tuple[str, ...]
    retry_at: float | None
    layers: tuple[LayerStatus, ...]
    deduplicated: bool
    first_decided_at: float

    def layer(self, name: str) -> LayerStatus:
        for status in self.layers:
            if status.layer == name:
                return status
        raise KeyError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": "allow" if self.allowed else "deny",
            "allowed": self.allowed,
            "tenant": self.tenant,
            "account": self.account,
            "source": self.source,
            "request_id": self.request_id,
            "decided_at": self.decided_at,
            "first_decided_at": self.first_decided_at,
            "deduplicated": self.deduplicated,
            "reason": self.reason,
            "blocked_layers": list(self.blocked_layers),
            "retry_at": self.retry_at,
            "layers": [status.to_dict() for status in self.layers],
        }


@dataclass
class _DedupEntry:
    account: str
    source: str
    decided_at: float
    decision: Decision
    hits: int = 0


class QuotaAdjudicator:
    """Thread-safe two-layer rolling-window quota adjudicator."""

    def __init__(self, config: QuotaConfig) -> None:
        if not isinstance(config, QuotaConfig):
            raise ValidationError("config must be a QuotaConfig")
        self._config = config
        self._lock = threading.Lock()
        self._account_windows: dict[tuple[str, str], RollingWindow] = {}
        self._source_windows: dict[tuple[str, str], RollingWindow] = {}
        self._dedup: OrderedDict[tuple[str, str], _DedupEntry] = OrderedDict()
        self._tenant_last_seen: dict[str, float] = {}

    @property
    def config(self) -> QuotaConfig:
        return self._config

    # -- public API ---------------------------------------------------------

    def adjudicate(
        self, *, tenant: str, account: str, source: str, request_id: str, now: float
    ) -> Decision:
        """Decide one login attempt.

        Raises :class:`RequestIdConflict` when ``request_id`` was already used
        for this tenant with a different account/source inside the dedup TTL,
        and :class:`ClockRegression` when a *new* decision is attempted for a
        tenant at an earlier instant than its previous decision.
        """

        tenant = require_tenant(tenant)
        account = require_canonical_account(account)
        source = canonical_source_address(source)
        request_id = require_request_id(request_id)
        self._validate_now(now)

        with self._lock:
            entry = self._lookup_dedup(tenant, request_id, now)
            if entry is not None:
                if entry.account != account or entry.source != source:
                    raise RequestIdConflict(
                        tenant=tenant,
                        request_id=request_id,
                        existing_account=entry.account,
                        existing_source=entry.source,
                        incoming_account=account,
                        incoming_source=source,
                        existing_decided_at=entry.decided_at,
                    )
                entry.hits += 1
                return _as_replay(entry.decision)

            last_seen = self._tenant_last_seen.get(tenant)
            if last_seen is not None and now < last_seen:
                raise ClockRegression(tenant=tenant, last_seen=last_seen, received=now)

            decision = self._decide(tenant, account, source, request_id, now)
            self._tenant_last_seen[tenant] = now
            self._dedup[(tenant, request_id)] = _DedupEntry(
                account=account, source=source, decided_at=now, decision=decision
            )
            return decision

    def sweep(self, now: float) -> None:
        """Drop expired windows and dedup entries (state hygiene, not accounting)."""

        self._validate_now(now)
        with self._lock:
            self._sweep_locked(now)

    def snapshot(self) -> dict[str, Any]:
        """Best-effort introspection of live state; intended for diagnostics."""

        with self._lock:
            return {
                "config": self._config.to_dict(),
                "account_windows": {
                    f"{tenant}\x1f{account}": len(window)
                    for (tenant, account), window in self._account_windows.items()
                },
                "source_windows": {
                    f"{tenant}\x1f{source}": len(window)
                    for (tenant, source), window in self._source_windows.items()
                },
                "dedup_entries": len(self._dedup),
            }

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _validate_now(now: Any) -> float:
        return require_finite_number(now, "now", minimum=0.0, exclusive=False)

    def _lookup_dedup(
        self, tenant: str, request_id: str, now: float
    ) -> _DedupEntry | None:
        key = (tenant, request_id)
        entry = self._dedup.get(key)
        if entry is None:
            return None
        if now < entry.decided_at + self._config.dedup_ttl_seconds:
            return entry
        del self._dedup[key]
        return None

    def _decide(
        self, tenant: str, account: str, source: str, request_id: str, now: float
    ) -> Decision:
        account_window = self._window_for(self._account_windows, tenant, account, self._config.account)
        source_window = self._window_for(self._source_windows, tenant, source, self._config.source)

        account_status = self._measure(ACCOUNT_LAYER, account_window, self._config.account, now)
        source_status = self._measure(SOURCE_LAYER, source_window, self._config.source, now)

        blocked: list[str] = []
        if account_status.blocked:
            blocked.append(ACCOUNT_LAYER)
        if source_status.blocked:
            blocked.append(SOURCE_LAYER)

        if not blocked:
            account_window.record(now)
            source_window.record(now)
            admitted_statuses = (
                self._admitted_status(ACCOUNT_LAYER, account_window, self._config.account),
                self._admitted_status(SOURCE_LAYER, source_window, self._config.source),
            )
            return Decision(
                allowed=True,
                tenant=tenant,
                account=account,
                source=source,
                request_id=request_id,
                decided_at=now,
                reason="admitted",
                blocked_layers=(),
                retry_at=None,
                layers=admitted_statuses,
                deduplicated=False,
                first_decided_at=now,
            )

        retry_at = max(
            status.retry_at for status in (account_status, source_status) if status.blocked
        )
        reason = f"{'_and_'.join(blocked)}_rate_limited"
        return Decision(
            allowed=False,
            tenant=tenant,
            account=account,
            source=source,
            request_id=request_id,
            decided_at=now,
            reason=reason,
            blocked_layers=tuple(blocked),
            retry_at=retry_at,
            layers=(account_status, source_status),
            deduplicated=False,
            first_decided_at=now,
        )

    @staticmethod
    def _window_for(
        store: dict[tuple[str, str], RollingWindow],
        tenant: str,
        key: str,
        config: LayerConfig,
    ) -> RollingWindow:
        full_key = (tenant, key)
        window = store.get(full_key)
        if window is None:
            window = RollingWindow(config.window_seconds)
            store[full_key] = window
        return window

    @staticmethod
    def _measure(
        name: str, window: RollingWindow, config: LayerConfig, now: float
    ) -> LayerStatus:
        window.prune(now)
        used = window.count()
        blocked = used >= config.limit
        return LayerStatus(
            layer=name,
            limit=config.limit,
            window_seconds=config.window_seconds,
            used=used,
            remaining=max(config.limit - used, 0),
            blocked=blocked,
            retry_at=window.expire_at(now) if blocked else None,
        )

    @staticmethod
    def _admitted_status(
        name: str, window: RollingWindow, config: LayerConfig
    ) -> LayerStatus:
        """Status after a successful record; windows were just pruned at ``now``."""

        return LayerStatus(
            layer=name,
            limit=config.limit,
            window_seconds=config.window_seconds,
            used=window.count(),
            remaining=max(config.limit - window.count(), 0),
            blocked=False,
            retry_at=None,
        )

    def _sweep_locked(self, now: float) -> None:
        # Prune events, then drop buckets that are now empty. Deleting an empty
        # window is indistinguishable from keeping it (a fresh window starts
        # empty and is pruned at `now` anyway), so this reclaims memory for
        # account/source identifiers that will never appear again.
        for store in (self._account_windows, self._source_windows):
            for window in store.values():
                window.prune(now)
            for key in [k for k, window in store.items() if window.count() == 0]:
                del store[key]
        ttl = self._config.dedup_ttl_seconds
        for key in [k for k, entry in self._dedup.items() if now >= entry.decided_at + ttl]:
            del self._dedup[key]


def _as_replay(decision: Decision) -> Decision:
    """Return the first decision again, flagged as a deduplicated replay."""

    return Decision(
        allowed=decision.allowed,
        tenant=decision.tenant,
        account=decision.account,
        source=decision.source,
        request_id=decision.request_id,
        decided_at=decision.decided_at,
        reason=decision.reason,
        blocked_layers=decision.blocked_layers,
        retry_at=decision.retry_at,
        layers=decision.layers,
        deduplicated=True,
        first_decided_at=decision.first_decided_at,
    )
