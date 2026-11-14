"""Data models for the cross-application logout-notification service.

A logout event comes from an identity provider (issuer).  Two event kinds
are supported:

* ``session`` -- revoke exactly one session, correlated by
  (issuer, session_id).
* ``account`` -- revoke every session of one account, correlated by
  (issuer, account).

Both kinds only affect sessions that were already established at the
moment the event was issued (``login_time <= issued_at``).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

EVENT_TYPE_SESSION = "session"
EVENT_TYPE_ACCOUNT = "account"
EVENT_TYPES = (EVENT_TYPE_SESSION, EVENT_TYPE_ACCOUNT)

# Per-application purge detail lifecycle.
STATUS_PENDING = "pending"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


@dataclass
class SessionRecord:
    """A local session an application established via the identity provider."""

    app: str
    issuer: str
    account: str
    session_id: str
    login_time: int
    active: bool = True

    def to_dict(self):
        return {
            "app": self.app,
            "issuer": self.issuer,
            "account": self.account,
            "session_id": self.session_id,
            "login_time": self.login_time,
            "active": self.active,
        }


@dataclass
class LogoutEvent:
    """A verified logout notification received from the identity system."""

    event_id: str
    issuer: str
    issued_at: int
    event_type: str
    session_id: Optional[str] = None
    account: Optional[str] = None

    def correlation_key(self):
        """The (issuer, key) pair this event correlates sessions by."""
        if self.event_type == EVENT_TYPE_SESSION:
            return (self.issuer, "session_id", self.session_id)
        return (self.issuer, "account", self.account)


@dataclass
class AppPurgeDetail:
    """Processing detail of one purge for one application."""

    app: str
    session_ids: List[str]
    status: str = STATUS_PENDING
    attempts: int = 0
    error: Optional[str] = None
    history: List[dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "app": self.app,
            "session_ids": list(self.session_ids),
            "status": self.status,
            "attempts": self.attempts,
            "error": self.error,
            "history": [dict(h) for h in self.history],
        }


@dataclass
class PurgeRecord:
    """One recorded purge.  Exactly one exists per event_id."""

    event_id: str
    issuer: str
    event_type: str
    issued_at: int
    correlation: tuple
    apps: Dict[str, AppPurgeDetail] = field(default_factory=dict)
    duplicate_count: int = 0
    seq: int = 0

    @property
    def status(self):
        if any(d.status != STATUS_SUCCESS for d in self.apps.values()):
            return STATUS_PENDING
        return "completed"

    def failed_apps(self):
        return [app for app, d in self.apps.items() if d.status == STATUS_FAILED]

    def to_dict(self):
        return {
            "event_id": self.event_id,
            "issuer": self.issuer,
            "event_type": self.event_type,
            "issued_at": self.issued_at,
            "correlation": list(self.correlation),
            "status": self.status,
            "duplicate_count": self.duplicate_count,
            "seq": self.seq,
            "apps": {app: d.to_dict() for app, d in self.apps.items()},
        }
