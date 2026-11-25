from __future__ import annotations

import json
import unittest

from identity_logout import EventConflictError, LogoutDecider, decide_logout_completion


class RobustnessTests(unittest.TestCase):
    def test_relevant_endpoint_without_barrier_coordinate_is_reported(self) -> None:
        events = [
            {
                "event_id": "derive",
                "type": "derivation",
                "issuer": "dc-new",
                "session_id": "child",
                "parent_session_id": "root",
            },
            {
                "event_id": "close",
                "type": "close",
                "issuer": "dc-new",
                "session_id": "child",
            },
        ]

        decision = decide_logout_completion(
            "root",
            events=events,
            watermarks={"dc-new": 99, "dc-somewhere-else": 3},
            barrier={"dc-somewhere-else": 3},
        )

        self.assertFalse(decision.complete)
        self.assertIn("barrier_position_missing", decision.reason_codes)
        self.assertEqual(decision.missing_endpoints, ("dc-new",))

    def test_integer_barrier_applies_to_every_relevant_endpoint(self) -> None:
        events = [
            {
                "event_id": "derive",
                "type": "derivation",
                "issuer": "dc-a",
                "session_id": "child",
                "parent_session_id": "root",
            },
            {
                "event_id": "close",
                "type": "close",
                "issuer": "dc-b",
                "session_id": "child",
            },
        ]

        not_ready = decide_logout_completion(
            "root", events=events, watermarks={"dc-a": 5}, barrier=5
        )
        self.assertFalse(not_ready.complete)
        self.assertEqual(not_ready.missing_endpoints, ("dc-b",))

        ready = decide_logout_completion(
            "root",
            events=events,
            watermarks={"dc-a": 5, "dc-b": 6},
            barrier=5,
        )
        self.assertTrue(ready.complete, ready.to_dict())

    def test_causal_cycle_is_reported_and_does_not_loop_forever(self) -> None:
        decider = LogoutDecider(
            events=[
                {
                    "event_id": "edge-a",
                    "type": "derivation",
                    "session_id": "a",
                    "parent_session_id": "b",
                },
                {
                    "event_id": "edge-b",
                    "type": "derivation",
                    "session_id": "b",
                    "parent_session_id": "a",
                },
            ],
            barrier=0,
        )

        decision = decider.evaluate("a")
        self.assertFalse(decision.complete)
        self.assertIn("causal_cycle", decision.reason_codes)

    def test_conflicting_parent_evidence_is_rejected(self) -> None:
        decider = LogoutDecider()
        decider.ingest(
            {
                "event_id": "derive-1",
                "type": "derivation",
                "session_id": "child",
                "parent_session_id": "parent-1",
            }
        )
        with self.assertRaises(EventConflictError):
            decider.ingest(
                {
                    "event_id": "derive-2",
                    "type": "derivation",
                    "session_id": "child",
                    "parent_session_id": "parent-2",
                }
            )

    def test_conflicting_reuse_of_event_id_is_rejected(self) -> None:
        decider = LogoutDecider()
        decider.ingest({"event_id": "e1", "type": "close", "session_id": "s1"})
        with self.assertRaises(EventConflictError):
            decider.ingest({"event_id": "e1", "type": "derivation", "session_id": "s2"})

    def test_repeated_close_is_idempotent_for_completion(self) -> None:
        events = [
            {
                "event_id": "derive",
                "type": "derivation",
                "session_id": "child",
                "parent_session_id": "root",
            },
            {"event_id": "close-1", "type": "close", "session_id": "child"},
            {"event_id": "close-2", "type": "close", "session_id": "child"},
        ]

        decision = decide_logout_completion("root", events=events, barrier=0)
        self.assertTrue(decision.complete, decision.to_dict())
        self.assertEqual(decision.closed_descendant_ids, ("child",))

    def test_decision_and_receipt_are_json_serializable(self) -> None:
        decision = decide_logout_completion(
            "root",
            events=[
                {
                    "event_id": "derive",
                    "type": "derivation",
                    "issuer": "dc-a",
                    "session_id": "child",
                    "parent_session_id": "root",
                },
                {
                    "event_id": "close",
                    "type": "close",
                    "issuer": "dc-a",
                    "session_id": "child",
                },
            ],
            watermarks={"dc-a": 3},
            barrier={"dc-a": 3},
        )

        payload = decision.to_dict()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertIn('"complete": true', encoded)
        self.assertTrue(decision.complete)


if __name__ == "__main__":
    unittest.main()
