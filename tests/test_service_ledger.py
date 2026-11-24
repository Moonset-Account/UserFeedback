from __future__ import annotations

import unittest

from error_budget import (
    DuplicateRequestError,
    ErrorBudgetLedger,
    InvalidWeightsError,
    RequestNotFoundError,
)


class ServiceLayerTests(unittest.TestCase):
    def test_service_is_charged_once_and_missing_attribution_is_pending(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "checkout", "2026-10", 100)

        self.assertEqual(ledger.service_balance("checkout", "2026-10"), 100)
        self.assertEqual(ledger.pending_balance("checkout", "2026-10"), 100)
        self.assertEqual(ledger.incident_balances(), {})

        service_entries = [e for e in ledger.entries if e.layer == "service"]
        self.assertEqual(len(service_entries), 1)
        self.assertEqual(service_entries[0].delta, 100)
        self.assertEqual(service_entries[0].reason, "confirmed_charge")

        reconciliation = ledger.assert_reconciled()
        self.assertEqual(reconciliation.service_total, 100)
        self.assertEqual(reconciliation.distribution_total, 100)
        self.assertTrue(reconciliation.balanced)

    def test_duplicate_registration_does_not_create_second_charge(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "checkout", "2026-10", 100)

        with self.assertRaises(DuplicateRequestError):
            ledger.register_request("req-1", "checkout", "2026-10", 100)

        service_entries = [e for e in ledger.entries if e.layer == "service"]
        self.assertEqual(len(service_entries), 1)
        self.assertEqual(ledger.service_balance("checkout", "2026-10"), 100)

    def test_attribution_revisions_do_not_post_to_service_layer(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request(
            "req-1", "checkout", "2026-10", 10, weights={"A": 1, "B": 1}
        )
        ledger.attribute("req-1", {"A": 3, "B": 1})
        ledger.revoke_attribution("req-1", "A", all_incidents=False, redistribute=True)

        service_entries = [e for e in ledger.entries if e.layer == "service"]
        self.assertEqual(len(service_entries), 1)
        self.assertEqual(sum(e.delta for e in service_entries), 10)
        self.assertEqual(ledger.service_balance("checkout", "2026-10"), 10)
        ledger.assert_reconciled()

    def test_unknown_request_is_rejected(self) -> None:
        ledger = ErrorBudgetLedger()
        with self.assertRaises(RequestNotFoundError):
            ledger.attribute("missing", {"A": 1})

    def test_only_explicit_billing_correction_changes_service_amount(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request(
            "req-1", "checkout", "2026-10", 100, weights={"A": 1, "B": 1}
        )

        # Attribution activity leaves service charge untouched.
        ledger.attribute("req-1", {"A": 9, "B": 1})
        self.assertEqual(ledger.service_balance("checkout", "2026-10"), 100)

        ledger.correct_billing("req-1", amount=120)
        self.assertEqual(ledger.service_balance("checkout", "2026-10"), 120)
        correction_entries = [
            e for e in ledger.entries if e.layer == "service" and "correction" in e.reason
        ]
        self.assertEqual([e.delta for e in correction_entries], [20])
        ledger.assert_reconciled()

    def test_explicit_reallocation_without_weights_is_atomic_rejection(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "checkout", "2026-10", 10)
        entries_before = len(ledger.entries)

        with self.assertRaises(InvalidWeightsError):
            ledger.correct_billing("req-1", amount=12, reallocate=True)

        self.assertEqual(len(ledger.entries), entries_before)
        self.assertEqual(ledger.service_balance("checkout", "2026-10"), 10)
        self.assertEqual(ledger.pending_balance("checkout", "2026-10"), 10)
        ledger.assert_reconciled()

    def test_billing_correction_can_move_service_and_period_bucket(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request(
            "req-1", "checkout", "2026-10", 100, weights={"A": 1, "B": 1}
        )
        ledger.correct_billing(
            "req-1", service="payments", period="2026-11", amount=120
        )

        # Old bucket has initial charge and gross reversal; net zero.
        self.assertEqual(ledger.service_balance("checkout", "2026-10"), 0)
        self.assertEqual(ledger.service_balance("payments", "2026-11"), 120)

        reasons_and_deltas = [
            (e.reason, e.delta)
            for e in ledger.entries
            if e.layer == "service" and e.request_id == "req-1"
        ]
        self.assertEqual(
            reasons_and_deltas,
            [
                ("confirmed_charge", 100),
                ("billing_correction_account_reversal", -100),
                ("billing_correction_account_repost", 120),
            ],
        )
        ledger.assert_reconciled()

    def test_original_period_is_retained_for_plain_amount_correction(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "checkout", "2026-10", 7)
        ledger.correct_billing("req-1", amount=9)

        self.assertEqual(ledger.service_balance("checkout", "2026-10"), 9)
        self.assertEqual(ledger.pending_balance("checkout", "2026-10"), 9)
        self.assertEqual(ledger.service_balances().get(("checkout", "2026-11"), 0), 0)
        ledger.assert_reconciled()


if __name__ == "__main__":
    unittest.main()
