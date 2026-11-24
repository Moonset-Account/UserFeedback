from __future__ import annotations

import unittest

from error_budget import ErrorBudgetLedger, RevocationExceedsBalanceError


class AttributionAndRevocationTests(unittest.TestCase):
    def test_initial_weight_allocation_and_tie_break(self) -> None:
        ledger = ErrorBudgetLedger()
        snapshot = ledger.register_request(
            "req-1", "svc", "2026-10", 4, weights={"C": 1, "A": 1, "B": 1}
        )

        self.assertEqual(snapshot["allocation"], {"A": 2, "B": 1, "C": 1})
        self.assertEqual(snapshot["pending"], 0)
        self.assertEqual(ledger.incident_balance("A"), 2)
        self.assertEqual(ledger.incident_balance("B"), 1)
        self.assertEqual(ledger.incident_balance("C"), 1)
        ledger.assert_reconciled()

    def test_missing_attribution_goes_to_pending_and_can_be_attributed_later(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "svc", "2026-10", 10)
        self.assertEqual(ledger.pending_balance("svc", "2026-10"), 10)

        ledger.attribute_pending("req-1", {"A": 1, "B": 1})
        snapshot = ledger.get_request("req-1")
        self.assertEqual(snapshot["allocation"], {"A": 5, "B": 5})
        self.assertEqual(snapshot["pending"], 0)
        self.assertEqual(ledger.pending_balance("svc", "2026-10"), 0)

        pending_entries = [e for e in ledger.entries if e.layer == "pending"]
        self.assertEqual([e.delta for e in pending_entries], [10, -10])
        ledger.assert_reconciled()

    def test_complete_revision_posts_only_allocation_differences(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "svc", "p", 100, weights={"A": 1, "B": 1})
        ledger.attribute("req-1", {"A": 2, "B": 1}, version="v2")

        snapshot = ledger.get_request("req-1")
        self.assertEqual(snapshot["allocation"], {"A": 67, "B": 33})

        revision_entries = [
            e
            for e in ledger.entries
            if e.layer == "incident" and e.reason == "attribution_revision"
        ]
        self.assertEqual({e.incident_id: e.delta for e in revision_entries}, {"A": 17, "B": -17})
        ledger.assert_reconciled()

    def test_direct_partial_revocation_moves_only_revoked_units_to_pending(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "svc", "p", 10, weights={"A": 1, "B": 1})

        ledger.revoke_attribution("req-1", units={"A": 2}, version="rev-1")
        snapshot = ledger.get_request("req-1")

        self.assertEqual(snapshot["allocation"], {"A": 3, "B": 5})
        self.assertEqual(snapshot["pending"], 2)
        self.assertEqual(ledger.incident_balance("A"), 3)
        self.assertEqual(ledger.incident_balance("B"), 5)
        self.assertEqual(ledger.pending_balance("svc", "p"), 2)

        revocation_incident_entries = [
            e for e in ledger.entries if e.reason == "partial_revocation" and e.layer == "incident"
        ]
        self.assertEqual(len(revocation_incident_entries), 1)
        self.assertEqual(revocation_incident_entries[0].incident_id, "A")
        self.assertEqual(revocation_incident_entries[0].delta, -2)

        revocation_pending_entries = [
            e for e in ledger.entries if e.reason == "partial_revocation" and e.layer == "pending"
        ]
        self.assertEqual([e.delta for e in revocation_pending_entries], [2])
        ledger.assert_reconciled()

    def test_revoke_entire_incident_balance_leaves_other_incident_unchanged(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "svc", "p", 10, weights={"A": 1, "B": 1})

        ledger.revoke_attribution("req-1", "A")
        snapshot = ledger.get_request("req-1")

        self.assertEqual(snapshot["allocation"], {"A": 0, "B": 5})
        self.assertEqual(snapshot["pending"], 5)
        self.assertEqual(ledger.incident_balance("B"), 5)
        ledger.assert_reconciled()

    def test_partial_revocation_can_redistribute_among_remaining_weights(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "svc", "p", 10, weights={"A": 1, "B": 1})

        ledger.revoke_attribution("req-1", "A", redistribute=True)
        snapshot = ledger.get_request("req-1")

        self.assertEqual(snapshot["allocation"], {"A": 0, "B": 10})
        self.assertEqual(snapshot["pending"], 0)
        deltas = {
            e.incident_id: e.delta
            for e in ledger.entries
            if e.reason == "partial_revocation_redistribute"
        }
        self.assertEqual(deltas, {"A": -5, "B": 5})
        ledger.assert_reconciled()

    def test_revoke_all_moves_full_amount_to_pending(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "svc", "p", 10, weights={"A": 3, "B": 7})

        ledger.revoke_attribution("req-1", all_incidents=True)
        snapshot = ledger.get_request("req-1")

        self.assertEqual(snapshot["allocation"], {"A": 0, "B": 0})
        self.assertEqual(snapshot["pending"], 10)
        self.assertEqual(ledger.incident_balance("A"), 0)
        self.assertEqual(ledger.incident_balance("B"), 0)
        ledger.assert_reconciled()

    def test_revoking_more_than_balance_is_atomic_rejection(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "svc", "p", 10, weights={"A": 1, "B": 1})
        entries_before = len(ledger.entries)

        with self.assertRaises(RevocationExceedsBalanceError):
            ledger.revoke_attribution("req-1", units={"A": 6})

        self.assertEqual(len(ledger.entries), entries_before)
        self.assertEqual(ledger.get_request("req-1")["allocation"], {"A": 5, "B": 5})
        ledger.assert_reconciled()

    def test_clearing_complete_attribution_moves_everything_to_pending(self) -> None:
        ledger = ErrorBudgetLedger()
        ledger.register_request("req-1", "svc", "p", 10, weights={"A": 1, "B": 1})

        ledger.attribute("req-1", None)
        snapshot = ledger.get_request("req-1")
        self.assertEqual(snapshot["allocation"], {"A": 0, "B": 0})
        self.assertEqual(snapshot["pending"], 10)
        self.assertIsNone(snapshot["weights"])
        ledger.assert_reconciled()


if __name__ == "__main__":
    unittest.main()
