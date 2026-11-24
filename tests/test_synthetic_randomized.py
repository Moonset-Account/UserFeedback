from __future__ import annotations

import random
import unittest
from collections import defaultdict

from error_budget import ErrorBudgetLedger


class SyntheticInterleavingPropertyTests(unittest.TestCase):
    def test_seeded_random_interleavings_always_reconcile(self) -> None:
        rng = random.Random(16698)
        ledger = ErrorBudgetLedger()
        incident_ids = ["INC-1", "INC-2", "INC-3", "INC-4"]
        services = ["checkout", "payments"]
        periods = ["2026-10", "2026-11"]

        for request_index in range(12):
            request_id = f"req-{request_index}"
            amount = rng.randint(0, 40)
            event = {
                "type": "register",
                "request_id": request_id,
                "service": rng.choice(services),
                "period": rng.choice(periods),
                "amount": amount,
            }
            if rng.random() < 0.75:
                chosen = rng.sample(incident_ids, k=rng.randint(1, 4))
                event["weights"] = {incident_id: rng.randint(0, 5) for incident_id in chosen}
                # Avoid an all-zero map by forcing one positive weight.
                positive = next(iter(event["weights"]))
                event["weights"][positive] = rng.randint(1, 5)
            ledger.handle_event(event)

        for step in range(500):
            request_id = f"req-{rng.randrange(12)}"
            snapshot = ledger.get_request(request_id)
            operation = rng.randrange(6)

            if operation == 0:
                chosen = rng.sample(incident_ids, k=rng.randint(0, 4))
                weights = {incident_id: rng.randint(0, 5) for incident_id in chosen}
                if weights and not any(weights.values()):
                    weights[rng.choice(chosen)] = 1
                event = {"type": "attribute", "request_id": request_id, "weights": weights}

            elif operation == 1 and snapshot["pending"] > 0:
                chosen = rng.sample(incident_ids, k=rng.randint(1, 3))
                event = {
                    "type": "attribute_pending",
                    "request_id": request_id,
                    "weights": {incident_id: rng.randint(1, 5) for incident_id in chosen},
                }

            elif operation == 2:
                positive = [
                    incident_id
                    for incident_id, balance in snapshot["allocation"].items()
                    if balance > 0
                ]
                if not positive:
                    continue
                incident_id = rng.choice(positive)
                revoke_units = rng.randint(1, snapshot["allocation"][incident_id])
                event = {
                    "type": "revoke",
                    "request_id": request_id,
                    "units": {incident_id: revoke_units},
                }

            elif operation == 3:
                event = {
                    "type": "correct_billing",
                    "request_id": request_id,
                    "amount": rng.randint(0, 60),
                }
                if rng.random() < 0.2:
                    event["service"] = rng.choice(services)
                    event["period"] = rng.choice(periods)

            elif operation == 4:
                positive = [
                    incident_id
                    for incident_id, balance in snapshot["allocation"].items()
                    if balance > 0
                ]
                if not positive:
                    continue
                event = {
                    "type": "revoke",
                    "request_id": request_id,
                    "incident_ids": [rng.choice(positive)],
                }

            else:
                chosen = rng.sample(incident_ids, k=rng.randint(1, 4))
                event = {
                    "type": "attribute",
                    "request_id": request_id,
                    "weights": {incident_id: rng.randint(1, 5) for incident_id in chosen},
                }

            snapshot = ledger.handle_event(event)
            allocation_total = sum(snapshot["allocation"].values())
            self.assertEqual(allocation_total + snapshot["pending"], snapshot["amount"])
            ledger.assert_reconciled()

        reconciliation = ledger.assert_reconciled()
        self.assertGreater(reconciliation.request_count, 0)
        self.assertEqual(reconciliation.difference, 0)

        # Replay-independent balance aggregation must match request snapshots.
        expected_incidents: defaultdict[str, int] = defaultdict(int)
        expected_pending: defaultdict[tuple[str, str], int] = defaultdict(int)
        service_total = 0
        for snapshot in ledger.list_requests():
            service_total += snapshot["amount"]
            for incident_id, balance in snapshot["allocation"].items():
                expected_incidents[incident_id] += balance
            expected_pending[(snapshot["service"], snapshot["period"])] += snapshot["pending"]

        self.assertEqual(service_total, reconciliation.service_total)
        self.assertEqual(dict(expected_incidents), ledger.incident_balances())
        self.assertEqual(dict(expected_pending), ledger.pending_balances())
        self.assertEqual(
            sum(expected_incidents.values()) + sum(expected_pending.values()),
            service_total,
        )


if __name__ == "__main__":
    unittest.main()
