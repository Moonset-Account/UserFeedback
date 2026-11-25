from __future__ import annotations

import unittest

from identity_logout import LogoutDecider
from tests.factories import (
    BROWSER,
    ROOT,
    SOURCE,
    TARGET,
    TRANSFER,
    barrier_positions,
    copied_events,
    passed_watermarks,
)


class TransferTests(unittest.TestCase):
    def _events_without_ack(self) -> list[dict]:
        events = copied_events()
        return [event for event in events if event["event_id"] != "transfer-ack-1"]

    def test_sent_transfer_without_target_receipt_is_in_flight(self) -> None:
        decider = LogoutDecider(
            events=self._events_without_ack(),
            watermarks=passed_watermarks(),
            barrier=barrier_positions(),
        )

        decision = decider.evaluate(ROOT)
        self.assertFalse(decision.complete)
        self.assertEqual(decision.in_flight_transfer_ids, (TRANSFER,))
        self.assertIn("in_flight_transfer", decision.reason_codes)

        in_flight = next(
            item for item in decision.missing if item.kind == "in_flight_transfer"
        )
        self.assertEqual(in_flight.endpoint, TARGET)
        self.assertEqual(in_flight.transfer_id, TRANSFER)
        self.assertIn(TARGET, decision.missing_endpoints)

    def test_plain_target_close_does_not_sign_for_transfer(self) -> None:
        # The factory omits the ack but still contains the target close.
        decider = LogoutDecider(
            events=self._events_without_ack(),
            watermarks=passed_watermarks(),
            barrier=barrier_positions(),
        )

        browser = decider.sessions[BROWSER]
        self.assertEqual(len(browser.close_event_ids), 1)
        decision = decider.evaluate(ROOT)
        self.assertFalse(decision.complete)
        self.assertEqual(decision.in_flight_transfer_ids, (TRANSFER,))

    def test_ack_then_duplicate_receipts_do_not_create_descendants(self) -> None:
        decider = LogoutDecider(barrier=barrier_positions())
        original_events = copied_events()

        # Deliver every event, including one exact duplicate ack event ID.
        decider.ingest_events(original_events)
        duplicate_same_id = decider.ingest(dict(original_events[2]))
        self.assertIsNone(duplicate_same_id)

        # Redeliver the same receipt under a new delivery event ID but with
        # the same natural receipt ID.
        decider.ingest(
            {
                "event_id": "transfer-ack-redelivery-same-receipt",
                "type": "ack",
                "issuer": TARGET,
                "session_id": BROWSER,
                "transfer_id": TRANSFER,
                "receipt_id": "receipt-1",
                # A redelivery must not be interpreted as a new derivation.
                "child_session_id": "must-not-become-descendant",
            }
        )

        # Another redelivery has neither the original event ID nor receipt ID,
        # but comes from the same target endpoint for the same transfer.
        decider.ingest(
            {
                "event_id": "transfer-ack-redelivery-endpoint",
                "type": "ack",
                "issuer": TARGET,
                "session_id": BROWSER,
                "transfer_id": TRANSFER,
            }
        )

        decider.update_watermarks(passed_watermarks())
        decision = decider.evaluate(ROOT)

        self.assertTrue(decision.complete, decision.to_dict())
        self.assertEqual(decision.descendant_count, 1)
        self.assertNotIn(
            "must-not-become-descendant",
            decision.closed_descendant_ids + decision.open_descendant_ids,
        )
        self.assertEqual(decision.closed_descendant_ids, (BROWSER,))
        self.assertEqual(decision.in_flight_transfer_ids, ())
        self.assertEqual(
            decider.transfers[TRANSFER].receipt_event_ids, ["transfer-ack-1"]
        )
        self.assertIn(
            "transfer-ack-redelivery-same-receipt",
            decider.duplicate_receipt_event_ids,
        )
        self.assertIn(
            "transfer-ack-redelivery-endpoint",
            decider.duplicate_receipt_event_ids,
        )

    def test_receipt_arriving_before_send_is_reconciled(self) -> None:
        ack = {
            "event_id": "ack-early",
            "type": "receipt",
            "issuer": TARGET,
            "session_id": BROWSER,
            "transfer_id": TRANSFER,
            "receipt_id": "early-receipt",
        }
        send = {
            "event_id": "send-late",
            "type": "transfer",
            "issuer": SOURCE,
            "session_id": BROWSER,
            "transfer_id": TRANSFER,
            "from_endpoint": SOURCE,
            "to_endpoint": TARGET,
        }
        derive = {
            "event_id": "derive",
            "type": "derivation",
            "issuer": SOURCE,
            "session_id": BROWSER,
            "parent_session_id": ROOT,
        }
        close = {
            "event_id": "close",
            "type": "close",
            "issuer": TARGET,
            "session_id": BROWSER,
        }

        decider = LogoutDecider()
        decider.ingest(ack)
        decider.ingest(send)
        decider.ingest(derive)
        decider.ingest(close)

        decision = decider.evaluate(ROOT, barrier=0)
        self.assertTrue(decision.complete, decision.to_dict())
        self.assertEqual(decision.in_flight_transfer_ids, ())


if __name__ == "__main__":
    unittest.main()
