from __future__ import annotations

import unittest

from identity_logout import LogoutDecider, decide_logout_completion
from tests.factories import (
    ROOT,
    SLOW,
    SOURCE,
    TARGET,
    barrier_positions,
    main_events,
    passed_watermarks,
    unrelated_backlog_events,
)


class WatermarkAndBacklogTests(unittest.TestCase):
    def test_missing_target_watermark_names_target_endpoint(self) -> None:
        watermarks = {SOURCE: 4, TARGET: 8}  # target needs 9

        decision = decide_logout_completion(
            ROOT,
            events=main_events(),
            watermarks=watermarks,
            barrier=barrier_positions(),
        )

        self.assertFalse(decision.complete)
        self.assertIn("watermark_behind_barrier", decision.reason_codes)
        self.assertEqual(decision.missing_endpoints, (TARGET,))
        missing = next(
            item
            for item in decision.missing
            if item.kind == "watermark_behind_barrier"
        )
        self.assertEqual(missing.endpoint, TARGET)
        self.assertEqual(missing.required, 9)
        self.assertEqual(missing.actual, 8)

    def test_absent_source_watermark_reports_source(self) -> None:
        decision = decide_logout_completion(
            ROOT,
            events=main_events(),
            watermarks={TARGET: 10},
            barrier=barrier_positions(),
        )

        self.assertFalse(decision.complete)
        self.assertEqual(decision.missing_endpoints, (SOURCE,))
        missing = next(item for item in decision.missing if item.endpoint == SOURCE)
        self.assertIsNone(missing.actual)
        self.assertEqual(missing.required, 4)

    def test_watermark_updates_are_monotonic(self) -> None:
        decider = LogoutDecider()
        decider.update_watermarks({TARGET: 9})
        decider.update_watermarks({TARGET: 4, SOURCE: 2})
        self.assertEqual(decider.watermarks[TARGET], 9)
        self.assertEqual(decider.watermarks[SOURCE], 2)

    def test_unrelated_backlog_does_not_block_receipt(self) -> None:
        unrelated_on_relevant_target = [
            {
                "event_id": "unrelated-target-derive",
                "type": "derivation",
                "issuer": TARGET,
                "session_id": "unrelated-target-child",
                "parent_session_id": "unrelated-target-root",
            }
            # Deliberately remains open.
        ]
        events = [
            *main_events(),
            *unrelated_backlog_events(),
            *unrelated_on_relevant_target,
        ]

        decision = decide_logout_completion(
            ROOT,
            events=events,
            watermarks=passed_watermarks(),
            barrier=barrier_positions(),
        )

        self.assertTrue(decision.complete, decision.to_dict())
        self.assertEqual(decision.descendant_count, 1)
        self.assertNotIn(SLOW, decision.relevant_endpoints)
        self.assertEqual(decision.missing_endpoints, ())

    def test_unrelated_low_watermark_not_checked(self) -> None:
        # SLOW has no watermark at all and a huge open backlog, but it is not
        # in this revocation's barrier coordinates or causal subtree.
        decider = LogoutDecider(
            events=[*main_events(), *unrelated_backlog_events()],
            barrier=barrier_positions(),
        )
        decider.update_watermarks(passed_watermarks())
        decider.update_watermarks({SLOW: 0})

        decision = decider.evaluate(ROOT)
        self.assertTrue(decision.complete, decision.to_dict())
        self.assertNotIn(SLOW, decision.relevant_endpoints)

    def test_unrelated_causal_gap_outside_barrier_endpoints_does_not_block(self) -> None:
        events = [
            *main_events(),
            {
                "event_id": "unrelated-gap-event",
                "type": "close",
                "issuer": SLOW,
                "session_id": "unrelated-session-with-gap",
                "predecessor_id": "missing-unrelated-event",
            },
        ]

        decision = decide_logout_completion(
            ROOT,
            events=events,
            watermarks=passed_watermarks(),
            barrier=barrier_positions(),
        )
        self.assertTrue(decision.complete, decision.to_dict())
        self.assertNotIn(SLOW, decision.relevant_endpoints)


if __name__ == "__main__":
    unittest.main()
