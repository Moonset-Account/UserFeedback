"""Timestamp parsing and cutoff comparison helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .errors import AuditDataError

UTC = timezone.utc


def parse_time(value: Any, *, field: str = "timestamp", required: bool = False) -> datetime | None:
    """Parse common timestamp representations into timezone-aware UTC.

    Naive ISO strings and datetimes are treated as UTC because audit inputs
    commonly omit the suffix. Numeric values are interpreted as POSIX seconds;
    values large enough to look like milliseconds are converted from
    milliseconds. Ordering is therefore preserved for synthetic logical clocks
    represented as small integers as well.
    """

    if value is None or value == "":
        if required:
            raise AuditDataError(f"{field} is required")
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool):
        raise AuditDataError(f"{field} must be a timestamp, not boolean")
    elif isinstance(value, (int, float)):
        number = float(value)
        if number > 100_000_000_000:  # milliseconds around year 5138 in seconds
            number /= 1000.0
        try:
            dt = datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise AuditDataError(f"{field} is not a valid epoch timestamp: {value!r}") from exc
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            if required:
                raise AuditDataError(f"{field} is required")
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M:%S",
                "%Y-%m-%d",
                "%Y%m%dT%H%M%S",
                "%Y%m%d%H%M%S",
                "%Y%m%d",
            ):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise AuditDataError(f"{field} is not an ISO-8601 timestamp: {value!r}")
    else:
        raise AuditDataError(f"{field} has unsupported type {type(value).__name__}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt


def format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
