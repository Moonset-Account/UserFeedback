from __future__ import annotations

import itertools
import unittest

from identity_logout import LogoutDecider
from tests.factories import (
    BROWSER,
    ROOT,
    SOURCE,
    TARGET,
    barrier_positions,
    copied_events,
    passed_watermarks,
)


class OutOfOrderAndInterleavingTests(unittest.TestCase):
    def test_target_close_first_source_derivation_later_names_debtor(self) -> None:
        decider = LogoutDecider(barrier={SOURCE: 2, TARGET: 3})

        target_close_first = {
            "event_id": "close-from-target",
            "type": "close",
            "issuer": TARGET,
            "sequence": 3,
            "session_id": BROWSER,
            "predecessor_id": "derive-from-source",
            "predecessor_endpoint": SOURCE,
        }
        decider.ingest(target_close_first)

        # The close is safely buffered even though its derivation has not
        # arrived and therefore it is not yet connected to ROOT.
        self.assertEqual(decider.orphan_session_ids(ROOT), (BROWSER,))
        early = decider.evaluate(ROOT)
        self.assertFalse(early.complete)
        self.assertIn(SOURCE, early.missing_endpoints)
        self.assertIn(TARGET, early.missing_endpoints)

        source_derivation_late = {
            "event_id": "derive-from-source",
            "type": "derivation",
            "issuer": SOURCE,
            "sequence": 1,
            "session_id": BROWSER,
            "parent_session_id": ROOT,
        }
        decider.ingest(source_derivation_late)
        decider.update_watermarks({SOURCE: 2, TARGET: 2})

        after_derivation = decider.evaluate(ROOT)
        self.assertEqual(after_derivation.descendant_count, 1)
        self.assertEqual(after_derivation.closed_descendant_ids, (BROWSER,))
        self.assertEqual(after_derivation.open_descendant_ids, ())
        self.assertFalse(after_derivation.complete)

        # Source has now supplied its event and reached its barrier.  Target is
        # still one sequence behind, so the report names exactly TARGET.
        self.assertEqual(after_derivation.missing_endpoints, (TARGET,))
        watermark_missing = next(
            item
            for item in after_derivation.missing
            if item.kind == "watermark_behind_barrier"
        )
        self.assertEqual(watermark_missing.endpoint, TARGET)
        self.assertEqual(watermark_missing.actual, 2)
        self.assertEqual(watermark_missing.required, 3)

        decider.update_watermarks({TARGET: 3})
        final = decider.evaluate(ROOT)
        self.assertTrue(final.complete, final.to_dict())
        self.assertEqual(final.missing_endpoints, ())
        self.assertIsNotNone(final.receipt)

    def test_high_watermarks_do_not_excuse_unlinked_missing_derivation(self) -> None:
        decider = LogoutDecider(
            barrier={SOURCE: 2, TARGET: 3},
            watermarks={SOURCE: 2, TARGET: 3},
        )
        decider.ingest(
            {
                "event_id": "close-from-target",
                "type": "close",
                "issuer": TARGET,
                "session_id": BROWSER,
                "predecessor_id": "derive-from-source",
                "predecessor_endpoint": SOURCE,
            }
        )

        early = decider.evaluate(ROOT)
        self.assertFalse(early.complete)
        self.assertIn("unlinked_causal_predecessor", early.reason_codes)
        self.assertEqual(early.missing_endpoints, (SOURCE,))
        gap = next(
            item
            for item in early.missing
            if item.kind == "unlinked_causal_predecessor"
        )
        self.assertEqual(gap.event_id, "derive-from-source")
        self.assertEqual(gap.endpoint, SOURCE)

        decider.ingest(
            {
                "event_id": "derive-from-source",
                "type": "derivation",
                "issuer": SOURCE,
                "session_id": BROWSER,
                "parent_session_id": ROOT,
            }
        )
        final = decider.evaluate(ROOT)
        self.assertTrue(final.complete, final.to_dict())

    def test_every_event_permutation_has_same_final_result(self) -> None:
        events = copied_events()
        self.assertEqual(len(events), 4)

        for order in itertools.permutations(range(len(events))):
            with self.subTest(order=order):
                decider = LogoutDecider(barrier=barrier_positions())
                for index in order:
                    decider.ingest(events[index])
                decider.update_watermarks(passed_watermarks())

                decision = decider.evaluate(ROOT)
                self.assertTrue(decision.complete, decision.to_dict())
                self.assertEqual(decision.descendant_count, 1)
                self.assertEqual(decision.closed_descendant_ids, (BROWSER,))
                self.assertEqual(decision.open_descendant_ids, ())
                self.assertEqual(decision.in_flight_transfer_ids, ())
                self.assertIsNotNone(decision.receipt)

    def test_missing_causal_predecessor_names_source_endpoint(self) -> None:
        events = [
            {
                "event_id": "derive-1",
                "type": "derivation",
                "issuer": SOURCE,
                "session_id": BROWSER,
                "parent_session_id": ROOT,
            },
            {
                "event_id": "transfer-send-1",
                "type": "transfer",
                "issuer": SOURCE,
                "session_id": BROWSER,
                "transfer_id": "transfer-1",
                "from_endpoint": SOURCE,
                "to_endpoint": TARGET,
                # Deliberately absent causal event.
                "predecessor_id": "missing-source-event",
            },
            {
                "event_id": "transfer-ack-1",
                "type": "receipt",
                "issuer": TARGET,
                "session_id": BROWSER,
                "transfer_id": "transfer-1",
            },
            {
                "event_id": "close-1",
                "type": "close",
                "issuer": TARGET,
                "session_id": BROWSER,
            },
        ]

        decider = LogoutDecider(events=events, barrier=0)
        decision = decider.evaluate(ROOT)

        self.assertFalse(decision.complete)
        self.assertIn("missing_causal_predecessor", decision.reason_codes)
        gap = next(
            item
            for item in decision.missing
            if item.kind == "missing_causal_predecessor"
        )
        self.assertEqual(gap.event_id, "missing-source-event")
        self.assertEqual(gap.endpoint, SOURCE)
        self.assertEqual(decision.missing_endpoints, (SOURCE,))

        decider.ingest(
            {
                "event_id": "missing-source-event",
                "type": "derivation",
                "issuer": SOURCE,
                "session_id": "earlier-session",
                "parent_session_id": "earlier-root",
            }
        )
        final = decider.evaluate(ROOT)
        self.assertTrue(final.complete, final.to_dict())

    def test_integer_predecessor_ids_are_normalized(self) -> None:
        decider = LogoutDecider()
        decider.ingest(
            {
                "event_id": 1,
                "type": "derivation",
                "issuer": SOURCE,
                "session_id": BROWSER,
                "parent_session_id": ROOT,
                "predecessor": 0,
            }
        )
        decider.ingest(
            {
                "event_id": 2,
                "type": "close",
                "issuer": TARGET,
                "session_id": BROWSER,
                "predecessor": 1,
            }
        )

        decision = decider.evaluate(ROOT, barrier=0)
        self.assertTrue(decision.complete, decision.to_dict())


if __name__ == "__main__":
    unittest.main()
