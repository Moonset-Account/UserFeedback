"""Shared helpers for the acceptance tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from handshake_time import TimeReceipt

BASE = datetime(2026, 11, 20, 0, 0, 0, tzinfo=timezone.utc)


def utc(minutes: float = 0, *, seconds: float = 0) -> datetime:
    """A UTC instant offset from 2026-11-20T00:00:00Z."""

    return BASE + timedelta(minutes=minutes, seconds=seconds)


def receipt(
    start: float,
    end: float,
    source: str,
    *,
    authenticated: bool = True,
) -> TimeReceipt:
    """Build a receipt whose offsets are expressed in minutes."""

    return TimeReceipt(
        start=utc(start),
        end=utc(end),
        source=source,
        authenticated=authenticated,
    )
