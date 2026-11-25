from __future__ import annotations

import unittest

from identity_logout import decide_logout_completion


def derivation(event_id: str, child: str, parent: str, endpoint: str = "dc-a") -> dict:
    return {
        "event_id": event_id,
        "type": "derivation",
        "issuer": endpoint,
        "session_id": child,
        "parent_session_id": parent,
    }


def close(event_id: str, session: str, endpoint: str = "dc-a") -> dict:
    return {
        "event_id": event_id,
        "type": "close",
        "issuer": endpoint,
        "session_id": session,
    }


class DescendantClosureTests(unittest.TestCase):
    def test_all_recursive_descendants_must_close(self) -> None:
        events = [
            derivation("d-a", "session-a", "root"),
            derivation("d-b", "session-b", "session-a"),
            derivation("d-c", "session-c", "session-a"),
            close("close-a", "session-a"),
            close("close-b", "session-b"),
            # session-c deliberately remains open.
        ]

        decision = decide_logout_completion("root", events=events, barrier=0)

        self.assertFalse(decision.complete)
        self.assertEqual(decision.descendant_count, 3)
        self.assertEqual(decision.closed_descendant_ids, ("session-a", "session-b"))
        self.assertEqual(decision.open_descendant_ids, ("session-c",))
        self.assertIn("open_descendant", decision.reason_codes)

        events.append(close("close-c", "session-c"))
        decision = decide_logout_completion("root", events=events, barrier=0)
        self.assertTrue(decision.complete, decision.to_dict())
        self.assertEqual(len(decision.receipt.closed_session_ids), 3)  # type: ignore[union-attr]

    def test_sibling_roots_do_not_count_as_descendants(self) -> None:
        events = [
            derivation("d-a", "session-a", "root-a"),
            close("close-a", "session-a"),
            derivation("d-b", "session-b", "root-b"),
            # root-b branch remains open but must not block root-a.
        ]

        decision = decide_logout_completion("root-a", events=events, barrier=0)
        self.assertTrue(decision.complete, decision.to_dict())
        self.assertEqual(decision.descendant_count, 1)
        self.assertEqual(decision.closed_descendant_ids, ("session-a",))


if __name__ == "__main__":
    unittest.main()
