"""Core logout-notification service.

Receives verified logout events from the identity system and drives the
per-application adapters.  Guarantees:

* single-session events correlate by (issuer, session_id);
* account-purge events correlate by (issuer, account);
* both only affect sessions with ``login_time <= issued_at`` (sessions
  established after the event was issued -- e.g. a fresh re-login -- are
  never touched);
* an event_id is purged at most once -- duplicate deliveries are counted
  on the existing purge record and cause no new adapter calls;
* applications that fail keep their detail in ``failed`` state so the
  purge stays ``pending``; ``retry()`` re-attempts only the failed
  applications.
"""

from models import (
    EVENT_TYPES,
    EVENT_TYPE_ACCOUNT,
    EVENT_TYPE_SESSION,
    STATUS_FAILED,
    STATUS_SUCCESS,
    AppPurgeDetail,
    LogoutEvent,
    PurgeRecord,
    SessionRecord,
)
from adapters import AppTimeoutError  # noqa: F401  (re-exported for callers)


class EventValidationError(ValueError):
    """Raised when a logout event is malformed."""


class UnknownEventError(KeyError):
    """Raised when retrying an event_id that was never processed."""


def validate_event(event):
    if not event.event_id:
        raise EventValidationError("event_id is required")
    if not event.issuer:
        raise EventValidationError("issuer is required")
    if event.event_type not in EVENT_TYPES:
        raise EventValidationError(
            "event_type must be one of %r, got %r" % (EVENT_TYPES, event.event_type)
        )
    if event.event_type == EVENT_TYPE_SESSION and not event.session_id:
        raise EventValidationError("session_id is required for session events")
    if event.event_type == EVENT_TYPE_ACCOUNT and not event.account:
        raise EventValidationError("account is required for account events")
    if not isinstance(event.issued_at, int):
        raise EventValidationError("issued_at must be an integer")


class LogoutService:
    def __init__(self, adapter):
        self.adapter = adapter
        self.sessions = []
        self.purges = {}        # event_id -> PurgeRecord
        self.purge_order = []   # event_ids in first-processing order
        self._seq = 0

    # -- session bookkeeping --------------------------------------------------
    def record_login(self, app, issuer, account, session_id, login_time):
        record = SessionRecord(
            app=app,
            issuer=issuer,
            account=account,
            session_id=session_id,
            login_time=login_time,
        )
        self.sessions.append(record)
        return record

    def active_sessions(self):
        return [s for s in self.sessions if s.active]

    # -- event processing -------------------------------------------------------
    def process_event(self, event):
        """Process a verified logout event.

        Returns ``(purge_record, is_duplicate)``.  A duplicate delivery
        never creates a second purge and never calls the adapters again.
        """
        validate_event(event)
        existing = self.purges.get(event.event_id)
        if existing is not None:
            existing.duplicate_count += 1
            return existing, True

        targets = self._match(event)
        by_app = {}
        for session in targets:
            by_app.setdefault(session.app, []).append(session)

        self._seq += 1
        purge = PurgeRecord(
            event_id=event.event_id,
            issuer=event.issuer,
            event_type=event.event_type,
            issued_at=event.issued_at,
            correlation=event.correlation_key(),
            seq=self._seq,
        )
        for app in sorted(by_app):
            app_sessions = by_app[app]
            detail = AppPurgeDetail(
                app=app, session_ids=[s.session_id for s in app_sessions]
            )
            purge.apps[app] = detail
            self._attempt(detail, app_sessions)

        self.purges[event.event_id] = purge
        self.purge_order.append(event.event_id)
        return purge, False

    def retry(self, event_id):
        """Re-attempt only the applications that failed for this purge."""
        try:
            purge = self.purges[event_id]
        except KeyError:
            raise UnknownEventError(event_id)
        for app in sorted(purge.apps):
            detail = purge.apps[app]
            if detail.status != STATUS_FAILED:
                continue
            sessions = [
                s
                for s in self.sessions
                if s.active and s.app == app and s.session_id in detail.session_ids
            ]
            self._attempt(detail, sessions)
        return purge

    def retry_all_pending(self):
        """Retry every purge that still has failed applications."""
        retried = []
        for event_id in self.purge_order:
            purge = self.purges[event_id]
            if purge.failed_apps():
                self.retry(event_id)
                retried.append(event_id)
        return retried

    # -- internals ----------------------------------------------------------------
    def _match(self, event):
        matched = []
        for session in self.sessions:
            if not session.active:
                continue
            if session.issuer != event.issuer:
                continue
            if session.login_time > event.issued_at:
                # Established after the event was issued: not covered.
                continue
            if event.event_type == EVENT_TYPE_SESSION:
                if session.session_id != event.session_id:
                    continue
            else:
                if session.account != event.account:
                    continue
            matched.append(session)
        return matched

    def _attempt(self, detail, sessions):
        detail.attempts += 1
        try:
            self.adapter.terminate(detail.app, list(sessions))
        except AppTimeoutError as exc:
            detail.status = STATUS_FAILED
            detail.error = str(exc)
            detail.history.append(
                {"attempt": detail.attempts, "result": "failed", "error": str(exc)}
            )
        else:
            detail.status = STATUS_SUCCESS
            detail.error = None
            for session in sessions:
                session.active = False
            detail.history.append(
                {"attempt": detail.attempts, "result": "success", "error": None}
            )

    # -- introspection ---------------------------------------------------------
    def purge_details(self):
        """Per-purge processing detail, in first-processing order."""
        return [self.purges[eid].to_dict() for eid in self.purge_order]
