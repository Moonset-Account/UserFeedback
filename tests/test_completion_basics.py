from __future__ import annotations

import unittest

from identity_logout import LogoutDecider, decide_logout_completion
from logout_completion import (
    LogoutCompletionDecider,
    can_issue_completion_receipt,
)
from tests.factories import (
    BROWSER,
    ROOT,
    TARGET,
    barrier_positions,
    copied_events,
    main_events,
    passed_watermarks,
)


class CompletionBasicsTests(unittest.TestCase):
    def test_happy_path_emits_deterministic_receipt(self) -> None:
        decision = decide_logout_completion(
            ROOT,
            events=main_events(),
            watermarks=passed_watermarks(),
            barrier=barrier_positions(),
        )

        self.assertTrue(decision.complete, decision.to_dict())
        self.assertIsNotNone(decision.receipt)
        self.assertEqual(decision.descendant_count, 1)
        self.assertEqual(decision.closed_descendant_ids, (BROWSER,))
        self.assertEqual(decision.open_descendant_ids, ())
        self.assertEqual(decision.in_flight_transfer_ids, ())
        self.assertEqual(decision.missing, ())
        self.assertEqual(decision.missing_endpoints, ())

        receipt = decision.receipt
        assert receipt is not None
        self.assertTrue(receipt.receipt_id.startswith(f"logout-receipt:{ROOT}:"))
        self.assertEqual(receipt.descendant_count, 1)
        self.assertEqual(receipt.closed_session_ids, (BROWSER,))
        self.assertIn("dc-source", receipt.relevant_endpoints)
        self.assertIn(TARGET, receipt.relevant_endpoints)

        # Re-evaluating unchanged evidence yields the same deterministic ID.
        second = decide_logout_completion(
            ROOT,
            events=main_events(),
            watermarks=passed_watermarks(),
            barrier=barrier_positions(),
        )
        assert second.receipt is not None
        self.assertEqual(second.receipt.receipt_id, receipt.receipt_id)
        self.assertEqual(second.receipt.evidence_digest, receipt.evidence_digest)

    def test_stateful_ingestion_and_issue_method(self) -> None:
        decider = LogoutDecider(barrier=barrier_positions())
        decider.ingest_events(copied_events())
        decider.update_watermarks(passed_watermarks())

        receipt = decider.issue_completion_receipt(ROOT)
        self.assertEqual(receipt.root_session_id, ROOT)
        self.assertEqual(receipt.closed_session_ids, (BROWSER,))

    def test_top_level_aliases_reexport_same_api(self) -> None:
        self.assertIs(LogoutCompletionDecider, LogoutDecider)
        self.assertIs(can_issue_completion_receipt, decide_logout_completion)

    def test_other_root_without_barrier_does_not_issue_receipt(self) -> None:
        decider = LogoutDecider(
            events=main_events(),
            watermarks=passed_watermarks(),
        )

        decision = decider.evaluate("some-other-root")
        self.assertFalse(decision.complete)
        self.assertEqual(decision.descendant_count, 0)
        self.assertIn("barrier_not_configured", decision.reason_codes)

    def test_barrier_is_required_by_default(self) -> None:
        decision = decide_logout_completion(ROOT, events=main_events())
        self.assertFalse(decision.complete)
        self.assertIn("barrier_not_configured", decision.reason_codes)

    def test_root_need_not_have_close_unless_requested(self) -> None:
        decider = LogoutDecider(
            events=main_events(),
            watermarks=passed_watermarks(),
            barrier=barrier_positions(),
        )
        self.assertTrue(decider.evaluate(ROOT).complete)

        strict_root = decider.evaluate(ROOT, require_root_closed=True)
        self.assertFalse(strict_root.complete)
        self.assertIn("open_root_session", strict_root.reason_codes)


if __name__ == "__main__":
    unittest.main()
