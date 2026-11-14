"""Core adjudicator behaviour: two layers, dedup, conflicts, tenant isolation."""

import threading
import unittest

from quota.config import LayerConfig, QuotaConfig
from quota.engine import QuotaAdjudicator
from quota.errors import ClockRegression, RequestIdConflict, ValidationError


def make(account_limit=2, account_window=10.0, source_limit=3, source_window=20.0, ttl=10.0):
    return QuotaAdjudicator(
        QuotaConfig(
            account=LayerConfig(account_limit, account_window),
            source=LayerConfig(source_limit, source_window),
            dedup_ttl_seconds=ttl,
        )
    )


class TwoLayerGateTests(unittest.TestCase):
    def test_both_layers_must_have_room(self) -> None:
        q = make(account_limit=5, source_limit=2, source_window=60)
        first = q.adjudicate(tenant="t", account="a1", source="192.0.2.1", request_id="r1", now=0)
        second = q.adjudicate(tenant="t", account="a2", source="192.0.2.1", request_id="r2", now=0)
        third = q.adjudicate(tenant="t", account="a3", source="192.0.2.1", request_id="r3", now=0)

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertFalse(third.allowed)
        self.assertEqual(third.blocked_layers, ("source",))
        self.assertEqual(third.reason, "source_rate_limited")

    def test_denial_charges_neither_layer(self) -> None:
        q = make(account_limit=1, source_limit=1, account_window=60, source_window=60)
        q.adjudicate(tenant="t", account="a1", source="192.0.2.1", request_id="r1", now=0)

        # Blocked on both layers; neither may be charged for the failed attempt.
        denied = q.adjudicate(tenant="t", account="a1", source="192.0.2.1", request_id="r2", now=1)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.blocked_layers, ("account", "source"))
        self.assertEqual(denied.retry_at, 60.0)
        self.assertEqual(denied.layer("account").used, 1)
        self.assertEqual(denied.layer("source").used, 1)

        # A different account on the same exhausted source proves the account
        # layer was not charged by the denied attempt.
        other = q.adjudicate(tenant="t", account="a2", source="192.0.2.1", request_id="r3", now=2)
        self.assertFalse(other.allowed)
        self.assertEqual(other.blocked_layers, ("source",))
        self.assertEqual(other.layer("account").used, 0)

    def test_denied_account_budget_is_intact_on_other_source(self) -> None:
        q = make(account_limit=1, source_limit=1, account_window=60, source_window=60)
        q.adjudicate(tenant="t", account="a1", source="192.0.2.1", request_id="r1", now=0)
        denied = q.adjudicate(tenant="t", account="a1", source="192.0.2.2", request_id="r2", now=1)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.blocked_layers, ("account",))
        # The denied attempt must not have charged source 192.0.2.2.
        fresh = q.adjudicate(tenant="t", account="a2", source="192.0.2.2", request_id="r3", now=2)
        self.assertTrue(fresh.allowed)
        self.assertEqual(fresh.layer("source").used, 1)

    def test_retry_at_is_max_over_blocked_layers(self) -> None:
        q = make(account_limit=1, account_window=10.0, source_limit=1, source_window=40.0)
        q.adjudicate(tenant="t", account="a1", source="192.0.2.1", request_id="r1", now=5)
        denied = q.adjudicate(tenant="t", account="a1", source="192.0.2.1", request_id="r2", now=6)
        self.assertEqual(denied.blocked_layers, ("account", "source"))
        self.assertEqual(denied.layer("account").retry_at, 15.0)
        self.assertEqual(denied.layer("source").retry_at, 45.0)
        self.assertEqual(denied.retry_at, 45.0)

    def test_retry_after_both_layers_release(self) -> None:
        q = make(account_limit=1, account_window=10.0, source_limit=1, source_window=40.0)
        q.adjudicate(tenant="t", account="a1", source="192.0.2.1", request_id="r1", now=0)
        denied = q.adjudicate(tenant="t", account="a1", source="192.0.2.1", request_id="r2", now=1)
        self.assertEqual(denied.retry_at, 40.0)
        # At t=10 the account layer has room, but retry_at already accounts for
        # the source layer, so the advised instant is exact.
        at_source_release = q.adjudicate(
            tenant="t", account="a1", source="192.0.2.1", request_id="r3", now=40.0
        )
        self.assertTrue(at_source_release.allowed)


class SlidingWindowTests(unittest.TestCase):
    def test_boundary_is_half_open(self) -> None:
        q = make(account_limit=1, account_window=10.0)
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r1", now=0)
        before = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r2", now=9.999)
        at = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r3", now=10.0)
        self.assertFalse(before.allowed)
        self.assertTrue(at.allowed)

    def test_sliding_not_fixed_bucket(self) -> None:
        # Admit at t=0 and t=9 under a 10s/limit-2 window. A fixed 10s bucket
        # would reset all counts at t=10 and allow two more; a sliding window
        # keeps the t=9 admission, so only one more fits at t=10 and the next
        # attempt is denied until t=19.
        q = make(account_limit=2, account_window=10.0, source_limit=99, source_window=10.0)
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r1", now=0)
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r2", now=9)
        at_ten = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r3", now=10)
        self.assertTrue(at_ten.allowed)
        self.assertEqual(at_ten.layer("account").used, 2)
        at_half = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r4", now=10.5)
        self.assertFalse(at_half.allowed)
        self.assertEqual(at_half.blocked_layers, ("account",))
        self.assertEqual(at_half.retry_at, 19.0)
        # At t=19 the t=9 event is gone, leaving only the t=10 admission.
        at_nineteen = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r5", now=19)
        self.assertTrue(at_nineteen.allowed)
        self.assertEqual(at_nineteen.layer("account").used, 2)

    def test_clock_regression_on_new_request(self) -> None:
        q = make()
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r1", now=10)
        with self.assertRaises(ClockRegression):
            q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r2", now=9)

    def test_clock_regression_is_per_tenant(self) -> None:
        q = make()
        q.adjudicate(tenant="t1", account="a", source="192.0.2.1", request_id="r1", now=10)
        # A different tenant is allowed to start at an earlier instant.
        decision = q.adjudicate(tenant="t2", account="a", source="192.0.2.1", request_id="r1", now=1)
        self.assertTrue(decision.allowed)


class DedupTests(unittest.TestCase):
    def test_replay_returns_first_decision_without_charging(self) -> None:
        q = make(account_limit=1, account_window=60, ttl=30)
        first = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="rid", now=0)
        replay = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="rid", now=5)

        self.assertTrue(first.allowed)
        self.assertFalse(first.deduplicated)
        self.assertTrue(replay.deduplicated)
        self.assertTrue(replay.allowed)
        self.assertEqual(replay.decided_at, 0.0)
        self.assertEqual(replay.first_decided_at, 0.0)
        self.assertEqual(replay.layer("account").used, 1)

    def test_replayed_denial_stays_denied(self) -> None:
        q = make(account_limit=1, account_window=60, ttl=30)
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r1", now=0)
        denied = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r2", now=1)
        replay = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r2", now=20)
        self.assertFalse(denied.allowed)
        self.assertTrue(replay.deduplicated)
        self.assertFalse(replay.allowed)
        self.assertEqual(replay.retry_at, 60.0)

    def test_changed_parameters_conflict(self) -> None:
        q = make(ttl=30)
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="rid", now=0)
        with self.assertRaises(RequestIdConflict) as ctx:
            q.adjudicate(tenant="t", account="b", source="192.0.2.1", request_id="rid", now=1)
        payload = ctx.exception.to_dict()
        self.assertEqual(payload["error"], "request_id_conflict")
        self.assertEqual(payload["existing"]["account"], "a")
        self.assertEqual(payload["incoming"]["account"], "b")

    def test_changed_source_conflicts(self) -> None:
        q = make(ttl=30)
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="rid", now=0)
        with self.assertRaises(RequestIdConflict):
            q.adjudicate(tenant="t", account="a", source="192.0.2.2", request_id="rid", now=1)

    def test_conflict_does_not_charge(self) -> None:
        q = make(account_limit=1, account_window=60, ttl=30)
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="rid", now=0)
        with self.assertRaises(RequestIdConflict):
            q.adjudicate(tenant="t", account="b", source="192.0.2.1", request_id="rid", now=1)
        # Account b was never charged, so it can log in on a fresh source.
        allowed = q.adjudicate(tenant="t", account="b", source="192.0.2.3", request_id="other", now=2)
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.layer("account").used, 1)

    def test_dedup_is_per_tenant(self) -> None:
        q = make(ttl=30)
        q.adjudicate(tenant="t1", account="a", source="192.0.2.1", request_id="rid", now=0)
        # Same id under another tenant is a fresh decision, even with a new account.
        decision = q.adjudicate(tenant="t2", account="b", source="192.0.2.1", request_id="rid", now=0)
        self.assertFalse(decision.deduplicated)

    def test_id_reusable_after_ttl(self) -> None:
        q = make(account_limit=2, ttl=10)
        first = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="rid", now=0)
        inside = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="rid", now=9.999)
        after = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="rid", now=10.0)
        self.assertFalse(first.deduplicated)
        self.assertTrue(inside.deduplicated)
        self.assertFalse(after.deduplicated)
        # The t=0 admission has left the 10s window, so the fresh decision
        # charges the account exactly once.
        self.assertEqual(after.layer("account").used, 1)

    def test_source_aliases_do_not_conflict(self) -> None:
        q = make(ttl=30)
        q.adjudicate(tenant="t", account="a", source="2001:db8::1", request_id="rid", now=0)
        # Same address in a different but valid spelling must be the same replay,
        # not a spurious conflict.
        replay = q.adjudicate(
            tenant="t", account="a",
            source="2001:0DB8:0000:0000:0000:0000:0000:0001",
            request_id="rid", now=1,
        )
        self.assertTrue(replay.deduplicated)

    def test_zero_ttl_never_deduplicates(self) -> None:
        q = make(account_limit=3, ttl=0.0)
        first = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="rid", now=0)
        second = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="rid", now=0)
        self.assertFalse(first.deduplicated)
        self.assertFalse(second.deduplicated)
        self.assertEqual(second.layer("account").used, 2)


class TenantIsolationTests(unittest.TestCase):
    def test_account_and_source_buckets_are_per_tenant(self) -> None:
        q = make(account_limit=1, source_limit=1, account_window=60, source_window=60)
        first = q.adjudicate(tenant="t1", account="a", source="192.0.2.1", request_id="r1", now=0)
        second = q.adjudicate(tenant="t2", account="a", source="192.0.2.1", request_id="r1", now=0)
        third = q.adjudicate(tenant="t1", account="a", source="192.0.2.1", request_id="r2", now=1)
        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertFalse(third.allowed)


class ConcurrencyTests(unittest.TestCase):
    def _race(self, workers: int, account_limit: int, source_limit: int) -> list:
        q = make(account_limit=account_limit, source_limit=source_limit, account_window=60, source_window=60, ttl=60)
        barrier = threading.Barrier(workers)
        decisions = []
        errors = []
        lock = threading.Lock()

        def run(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                decision = q.adjudicate(
                    tenant="race", account="shared", source="192.0.2.9",
                    request_id=f"req-{index}", now=1.0,
                )
                with lock:
                    decisions.append(decision)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(decisions), workers)
        return decisions

    def test_race_for_last_account_unit(self) -> None:
        decisions = self._race(workers=2, account_limit=1, source_limit=10)
        self.assertEqual(sum(1 for d in decisions if d.allowed), 1)

    def test_race_for_last_source_unit(self) -> None:
        decisions = self._race(workers=2, account_limit=10, source_limit=1)
        self.assertEqual(sum(1 for d in decisions if d.allowed), 1)

    def test_many_workers_never_exceed_limit(self) -> None:
        decisions = self._race(workers=16, account_limit=3, source_limit=3)
        self.assertEqual(sum(1 for d in decisions if d.allowed), 3)

    def test_concurrent_duplicate_ids_all_share_one_admission(self) -> None:
        q = make(account_limit=5, source_limit=5, ttl=60)
        barrier = threading.Barrier(8)
        decisions = []
        lock = threading.Lock()

        def run() -> None:
            barrier.wait(timeout=5)
            decision = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="same", now=1.0)
            with lock:
                decisions.append(decision)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(decisions), 8)
        self.assertEqual(sum(1 for d in decisions if d.allowed), 8)
        self.assertEqual(sum(1 for d in decisions if not d.deduplicated), 1)
        final = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="probe", now=2.0)
        self.assertEqual(final.layer("account").used, 2)


class ValidationTests(unittest.TestCase):
    def test_non_canonical_account_rejected(self) -> None:
        q = make()
        with self.assertRaises(ValidationError):
            q.adjudicate(tenant="t", account="Alice", source="192.0.2.1", request_id="r", now=0)

    def test_bad_source_rejected(self) -> None:
        q = make()
        for bad in ("", "host.example", "192.0.2.1/32"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    q.adjudicate(tenant="t", account="a", source=bad, request_id="r", now=0)

    def test_non_finite_clock_rejected(self) -> None:
        q = make()
        for bad in (float("nan"), float("inf"), -1, "1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r", now=bad)

    def test_rejected_call_does_not_mutate_state(self) -> None:
        q = make(account_limit=1)
        with self.assertRaises(ValidationError):
            q.adjudicate(tenant="t", account="A", source="192.0.2.1", request_id="r", now=0)
        decision = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r", now=0)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.layer("account").used, 1)


class SweepTests(unittest.TestCase):
    def test_sweep_drops_expired_state(self) -> None:
        q = make(account_limit=1, account_window=10, source_limit=1, source_window=10, ttl=10)
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r1", now=0)
        self.assertEqual(q.snapshot()["dedup_entries"], 1)
        q.sweep(10.0)
        snapshot = q.snapshot()
        self.assertEqual(snapshot["dedup_entries"], 0)
        # Empty buckets are reclaimed, not retained.
        self.assertEqual(snapshot["account_windows"], {})
        self.assertEqual(snapshot["source_windows"], {})
        decision = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r1", now=10.0)
        self.assertTrue(decision.allowed)

    def test_sweep_keeps_still_active_buckets(self) -> None:
        q = make(account_limit=2, account_window=10, source_limit=2, source_window=10, ttl=10)
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r1", now=5)
        q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r2", now=6)
        q.sweep(6.0)
        snapshot = q.snapshot()
        self.assertEqual(snapshot["account_windows"], {"t\x1fa": 2})
        self.assertEqual(snapshot["source_windows"], {"t\x1f192.0.2.1": 2})
        # Sweeping must not have altered accounting: both admissions still count.
        denied = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r3", now=14.9)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.retry_at, 15.0)
        allowed = q.adjudicate(tenant="t", account="a", source="192.0.2.1", request_id="r4", now=15.0)
        self.assertTrue(allowed.allowed)

    def test_bucket_cardinality_is_reclaimed_after_sweep(self) -> None:
        q = make(account_limit=1, account_window=10, source_limit=1, source_window=10, ttl=10)
        for index in range(50):
            q.adjudicate(
                tenant="t", account=f"acct-{index}", source=f"192.0.2.{index}",
                request_id=f"r{index}", now=0,
            )
        self.assertEqual(len(q.snapshot()["account_windows"]), 50)
        q.sweep(10.0)
        snapshot = q.snapshot()
        self.assertEqual(snapshot["account_windows"], {})
        self.assertEqual(snapshot["source_windows"], {})


if __name__ == "__main__":
    unittest.main()
