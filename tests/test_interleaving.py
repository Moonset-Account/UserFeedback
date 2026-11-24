from __future__ import annotations

import unittest

from error_budget import ErrorBudgetLedger, settle_events


class InterleavingTests(unittest.TestCase):
    def test_weight_based_corrections_and_revocations_interleaved(self) -> None:
        ledger = ErrorBudgetLedger()

        ledger.register_request("r1", "svc-a", "2026-10", 10, weights={"A": 1, "B": 1})
        ledger.assert_reconciled()

        # A one-unit billing increase is reallocated by the active weights.
        ledger.correct_billing("r1", amount=11)
        self.assertEqual(ledger.get_request("r1")["allocation"], {"A": 6, "B": 5})
        ledger.assert_reconciled()

        # Partial redistribution removes A and makes B absorb the full amount.
        ledger.revoke_attribution("r1", "A", redistribute=True)
        self.assertEqual(ledger.get_request("r1")["allocation"], {"A": 0, "B": 11})
        ledger.assert_reconciled()

        # A later decrease recomputes the sole remaining incident allocation.
        ledger.correct_billing("r1", amount=8)
        self.assertEqual(ledger.get_request("r1")["allocation"], {"A": 0, "B": 8})
        ledger.assert_reconciled()

        # Removing attribution sends the corrected amount to pending.
        ledger.attribute("r1", None)
        snapshot = ledger.get_request("r1")
        self.assertEqual(snapshot["pending"], 8)
        self.assertEqual(snapshot["allocation"], {"A": 0, "B": 0})
        ledger.assert_reconciled()

    def test_manual_revocation_pending_is_preserved_through_billing_increase(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request(
            "r1", "svc-a", "2026-10", 7, weights={"A": 1, "B": 1, "C": 1}
        )
        self.assertEqual(ledger.get_request("r1")["allocation"], {"A": 3, "B": 2, "C": 2})

        ledger.revoke_attribution("r1", units={"A": 1})
        snapshot = ledger.get_request("r1")
        self.assertEqual(snapshot["allocation"], {"A": 2, "B": 2, "C": 2})
        self.assertEqual(snapshot["pending"], 1)

        # Manual revocation clears the complete-map weight policy; the billing
        # increase is preserved as additional pending amount.
        ledger.correct_billing("r1", amount=10)
        snapshot = ledger.get_request("r1")
        self.assertEqual(snapshot["allocation"], {"A": 2, "B": 2, "C": 2})
        self.assertEqual(snapshot["pending"], 4)
        ledger.assert_reconciled()

        ledger.attribute_pending("r1", {"X": 1, "Y": 1})
        snapshot = ledger.get_request("r1")
        self.assertEqual(snapshot["allocation"], {"A": 2, "B": 2, "C": 2, "X": 2, "Y": 2})
        self.assertEqual(snapshot["pending"], 0)
        ledger.assert_reconciled()

        # A decrease with no pending balance uses deterministic pro-rata clawback.
        ledger.correct_billing("r1", amount=8)
        snapshot = ledger.get_request("r1")
        self.assertEqual(
            snapshot["allocation"],
            {"A": 1, "B": 1, "C": 2, "X": 2, "Y": 2},
        )
        self.assertEqual(snapshot["pending"], 0)
        ledger.assert_reconciled()

        # A complete new weight version supersedes the manual split.
        ledger.attribute("r1", {"C": 1})
        snapshot = ledger.get_request("r1")
        self.assertEqual(snapshot["allocation"], {"A": 0, "B": 0, "C": 8, "X": 0, "Y": 0})
        self.assertEqual(snapshot["pending"], 0)
        ledger.assert_reconciled()

    def test_correction_absorbs_existing_pending_before_clawing_back_incidents(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("r1", "svc", "p", 10, weights={"A": 1, "B": 1})
        ledger.revoke_attribution("r1", units={"A": 3})
        self.assertEqual(ledger.get_request("r1")["pending"], 3)

        # Reduce amount by 2: pending absorbs it; incident balances stay fixed.
        ledger.correct_billing("r1", amount=8)
        snapshot = ledger.get_request("r1")
        self.assertEqual(snapshot["allocation"], {"A": 2, "B": 5})
        self.assertEqual(snapshot["pending"], 1)
        ledger.assert_reconciled()

    def test_multiple_requests_and_events_remain_globally_balanced(self) -> None:
        events = [
            {"type": "register", "request_id": "r1", "service": "s1", "period": "2026-10", "amount": 9},
            {"type": "register", "request_id": "r2", "service": "s2", "period": "2026-10", "amount": 13,
             "weights": {"A": 1, "B": 2}},
            {"type": "attribute", "request_id": "r1", "weights": {"A": 1, "B": 1, "C": 1}},
            {"type": "correct_billing", "request_id": "r1", "amount": 10},
            {"type": "revoke", "request_id": "r2", "incident_ids": ["A"]},
            {"type": "correct_billing", "request_id": "r2", "amount": 11},
            {"type": "attribute_pending", "request_id": "r2", "weights": {"C": 1}},
            {"type": "correct_billing", "request_id": "r1", "service": "s1x", "period": "2026-11"},
        ]

        ledger, snapshots = settle_events(events)
        self.assertEqual(len(snapshots), len(events))
        reconciliation = ledger.assert_reconciled()

        # r1 final amount=10; r2 corrected to 11.
        self.assertEqual(reconciliation.service_total, 21)
        self.assertEqual(reconciliation.distribution_total, 21)
        self.assertEqual(reconciliation.request_count, 2)

        # r2: initial 13 with A:B=1:2 gives A4 B9. Revoke A -> A0 B9 pending4.
        # Correction to 11 absorbs 2 from pending -> pending2. Attribute C -> C2.
        r2 = ledger.get_request("r2")
        self.assertEqual(r2["allocation"], {"A": 0, "B": 9, "C": 2})
        self.assertEqual(r2["pending"], 0)

        # r1 after equal split of 9 -> A3 B3 C3; correction to 10 with active
        # weights recomputes to A4 B3 C3; account move preserves totals.
        r1 = ledger.get_request("r1")
        self.assertEqual(r1["service"], "s1x")
        self.assertEqual(r1["period"], "2026-11")
        self.assertEqual(r1["amount"], 10)
        self.assertEqual(r1["allocation"], {"A": 4, "B": 3, "C": 3})

    def test_reconciliation_reports_unbalanced_if_manual_state_is_corrupted(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("r1", "s", "p", 3)
        # Test-only corruption proves the guard compares both layers.
        ledger._requests["r1"].pending = 4  # noqa: SLF001
        with self.assertRaises(Exception):
            ledger.assert_reconciled()


if __name__ == "__main__":
    unittest.main()
