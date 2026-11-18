"""Acceptance tests for the cached, quota-guarded ResultPublisher.

Coverage map of the cross-cutting constraints:

* acceptable result ............ test_cached_result_is_reused
* blocked/rejected result ...... test_quota_rejection_leaks_nothing
* boundary ..................... test_exact_limit_succeeds_then_next_fails
* snapshot interleaving ........ test_snapshot_change_recharges_quota
* scope interleaving ........... test_authorization_scopes_are_isolated
* concurrency single flight .... test_concurrent_identical_requests_coalesce
* failure/concurrency interleave test_concurrent_failure_propagates_and_releases
* quota contention interleave .. test_concurrent_distinct_queries_never_oversell
* retry after failure .......... test_retry_after_failure_reuses_released_quota
"""

import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from result_publisher import (  # noqa: E402
    BudgetRegistry,
    BudgetScope,
    DataSnapshot,
    PublicationError,
    PublisherError,
    Query,
    QuotaExceededError,
    ResultPublisher,
    UnknownScopeError,
)

SCOPE = BudgetScope("hr", "staffing_audit")
OTHER_SCOPE = BudgetScope("hr", "incident_review")


class RecordingAdapter:
    """Adapter that records calls and can be switched to failing mode."""

    def __init__(self, value="noisy-aggregate", fail: bool = False) -> None:
        self.value = value
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def add_noise(self, query: Query, snapshot: DataSnapshot):
        with self._lock:
            self.calls.append((query.canonical_id, snapshot.snapshot_id))
        if self.fail:
            raise RuntimeError("noise mechanism offline")
        return self.value


class BlockingAdapter:
    """Adapter that blocks until a test releases it, counting entries.

    With ``fail=True`` every invocation raises after passing the gate,
    which is how concurrent-failure tests keep the single-flight window
    open long enough for every waiter to join the same attempt.
    """

    def __init__(self, fail: bool = False) -> None:
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.fail = fail
        self.call_count = 0
        self._lock = threading.Lock()

    def add_noise(self, query: Query, snapshot: DataSnapshot):
        with self._lock:
            self.call_count += 1
            if self.call_count == 1:
                self.entered.set()
        self.entered.wait(timeout=5)
        self.gate.wait(timeout=5)
        if self.fail:
            raise RuntimeError("noise mechanism offline")
        return f"value-{snapshot.snapshot_id}"


class SlowAdapter:
    """Adapter used to force overlap between distinct publications."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def add_noise(self, query: Query, snapshot: DataSnapshot):
        with self._lock:
            self.calls += 1
        event = threading.Event()
        event.wait(timeout=self.delay)
        return f"value-{query.kind}"


def make_query(shift: str = "day", purpose: str = "staffing_audit") -> Query:
    return Query(
        dataset="hr",
        purpose=purpose,
        kind="on_duty_count",
        filters={"shift": shift, "teams": {"a", "b"}},
    )


def make_snapshot(snapshot_id: str = "snap-1", rows: int = 7) -> DataSnapshot:
    return DataSnapshot(
        dataset="hr",
        snapshot_id=snapshot_id,
        payload=[{"id": i} for i in range(rows)],
    )


def make_publisher(
    adapter,
    limit: float = 10.0,
    cost: float = 2.0,
    extra_scopes: dict[BudgetScope, float] | None = None,
):
    grants = {SCOPE: limit}
    if extra_scopes:
        grants.update(extra_scopes)
    registry = BudgetRegistry(grants)

    def costs(query: Query) -> float:
        return cost

    return ResultPublisher(registry, adapter, costs), registry


class ResultPublisherCachingTest(unittest.TestCase):
    def test_cached_result_is_reused(self):
        adapter = RecordingAdapter()
        publisher, registry = make_publisher(adapter, limit=10.0, cost=2.0)
        query = make_query()
        snapshot = make_snapshot()

        first = publisher.publish(query, snapshot)
        # A semantically identical query (different filter object layout)
        # must hit the same normalized identifier.
        equivalent = Query(
            dataset="hr",
            purpose="staffing_audit",
            kind="on_duty_count",
            filters={"teams": ["b", "a"], "shift": "day"},
        )
        second = publisher.publish(equivalent, snapshot)

        self.assertIs(first, second)  # literally the same cached object
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(first.attempts, 1)
        self.assertEqual(first.cost, 2.0)
        budget = registry.get(SCOPE)
        self.assertEqual(budget.consumed, 2.0)
        self.assertEqual(budget.reserved, 0.0)
        # get_cached never triggers generation.
        self.assertIs(publisher.get_cached(query, snapshot), first)

    def test_snapshot_change_recharges_quota(self):
        adapter = RecordingAdapter()
        publisher, registry = make_publisher(adapter, limit=10.0, cost=4.0)
        query = make_query()
        first_snapshot = make_snapshot("snap-1")
        second_snapshot = make_snapshot("snap-2")

        result_one = publisher.publish(query, first_snapshot)
        result_two = publisher.publish(query, second_snapshot)

        self.assertIsNot(result_one, result_two)
        self.assertEqual(len(adapter.calls), 2)
        # Each snapshot consumes quota independently.
        self.assertEqual(registry.get(SCOPE).consumed, 8.0)
        # The old snapshot's result is still cached and free to read.
        self.assertIs(
            publisher.get_cached(query, first_snapshot), result_one
        )
        self.assertEqual(publisher.stats()["cached_entries"], 2)

    def test_authorization_scopes_are_isolated(self):
        adapter = RecordingAdapter()
        publisher, registry = make_publisher(
            adapter,
            limit=2.0,
            cost=2.0,
            extra_scopes={OTHER_SCOPE: 10.0},
        )
        query = make_query()
        other_query = make_query(purpose="incident_review")
        snapshot = make_snapshot()

        # The staffing_audit scope is fully consumed by one publication.
        publisher.publish(query, snapshot)
        with self.assertRaises(QuotaExceededError):
            publisher.publish(
                Query(
                    dataset="hr",
                    purpose="staffing_audit",
                    kind="on_duty_count",
                    filters={"shift": "night"},
                ),
                snapshot,
            )
        # The other approved purpose still has its own full budget.
        other_result = publisher.publish(other_query, snapshot)
        self.assertEqual(other_result.value, "noisy-aggregate")
        self.assertEqual(registry.get(OTHER_SCOPE).consumed, 2.0)
        self.assertEqual(registry.get(SCOPE).consumed, 2.0)

    def test_unknown_scope_is_rejected_without_adapter_call(self):
        adapter = RecordingAdapter()
        publisher, _ = make_publisher(adapter)
        rogue = make_query(purpose="unapproved_purpose")
        snapshot = make_snapshot()
        with self.assertRaises(UnknownScopeError):
            publisher.publish(rogue, snapshot)
        self.assertEqual(adapter.calls, [])
        self.assertIsNone(publisher.get_cached(rogue, snapshot))


class ResultPublisherQuotaAndFailureTest(unittest.TestCase):
    def test_quota_rejection_leaks_nothing(self):
        adapter = RecordingAdapter()
        publisher, registry = make_publisher(adapter, limit=3.0, cost=4.0)
        query = make_query()
        snapshot = make_snapshot()

        with self.assertRaises(QuotaExceededError):
            publisher.publish(query, snapshot)

        budget = registry.get(SCOPE)
        self.assertEqual(budget.consumed, 0.0)
        self.assertEqual(budget.reserved, 0.0)
        self.assertEqual(budget.available, 3.0)
        # The adapter never ran, no result is visible, nothing in flight.
        self.assertEqual(adapter.calls, [])
        self.assertIsNone(publisher.get_cached(query, snapshot))
        self.assertFalse(publisher.is_in_flight(query, snapshot))

    def test_exact_limit_succeeds_then_next_fails(self):
        adapter = RecordingAdapter()
        publisher, registry = make_publisher(adapter, limit=5.0, cost=5.0)
        query = make_query()
        snapshot = make_snapshot()

        result = publisher.publish(query, snapshot)  # boundary: exactly full
        self.assertEqual(result.value, "noisy-aggregate")
        self.assertEqual(registry.get(SCOPE).available, 0.0)
        with self.assertRaises(QuotaExceededError):
            publisher.publish(make_query(shift="night"), snapshot)
        # The rejected interleaving request did not create an entry.
        self.assertEqual(publisher.stats()["cached_entries"], 1)

    def test_adapter_failure_releases_quota_and_caches_nothing(self):
        adapter = RecordingAdapter(fail=True)
        publisher, registry = make_publisher(adapter, limit=5.0, cost=5.0)
        query = make_query()
        snapshot = make_snapshot()

        with self.assertRaises(PublicationError) as context:
            publisher.publish(query, snapshot)
        self.assertIsInstance(context.exception.__cause__, RuntimeError)

        budget = registry.get(SCOPE)
        self.assertEqual(budget.reserved, 0.0)
        self.assertEqual(budget.consumed, 0.0)
        self.assertEqual(budget.available, 5.0)
        self.assertIsNone(publisher.get_cached(query, snapshot))
        self.assertFalse(publisher.is_in_flight(query, snapshot))

    def test_retry_after_failure_reuses_released_quota(self):
        adapter = RecordingAdapter(fail=True)
        publisher, registry = make_publisher(adapter, limit=5.0, cost=5.0)
        query = make_query()
        snapshot = make_snapshot()

        with self.assertRaises(PublicationError):
            publisher.publish(query, snapshot)
        # Repair the local noise mechanism and retry: the released
        # reservation covers the whole budget, so the retry succeeds.
        adapter.fail = False
        result = publisher.publish(query, snapshot)
        self.assertEqual(result.value, "noisy-aggregate")
        self.assertEqual(result.attempts, 1)  # only the success is counted
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(registry.get(SCOPE).consumed, 5.0)
        self.assertIs(publisher.publish(query, snapshot), result)

    def test_zero_cost_query_never_consumes_budget(self):
        adapter = RecordingAdapter()
        publisher, registry = make_publisher(adapter, limit=0.0, cost=0.0)
        result = publisher.publish(make_query(), make_snapshot())
        self.assertEqual(result.cost, 0.0)
        self.assertEqual(registry.get(SCOPE).consumed, 0.0)

    def test_snapshot_dataset_mismatch_is_rejected(self):
        adapter = RecordingAdapter()
        publisher, _ = make_publisher(adapter)
        foreign = DataSnapshot("finance", "snap-1", payload=[])
        with self.assertRaises(ValueError):
            publisher.publish(make_query(), foreign)

    def test_missing_cost_mapping_entry_is_a_publication_failure(self):
        registry = BudgetRegistry({SCOPE: 10.0})
        publisher = ResultPublisher(registry, RecordingAdapter(), {})
        snapshot = make_snapshot()
        with self.assertRaises(PublicationError):
            publisher.publish(make_query(), snapshot)
        self.assertEqual(registry.get(SCOPE).reserved, 0.0)


class ResultPublisherConcurrencyTest(unittest.TestCase):
    def test_concurrent_identical_requests_coalesce(self):
        adapter = BlockingAdapter()
        publisher, registry = make_publisher(adapter, limit=10.0, cost=3.0)
        query = make_query()
        snapshot = make_snapshot()
        results: list = []
        errors: list = []

        def worker() -> None:
            try:
                results.append(publisher.publish(query, snapshot))
            except PublisherError as exc:  # pragma: no cover - failure path
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker) for _ in range(8)]
            self.assertTrue(adapter.entered.wait(timeout=5))
            # While the leader is blocked inside the adapter, waiters see
            # an in-flight publication and no cached value yet.
            self.assertTrue(publisher.is_in_flight(query, snapshot))
            self.assertIsNone(publisher.get_cached(query, snapshot))
            adapter.gate.set()
            for future in futures:
                future.result(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(all(result is results[0] for result in results))
        # One adapter invocation, one quota charge for all eight requests.
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(registry.get(SCOPE).consumed, 3.0)
        self.assertEqual(registry.get(SCOPE).reserved, 0.0)

    def test_concurrent_failure_propagates_and_releases(self):
        # Gate the failing adapter so the in-flight window stays open until
        # all four requests have joined the single flight.
        adapter = BlockingAdapter(fail=True)
        publisher, registry = make_publisher(adapter, limit=6.0, cost=6.0)
        query = make_query()
        snapshot = make_snapshot()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(publisher.publish, query, snapshot)
                for _ in range(4)
            ]
            self.assertTrue(adapter.entered.wait(timeout=5))
            # The reservation is visible while generation is in flight,
            # and no cached intermediate value exists.
            self.assertTrue(publisher.is_in_flight(query, snapshot))
            self.assertIsNone(publisher.get_cached(query, snapshot))
            adapter.gate.set()
            for future in futures:
                with self.assertRaises(PublicationError):
                    future.result(timeout=5)

        budget = registry.get(SCOPE)
        self.assertEqual(adapter.call_count, 1)  # one shared failed attempt
        self.assertEqual(budget.reserved, 0.0)
        self.assertEqual(budget.consumed, 0.0)
        self.assertEqual(budget.available, 6.0)
        self.assertIsNone(publisher.get_cached(query, snapshot))
        self.assertFalse(publisher.is_in_flight(query, snapshot))

        # A deliberate later retry on the same publisher replaces the
        # retained failure entry and becomes a successful publication.
        healthy = RecordingAdapter()
        publisher._adapter = healthy  # repair the local noise mechanism
        result = publisher.publish(query, snapshot)
        self.assertEqual(result.value, "noisy-aggregate")
        self.assertEqual(budget.consumed, 6.0)

    def test_concurrent_distinct_queries_never_oversell(self):
        adapter = SlowAdapter(delay=0.05)
        # Budget covers only two of the three distinct queries.
        publisher, registry = make_publisher(adapter, limit=4.0, cost=2.0)
        snapshot = make_snapshot()
        queries = [
            make_query(shift=shift) for shift in ("day", "night", "swing")
        ]

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                query.filters["shift"]: pool.submit(
                    publisher.publish, query, snapshot
                )
                for query in queries
            }
            outcomes = {}
            for label, future in futures.items():
                try:
                    outcomes[label] = ("ok", future.result(timeout=5))
                except QuotaExceededError:
                    outcomes[label] = ("rejected", None)

        accepted = [
            label for label, (status, _) in outcomes.items() if status == "ok"
        ]
        rejected = [
            label
            for label, (status, _) in outcomes.items()
            if status == "rejected"
        ]
        self.assertEqual(len(accepted), 2)
        self.assertEqual(len(rejected), 1)
        budget = registry.get(SCOPE)
        self.assertEqual(budget.consumed, 4.0)
        self.assertEqual(budget.reserved, 0.0)
        self.assertEqual(budget.available, 0.0)
        self.assertEqual(publisher.stats()["cached_entries"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
