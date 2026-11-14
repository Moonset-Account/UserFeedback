"""Controllable clock so cross-day budget behaviour is deterministic in tests."""

from datetime import datetime, timezone


class Clock:
    """A manually controlled UTC clock.

    Time can only move forward: setting an earlier timestamp raises
    ``ValueError`` so scenario files cannot silently rewind the clock.
    """

    def __init__(self, start: datetime | None = None):
        if start is None:
            start = datetime(2026, 11, 14, 0, 0, 0, tzinfo=timezone.utc)
        self._now = self._ensure_aware(start)

    @staticmethod
    def _ensure_aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def now(self) -> datetime:
        return self._now

    def set_time(self, value: datetime) -> datetime:
        value = self._ensure_aware(value)
        if value < self._now:
            raise ValueError(
                f"clock cannot move backwards: {value.isoformat()} < {self._now.isoformat()}"
            )
        self._now = value
        return self._now

    def advance(self, **kwargs) -> datetime:
        """Advance by a timedelta-style keyword set (e.g. hours=25)."""
        from datetime import timedelta

        self._now = self._now + timedelta(**kwargs)
        return self._now

    def day_key(self) -> str:
        return self._now.date().isoformat()
