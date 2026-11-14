"""Structured error types raised by the quota adjudicator."""

from __future__ import annotations

from typing import Any


class QuotaError(Exception):
    """Base class for all errors raised by this package."""


class ValidationError(QuotaError, ValueError):
    """An input, config value, or identity value is not usable as-is."""


class ClockRegression(QuotaError):
    """A tenant was adjudicated at an earlier timestamp than before."""

    def __init__(self, *, tenant: str, last_seen: float, received: float) -> None:
        self.tenant = tenant
        self.last_seen = last_seen
        self.received = received
        super().__init__(
            f"tenant {tenant!r} was last adjudicated at {last_seen!r}; "
            f"received earlier time {received!r}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "clock_regression",
            "tenant": self.tenant,
            "last_seen": self.last_seen,
            "received": self.received,
        }


class RequestIdConflict(QuotaError):
    """A request id was replayed with a different account or source address."""

    def __init__(
        self,
        *,
        tenant: str,
        request_id: str,
        existing_account: str,
        existing_source: str,
        incoming_account: str,
        incoming_source: str,
        existing_decided_at: float,
    ) -> None:
        self.tenant = tenant
        self.request_id = request_id
        self.existing_account = existing_account
        self.existing_source = existing_source
        self.incoming_account = incoming_account
        self.incoming_source = incoming_source
        self.existing_decided_at = existing_decided_at
        super().__init__(
            f"request id {request_id!r} for tenant {tenant!r} was already used with "
            f"account {existing_account!r}/source {existing_source!r}; "
            f"replay had account {incoming_account!r}/source {incoming_source!r}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "request_id_conflict",
            "tenant": self.tenant,
            "request_id": self.request_id,
            "existing": {
                "account": self.existing_account,
                "source": self.existing_source,
                "decided_at": self.existing_decided_at,
            },
            "incoming": {
                "account": self.incoming_account,
                "source": self.incoming_source,
            },
        }
