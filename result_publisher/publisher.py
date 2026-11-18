"""Result publisher: cached, quota-guarded, single-flight publications.

Guarantees implemented here
---------------------------
1. **Same semantics -> same result.** Queries with equal canonical ids,
   executed against the same snapshot and the same authorization scope
   (dataset + approved purpose), return the very same
   :class:`~result_publisher.models.PublishedResult`.
2. **Snapshots invalidate cache.** A new snapshot id is a different cache
   key, so quota is charged again for the new publication.
3. **No duplicate generation under concurrency.** Concurrent identical
   requests are coalesced (single flight): exactly one thread reserves
   quota and invokes the noise adapter; every other thread waits and
   receives the same result (or the same failure).
4. **Failures release reserved quota.** If the adapter raises, the
   reservation is released and no cache entry is created; a later retry
   can therefore reuse quota and publish successfully.
5. **No intermediate values on failure.** Results enter the cache only
   after the adapter has returned successfully and the reservation has
   been committed. A request that fails (or is rejected on quota) can
   never be observed by another caller, and the adapter is never invoked
   when quota is insufficient.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .adapters import NoiseAdapter
from .budget import BudgetRegistry, BudgetScope
from .errors import PublicationError
from .models import CacheKey, DataSnapshot, PublishedResult, Query

CostLookup = Mapping[str, float] | Callable[[Query], float]

_RUNNING = "running"
_SUCCEEDED = "succeeded"
_FAILED = "failed"


@dataclass
class _InFlight:
    """Bookkeeping for one coalesced publication.

    A failed entry stays in the registry until a later retry replaces it.
    That is deliberate: threads whose task only starts after the leader
    finished must still observe the shared failure instead of each
    launching a fresh publication. A succeeded entry is removed at once
    because its result is already in the cache and serves future callers.
    """

    event: threading.Event
    state: str = _RUNNING
    result: PublishedResult | None = None
    error: BaseException | None = None


class ResultPublisher:
    """Publishes noisy aggregate results under privacy quotas."""

    def __init__(
        self,
        budgets: BudgetRegistry,
        adapter: NoiseAdapter,
        costs: CostLookup,
    ) -> None:
        self._budgets = budgets
        self._adapter = adapter
        self._costs = costs
        # One lock guards every piece of mutable publisher state. The
        # noise adapter runs *outside* the lock, so a slow adapter never
        # blocks unrelated (different cache key) publications beyond the
        # brief bookkeeping sections.
        self._lock = threading.Lock()
        self._cache: dict[CacheKey, PublishedResult] = {}
        self._inflight: dict[CacheKey, _InFlight] = {}
        self._attempts: dict[CacheKey, int] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def publish(
        self, query: Query, snapshot: DataSnapshot
    ) -> PublishedResult:
        """Return the cached result for ``query`` or publish a new one."""

        key = CacheKey.from_parts(query, snapshot)

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            flight = self._inflight.get(key)
            if flight is None or flight.state == _FAILED:
                # Either no publication has started yet, or the previous
                # attempt failed (its entry is kept so late threads still
                # observe that failure). This thread becomes the leader of
                # a fresh attempt; waiters of the old flight keep waiting
                # on the old event.
                flight = _InFlight(threading.Event())
                self._inflight[key] = flight
                leader = True
            else:
                leader = False

        if not leader:
            return self._wait_for(flight)

        return self._publish_as_leader(key, query, snapshot, flight)

    def get_cached(
        self, query: Query, snapshot: DataSnapshot
    ) -> PublishedResult | None:
        """Return the cached publication without generating anything."""

        key = CacheKey.from_parts(query, snapshot)
        with self._lock:
            return self._cache.get(key)

    def is_in_flight(self, query: Query, snapshot: DataSnapshot) -> bool:
        """Whether a publication for this exact key is currently running."""

        key = CacheKey.from_parts(query, snapshot)
        with self._lock:
            flight = self._inflight.get(key)
            return flight is not None and flight.state == _RUNNING

    def stats(self) -> dict[str, Any]:
        """Snapshot of counters, mainly for diagnostics and tests."""

        with self._lock:
            return {
                "cached_entries": len(self._cache),
                "in_flight": sum(
                    1 for flight in self._inflight.values()
                    if flight.state == _RUNNING
                ),
                "total_attempts": sum(self._attempts.values()),
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_cost(self, query: Query) -> float:
        costs = self._costs
        if callable(costs):
            cost = float(costs(query))
        else:
            try:
                cost = float(costs[query.canonical_id])
            except KeyError:
                raise PublicationError(
                    "no quota cost configured for query "
                    f"{query.canonical_id[:12]}..."
                ) from None
        if cost < 0:
            raise ValueError("query cost must be non-negative")
        return cost

    def _wait_for(self, flight: _InFlight) -> PublishedResult:
        flight.event.wait()
        # The terminal entry is read without the lock: the leader mutates
        # state/result/error before setting the event, and the event
        # establishes the happens-before edge for these writes.
        if flight.state == _FAILED:
            assert flight.error is not None
            # Every waiter observes the leader's failure; no intermediate
            # value exists because nothing was cached.
            raise flight.error
        assert flight.state == _SUCCEEDED and flight.result is not None
        return flight.result

    def _publish_as_leader(
        self,
        key: CacheKey,
        query: Query,
        snapshot: DataSnapshot,
        flight: _InFlight,
    ) -> PublishedResult:
        reservation = None
        try:
            cost = self._resolve_cost(query)
            scope = BudgetScope(query.dataset, query.purpose)
            budget = self._budgets.get(scope)
            # Raises QuotaExceededError before the adapter is ever called.
            reservation = budget.reserve(cost)

            # Local noise generation runs without holding the publisher
            # lock; the in-flight slot already guarantees single flight.
            try:
                value = self._adapter.add_noise(query, snapshot)
            except Exception as exc:  # noqa: BLE001 - re-raised uniformly
                raise PublicationError(
                    "noise adapter failed while publishing query "
                    f"{query.canonical_id[:12]}..."
                ) from exc

            with self._lock:
                reservation.commit()
                self._attempts[key] = self._attempts.get(key, 0) + 1
                result = PublishedResult(
                    key=key,
                    value=value,
                    cost=cost,
                    attempts=self._attempts[key],
                )
                # Publish only after commit: readers never see a value
                # whose quota has not been consumed. The succeeded entry
                # is removed and the event is set in the same lock section,
                # so waiters cannot miss the completion; future callers are
                # served from the cache above.
                self._cache[key] = result
                flight.state = _SUCCEEDED
                flight.result = result
                del self._inflight[key]
                flight.event.set()
            return result
        except BaseException as exc:
            # Quota rejection, unknown scope, bad cost, or adapter failure:
            # roll the reservation back and wake every waiter with the same
            # error, leaving no cache entry behind. The failed entry is
            # intentionally retained so threads starting late still join
            # this failure instead of generating duplicate attempts; a
            # deliberate retry replaces it with a fresh flight.
            if reservation is not None and reservation.is_open:
                reservation.release()
            with self._lock:
                flight.state = _FAILED
                flight.error = exc
                flight.event.set()
            raise
