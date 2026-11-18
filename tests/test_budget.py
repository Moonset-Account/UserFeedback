"""Unit tests for the privacy budget ledger state machine."""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from result_publisher import (  # noqa: E402
    BudgetRegistry,
    BudgetScope,
    QuotaExceededError,
    UnknownScopeError,
)

SCOPE = BudgetScope("hr", "staffing_audit")


class PrivacyBudgetTest(unittest.TestCase):
    def test_reserve_commit_consumes_quota(self):
        budget = BudgetRegistry({SCOPE: 10.0}).get(SCOPE)
        reservation = budget.reserve(4.0)
        self.assertEqual(budget.available, 6.0)
        self.assertTrue(reservation.commit())
        self.assertEqual(budget.consumed, 4.0)
        self.assertEqual(budget.reserved, 0.0)
        self.assertEqual(budget.available, 6.0)
        # A committed reservation cannot be released again.
        self.assertFalse(reservation.release())

    def test_release_returns_quota_and_is_idempotent(self):
        budget = BudgetRegistry({SCOPE: 5.0}).get(SCOPE)
        reservation = budget.reserve(5.0)
        self.assertEqual(budget.available, 0.0)
        self.assertTrue(reservation.release())
        self.assertEqual(budget.available, 5.0)
        self.assertFalse(reservation.release())
        self.assertEqual(budget.available, 5.0)

    def test_exact_limit_is_accepted_but_over_limit_is_rejected(self):
        budget = BudgetRegistry({SCOPE: 3.0}).get(SCOPE)
        reservation = budget.reserve(3.0)  # boundary: exactly the limit
        self.assertEqual(budget.available, 0.0)
        with self.assertRaises(QuotaExceededError):
            budget.reserve(0.0001)
        reservation.release()
        with self.assertRaises(QuotaExceededError):
            budget.reserve(3.5)

    def test_negative_amounts_are_rejected(self):
        budget = BudgetRegistry({SCOPE: 1.0}).get(SCOPE)
        with self.assertRaises(ValueError):
            budget.reserve(-1.0)

    def test_registry_scopes_are_isolated(self):
        other = BudgetScope("hr", "incident_review")
        registry = BudgetRegistry({SCOPE: 2.0, other: 100.0})
        registry.get(SCOPE).reserve(2.0).commit()
        self.assertEqual(registry.get(SCOPE).available, 0.0)
        self.assertEqual(registry.get(other).available, 100.0)
        with self.assertRaises(UnknownScopeError):
            registry.get(BudgetScope("finance", "staffing_audit"))

    def test_concurrent_reservations_never_oversell(self):
        budget = BudgetRegistry({SCOPE: 10.0}).get(SCOPE)
        outcomes = []

        def worker() -> None:
            try:
                reservation = budget.reserve(3.0)
            except QuotaExceededError:
                outcomes.append("rejected")
                return
            outcomes.append("accepted")
            reservation.commit()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(outcomes.count("accepted"), 3)
        self.assertEqual(outcomes.count("rejected"), 3)
        self.assertEqual(budget.consumed, 9.0)
        self.assertAlmostEqual(budget.available, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
