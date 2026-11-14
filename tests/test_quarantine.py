"""Unit tests for the SEC-20261114-018 quarantine state module.

Requirement mapping (see README.md):
  SEC018-RQ-01  multi-recipient isolation
  SEC018-RQ-02  delivery record keyed by (email, recipient) with version+reasons
  SEC018-RQ-03  approval must match recipient+version and cover active reasons
  SEC018-RQ-04  appended reasons send release tasks back to waiting
  SEC018-RQ-05  release idempotency / second independent request / confirm dedupe
  SEC018-RQ-06  operator per-recipient status view
  SEC018-BC-CLI app.py command batch runner
"""

import unittest

import app
from quarantine import (
    RELEASABLE,
    RELEASED,
    WAITING,
    QuarantineError,
    QuarantineStore,
)


def make_store():
    return QuarantineStore()


class RQ01MultiRecipientIsolationTests(unittest.TestCase):
    """SEC018-RQ-01: one recipient's successful appeal must not free others."""

    def test_release_is_scoped_to_single_recipient(self):
        store = make_store()
        store.record_delivery("m1", "alice", "v1", ["spam"])
        store.record_delivery("m1", "bob", "v1", ["spam"])
        store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        out = store.release("m1", "alice", "req-1")
        self.assertTrue(out["executed"])
        recipients = store.mail_status("m1")["recipients"]
        self.assertEqual(RELEASED, recipients["alice"]["state"])
        self.assertEqual(WAITING, recipients["bob"]["state"])

    def test_approval_for_one_recipient_does_not_cover_another(self):
        store = make_store()
        store.record_delivery("m1", "alice", "v1", ["spam"])
        store.record_delivery("m1", "bob", "v1", ["spam"])
        store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        recipients = store.mail_status("m1")["recipients"]
        self.assertEqual(RELEASABLE, recipients["alice"]["state"])
        self.assertEqual(WAITING, recipients["bob"]["state"])
        with self.assertRaises(QuarantineError):
            store.release("m1", "bob", "req-b")


class RQ02DeliveryRecordTests(unittest.TestCase):
    """SEC018-RQ-02: records located by (email, recipient) carry version+reasons."""

    def test_record_exposes_version_and_reasons(self):
        store = make_store()
        out = store.record_delivery("m1", "alice", "v1", ["spam", "virus"])
        self.assertEqual("v1", out["version"])
        self.assertEqual(["spam", "virus"], out["reasons"])
        self.assertEqual(WAITING, out["state"])

    def test_same_email_different_recipients_are_independent_records(self):
        store = make_store()
        store.record_delivery("m1", "alice", "v1", ["spam"])
        store.record_delivery("m1", "bob", "v9", ["virus"])
        recipients = store.mail_status("m1")["recipients"]
        self.assertEqual("v1", recipients["alice"]["version"])
        self.assertEqual("v9", recipients["bob"]["version"])
        self.assertEqual(["spam"], recipients["alice"]["reasons"])
        self.assertEqual(["virus"], recipients["bob"]["reasons"])

    def test_record_update_replaces_version_and_reason_set(self):
        store = make_store()
        store.record_delivery("m1", "alice", "v1", ["spam"])
        out = store.record_delivery("m1", "alice", "v2", ["phishing"])
        self.assertEqual("v1", out["previous_version"])
        self.assertEqual("v2", out["version"])
        self.assertEqual(["phishing"], out["reasons"])

    def test_invalid_record_rejected(self):
        store = make_store()
        with self.assertRaises(QuarantineError):
            store.record_delivery("m1", "alice", "v1", [])
        with self.assertRaises(QuarantineError):
            store.record_delivery("", "alice", "v1", ["spam"])
        with self.assertRaises(QuarantineError):
            store.record_delivery("m1", "alice", "v1", ["spam", "spam"])


class RQ03ApprovalCoverageTests(unittest.TestCase):
    """SEC018-RQ-03: approval needs same recipient, same version, full coverage."""

    def setUp(self):
        self.store = make_store()
        self.store.record_delivery("m1", "alice", "v1", ["spam", "virus"])

    def test_partial_coverage_keeps_task_waiting(self):
        out = self.store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        self.assertEqual(WAITING, out["state"])
        self.assertEqual(["virus"], out["missing_reasons"])

    def test_full_coverage_across_approvals_makes_releasable(self):
        self.store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        out = self.store.submit_approval("m1", "alice", "ap-2", "v1", ["virus"])
        self.assertEqual(RELEASABLE, out["state"])
        self.assertEqual([], out["missing_reasons"])

    def test_single_approval_covering_all_reasons(self):
        out = self.store.submit_approval(
            "m1", "alice", "ap-1", "v1", ["spam", "virus"]
        )
        self.assertEqual(RELEASABLE, out["state"])

    def test_stale_version_approval_rejected(self):
        with self.assertRaises(QuarantineError):
            self.store.submit_approval("m1", "alice", "ap-1", "v0", ["spam", "virus"])

    def test_approval_without_record_rejected(self):
        with self.assertRaises(QuarantineError):
            self.store.submit_approval("m1", "ghost", "ap-1", "v1", ["spam"])

    def test_approval_id_replay_is_idempotent(self):
        first = self.store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        replay = self.store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])

    def test_approval_id_conflict_rejected(self):
        self.store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        with self.assertRaises(QuarantineError):
            self.store.submit_approval("m1", "alice", "ap-1", "v1", ["virus"])


class RQ04ReasonAppendTests(unittest.TestCase):
    """SEC018-RQ-04: appended reasons send release tasks back to waiting."""

    def test_appended_reason_returns_releasable_task_to_waiting(self):
        store = make_store()
        store.record_delivery("m1", "alice", "v1", ["spam"])
        store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        before = store.mail_status("m1")["recipients"]["alice"]["state"]
        self.assertEqual(RELEASABLE, before)
        out = store.record_delivery("m1", "alice", "v1", ["spam", "phishing"])
        self.assertEqual(WAITING, out["state"])
        status = store.mail_status("m1")["recipients"]["alice"]
        self.assertEqual(["phishing"], status["missing_reasons"])

    def test_new_approval_covering_appended_reason_restores_releasable(self):
        store = make_store()
        store.record_delivery("m1", "alice", "v1", ["spam"])
        store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        store.record_delivery("m1", "alice", "v1", ["spam", "phishing"])
        out = store.submit_approval("m1", "alice", "ap-2", "v1", ["phishing"])
        self.assertEqual(RELEASABLE, out["state"])

    def test_version_bump_invalidates_prior_approvals(self):
        store = make_store()
        store.record_delivery("m1", "alice", "v1", ["spam"])
        store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        out = store.record_delivery("m1", "alice", "v2", ["spam"])
        self.assertEqual(WAITING, out["state"])
        with self.assertRaises(QuarantineError):
            store.submit_approval("m1", "alice", "ap-2", "v1", ["spam"])
        out = store.submit_approval("m1", "alice", "ap-3", "v2", ["spam"])
        self.assertEqual(RELEASABLE, out["state"])

    def test_version_bump_after_release_starts_new_cycle(self):
        store = make_store()
        store.record_delivery("m1", "alice", "v1", ["spam"])
        store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])
        store.release("m1", "alice", "req-1")
        self.assertEqual(
            RELEASED, store.mail_status("m1")["recipients"]["alice"]["state"]
        )
        out = store.record_delivery("m1", "alice", "v2", ["spam"])
        self.assertEqual(WAITING, out["state"])


class RQ05ReleaseIdempotencyTests(unittest.TestCase):
    """SEC018-RQ-05: retry vs second independent request; confirm dedupe."""

    def setUp(self):
        self.store = make_store()
        self.store.record_delivery("m1", "alice", "v1", ["spam"])
        self.store.submit_approval("m1", "alice", "ap-1", "v1", ["spam"])

    def test_release_while_waiting_rejected(self):
        store = make_store()
        store.record_delivery("m2", "bob", "v1", ["spam"])
        with self.assertRaises(QuarantineError):
            store.release("m2", "bob", "req-1")

    def test_first_release_executes_and_confirms_once(self):
        out = self.store.release("m1", "alice", "req-1")
        self.assertTrue(out["executed"])
        self.assertTrue(out["downstream_confirmation_sent"])
        self.assertEqual("cfm:m1:alice:v1", out["confirmation_id"])
        self.assertFalse(out["idempotent_replay"])

    def test_same_request_retry_is_idempotent(self):
        first = self.store.release("m1", "alice", "req-1")
        retry = self.store.release("m1", "alice", "req-1")
        self.assertTrue(retry["idempotent_replay"])
        self.assertFalse(retry["downstream_confirmation_sent"])
        self.assertEqual(first["confirmation_id"], retry["confirmation_id"])
        confirmations = self.store.mail_status("m1")["recipients"]["alice"][
            "confirmations"
        ]
        self.assertEqual(1, len(confirmations))

    def test_second_independent_request_no_duplicate_confirmation(self):
        self.store.release("m1", "alice", "req-1")
        second = self.store.release("m1", "alice", "req-2")
        self.assertFalse(second["idempotent_replay"])
        self.assertTrue(second["duplicate_of_prior_release"])
        self.assertFalse(second["executed"])
        self.assertFalse(second["downstream_confirmation_sent"])
        view = self.store.mail_status("m1")["recipients"]["alice"]
        self.assertEqual(1, len(view["confirmations"]))
        self.assertEqual(
            ["req-1", "req-2"],
            [item["request_id"] for item in view["release_requests"]],
        )

    def test_downstream_confirmation_recorded_once(self):
        out = self.store.release("m1", "alice", "req-1")
        confirmation_id = out["confirmation_id"]
        first = self.store.confirm_downstream("m1", "alice", confirmation_id)
        self.assertTrue(first["recorded"])
        duplicate = self.store.confirm_downstream("m1", "alice", confirmation_id)
        self.assertFalse(duplicate["recorded"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(1, duplicate["acknowledged_count"])

    def test_unknown_confirmation_rejected(self):
        with self.assertRaises(QuarantineError):
            self.store.confirm_downstream("m1", "alice", "cfm:nope")


class RQ06OperatorStatusViewTests(unittest.TestCase):
    """SEC018-RQ-06: operator sees per-recipient waiting/releasable/released."""

    def test_status_view_groups_all_states_under_one_email(self):
        store = make_store()
        store.record_delivery("m1", "alice", "v1", ["spam"])
        store.record_delivery("m1", "bob", "v1", ["spam"])
        store.record_delivery("m1", "carol", "v1", ["spam"])
        store.submit_approval("m1", "bob", "ap-b", "v1", ["spam"])
        store.submit_approval("m1", "carol", "ap-c", "v1", ["spam"])
        store.release("m1", "carol", "req-c")
        recipients = store.mail_status("m1")["recipients"]
        self.assertEqual({"alice", "bob", "carol"}, set(recipients))
        self.assertEqual(WAITING, recipients["alice"]["state"])
        self.assertEqual(RELEASABLE, recipients["bob"]["state"])
        self.assertEqual(RELEASED, recipients["carol"]["state"])

    def test_unknown_email_rejected(self):
        store = make_store()
        with self.assertRaises(QuarantineError):
            store.mail_status("nope")


class CliBatchTests(unittest.TestCase):
    """SEC018-BC-CLI: app.run_commands applies a JSON batch end to end."""

    def test_run_commands_end_to_end(self):
        commands = [
            {"op": "record_delivery", "email": "m1", "recipient": "alice",
             "version": "v1", "reasons": ["spam"]},
            {"op": "submit_approval", "email": "m1", "recipient": "alice",
             "approval_id": "ap-1", "version": "v1", "reasons": ["spam"]},
            {"op": "release", "email": "m1", "recipient": "alice",
             "request_id": "req-1"},
            {"op": "status", "email": "m1"},
        ]
        results = app.run_commands(commands)
        self.assertTrue(all(item["ok"] for item in results))
        self.assertEqual(RELEASABLE, results[1]["result"]["state"])
        self.assertTrue(results[2]["result"]["executed"])
        self.assertEqual(
            RELEASED, results[3]["result"]["recipients"]["alice"]["state"]
        )

    def test_unknown_op_reported_per_command(self):
        results = app.run_commands([{"op": "bogus"}])
        self.assertFalse(results[0]["ok"])
        self.assertIn("unknown op", results[0]["error"])

    def test_business_error_does_not_abort_batch(self):
        commands = [
            {"op": "release", "email": "mX", "recipient": "alice",
             "request_id": "req-1"},
            {"op": "record_delivery", "email": "mX", "recipient": "alice",
             "version": "v1", "reasons": ["spam"]},
        ]
        results = app.run_commands(commands)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[1]["ok"])


if __name__ == "__main__":
    unittest.main()
