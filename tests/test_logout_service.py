"""Unit tests for the logout-notification service (SEC-20261114-004).

Each test carries a stable case_id (SEC004-Txx) that maps to one explicit
requirement from the original prompt; see README.md for the mapping table.
"""

import unittest

from adapters import ControllableAppAdapter
from models import LogoutEvent
from service import (
    EventValidationError,
    LogoutService,
    UnknownEventError,
)

ISSUER = "idp-1"
OTHER_ISSUER = "idp-2"


def make_service():
    adapter = ControllableAppAdapter()
    return LogoutService(adapter), adapter


def seed_two_app_session(service, account="alice", session_id="s1",
                         issuer=ISSUER, login_time=100):
    """The same IdP session materialised as a local session on app A and B."""
    for app in ("A", "B"):
        service.record_login(
            app=app,
            issuer=issuer,
            account=account,
            session_id=session_id,
            login_time=login_time,
        )


class TestSessionEventCorrelation(unittest.TestCase):
    case_id = "SEC004-T01"

    def test_session_event_matches_issuer_and_session_id_only(self):
        service, adapter = make_service()
        seed_two_app_session(service, account="alice", session_id="s1")
        # Same account, different session: must survive.
        seed_two_app_session(service, account="alice", session_id="s2")
        # Same session_id but a different issuer: must survive.
        seed_two_app_session(service, account="alice", session_id="s1",
                             issuer=OTHER_ISSUER)

        event = LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=200,
                            event_type="session", session_id="s1")
        purge, is_dup = service.process_event(event)

        self.assertFalse(is_dup)
        self.assertEqual(purge.status, "completed")
        self.assertEqual(sorted(purge.apps), ["A", "B"])
        for app in ("A", "B"):
            self.assertEqual(purge.apps[app].session_ids, ["s1"])

        active = {(s.app, s.issuer, s.session_id) for s in service.active_sessions()}
        self.assertEqual(
            active,
            {
                ("A", ISSUER, "s2"),
                ("B", ISSUER, "s2"),
                ("A", OTHER_ISSUER, "s1"),
                ("B", OTHER_ISSUER, "s1"),
            },
        )


class TestAccountPurgeCorrelation(unittest.TestCase):
    case_id = "SEC004-T02"

    def test_account_event_matches_issuer_and_account_across_apps(self):
        service, adapter = make_service()
        seed_two_app_session(service, account="alice", session_id="s1")
        seed_two_app_session(service, account="alice", session_id="s2",
                             login_time=150)
        # Other account, same issuer: must survive.
        seed_two_app_session(service, account="bob", session_id="s3")
        # Same account, other issuer: must survive.
        seed_two_app_session(service, account="alice", session_id="s4",
                             issuer=OTHER_ISSUER)

        event = LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=200,
                            event_type="account", account="alice")
        purge, _ = service.process_event(event)

        self.assertEqual(purge.status, "completed")
        self.assertEqual(sorted(purge.apps["A"].session_ids), ["s1", "s2"])
        self.assertEqual(sorted(purge.apps["B"].session_ids), ["s1", "s2"])

        active = {(s.issuer, s.account, s.session_id)
                  for s in service.active_sessions()}
        self.assertEqual(
            active,
            {(ISSUER, "bob", "s3"), (OTHER_ISSUER, "alice", "s4")},
        )


class TestIssuedAtCutoff(unittest.TestCase):
    case_id = "SEC004-T03"

    def test_only_sessions_established_at_or_before_issued_at_are_purged(self):
        service, adapter = make_service()
        service.record_login(app="A", issuer=ISSUER, account="alice",
                             session_id="old", login_time=99)
        # Boundary: login_time == issued_at counts as already established.
        service.record_login(app="A", issuer=ISSUER, account="alice",
                             session_id="edge", login_time=200)
        # Established after the event was issued: must survive.
        service.record_login(app="A", issuer=ISSUER, account="alice",
                             session_id="new", login_time=201)

        event = LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=200,
                            event_type="account", account="alice")
        purge, _ = service.process_event(event)

        self.assertEqual(sorted(purge.apps["A"].session_ids), ["edge", "old"])
        active_ids = {s.session_id for s in service.active_sessions()}
        self.assertEqual(active_ids, {"new"})


class TestDuplicateDelivery(unittest.TestCase):
    case_id = "SEC004-T04"

    def test_duplicate_event_is_recorded_once_and_not_reprocessed(self):
        service, adapter = make_service()
        seed_two_app_session(service, session_id="s1")
        event = LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=200,
                            event_type="session", session_id="s1")

        purge1, dup1 = service.process_event(event)
        purge2, dup2 = service.process_event(event)
        purge3, dup3 = service.process_event(event)

        self.assertFalse(dup1)
        self.assertTrue(dup2)
        self.assertTrue(dup3)
        self.assertIs(purge1, purge2)
        self.assertIs(purge2, purge3)
        # Exactly one purge record exists, duplicates only counted.
        self.assertEqual(len(service.purges), 1)
        self.assertEqual(purge1.duplicate_count, 2)
        # Adapters were called exactly once per app.
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(
            sorted((c["app"], c["outcome"]) for c in adapter.calls),
            [("A", "success"), ("B", "success")],
        )


class TestAppFailureKeepsPending(unittest.TestCase):
    case_id = "SEC004-T05"

    def test_failing_app_keeps_purge_pending_and_session_active(self):
        service, adapter = make_service()
        seed_two_app_session(service, session_id="s1")
        adapter.set_timeout("B", True)

        event = LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=200,
                            event_type="session", session_id="s1")
        purge, _ = service.process_event(event)

        self.assertEqual(purge.status, "pending")
        self.assertEqual(purge.apps["A"].status, "success")
        self.assertEqual(purge.apps["B"].status, "failed")
        self.assertEqual(purge.failed_apps(), ["B"])
        self.assertIn("timed out", purge.apps["B"].error)

        active = {(s.app, s.session_id) for s in service.active_sessions()}
        self.assertEqual(active, {("B", "s1")})


class TestRetryOnlyFailedApps(unittest.TestCase):
    case_id = "SEC004-T06"

    def test_retry_reattempts_only_failed_apps(self):
        service, adapter = make_service()
        seed_two_app_session(service, session_id="s1")
        adapter.set_failures("B", 1)  # first B attempt times out

        event = LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=200,
                            event_type="session", session_id="s1")
        purge, _ = service.process_event(event)
        self.assertEqual(purge.status, "pending")

        adapter.calls.clear()
        service.retry("e1")

        # Only app B was called again; app A was not re-processed.
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(adapter.calls[0]["app"], "B")
        self.assertEqual(adapter.calls[0]["outcome"], "success")

        self.assertEqual(purge.status, "completed")
        self.assertEqual(purge.apps["A"].attempts, 1)
        self.assertEqual(purge.apps["B"].attempts, 2)
        self.assertEqual(service.active_sessions(), [])

    def test_retry_unknown_event_raises(self):
        service, _ = make_service()
        with self.assertRaises(UnknownEventError):
            service.retry("no-such-event")


class TestReloginSurvivesOldEvent(unittest.TestCase):
    case_id = "SEC004-T07"

    def test_relogin_after_purge_is_not_killed_by_later_old_event(self):
        service, adapter = make_service()
        seed_two_app_session(service, account="alice", session_id="s1",
                             login_time=100)

        first = LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=200,
                            event_type="account", account="alice")
        purge1, _ = service.process_event(first)
        self.assertEqual(purge1.status, "completed")
        self.assertEqual(service.active_sessions(), [])

        # User logs in again on both apps.
        for app in ("A", "B"):
            adapter.simulate_relogin(service, app=app, issuer=ISSUER,
                                     account="alice", session_id="s2",
                                     login_time=300)

        # A different event that was issued before the re-login arrives late.
        late = LogoutEvent(event_id="e2", issuer=ISSUER, issued_at=250,
                           event_type="account", account="alice")
        purge2, _ = service.process_event(late)

        self.assertEqual(purge2.status, "completed")
        self.assertEqual(purge2.apps, {})  # nothing matched
        active = {(s.app, s.session_id) for s in service.active_sessions()}
        self.assertEqual(active, {("A", "s2"), ("B", "s2")})


class TestProcessingDetailsVisible(unittest.TestCase):
    case_id = "SEC004-T08"

    def test_purge_details_expose_per_app_history_and_adapter_calls(self):
        service, adapter = make_service()
        seed_two_app_session(service, session_id="s1")
        adapter.set_failures("B", 1)

        event = LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=200,
                            event_type="session", session_id="s1")
        service.process_event(event)
        service.retry("e1")

        details = service.purge_details()
        self.assertEqual(len(details), 1)
        detail = details[0]
        self.assertEqual(detail["event_id"], "e1")
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(detail["apps"]["A"]["attempts"], 1)
        self.assertEqual(detail["apps"]["B"]["attempts"], 2)
        self.assertEqual(
            [h["result"] for h in detail["apps"]["B"]["history"]],
            ["failed", "success"],
        )
        self.assertEqual(
            [(c["app"], c["outcome"]) for c in adapter.calls],
            [("A", "success"), ("B", "timeout"), ("B", "success")],
        )


class TestEventValidation(unittest.TestCase):
    case_id = "SEC004-T09"

    def test_malformed_events_are_rejected(self):
        service, _ = make_service()
        bad_events = [
            LogoutEvent(event_id="", issuer=ISSUER, issued_at=1,
                        event_type="session", session_id="s1"),
            LogoutEvent(event_id="e1", issuer="", issued_at=1,
                        event_type="session", session_id="s1"),
            LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=1,
                        event_type="bogus"),
            LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=1,
                        event_type="session"),
            LogoutEvent(event_id="e1", issuer=ISSUER, issued_at=1,
                        event_type="account"),
        ]
        for event in bad_events:
            with self.assertRaises(EventValidationError):
                service.process_event(event)
        self.assertEqual(service.purges, {})


if __name__ == "__main__":
    unittest.main()
