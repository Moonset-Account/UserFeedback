"""Time and duration coercion helpers.

The module intentionally uses only the Python standard library.  All external
datetimes are normalised to timezone-aware UTC datetimes so that naive values
cannot produce subtraction errors or ambiguous comparisons.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

UTC = timezone.utc

_DURATION_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>microseconds?|milliseconds?|seconds?|secs?|ms|us|µs|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w)",
    re.IGNORECASE,
)
_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "microsecond": 1e-6,
    "us": 1e-6,
    "µs": 1e-6,
    "millisecond": 1e-3,
    "ms": 1e-3,
    "second": 1,
    "sec": 1,
    "s": 1,
    "minute": 60,
    "min": 60,
    "m": 60,
    "hour": 3600,
    "hr": 3600,
    "h": 3600,
    "day": 86400,
    "d": 86400,
    "week": 604800,
    "w": 604800,
}


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def coerce_datetime(value: Any, field_name: str = "datetime") -> datetime:
    """Coerce common datetime representations to aware UTC.

    Supports :class:`datetime`, :class:`date`, ISO-8601 strings (including a
    trailing ``Z``), and numeric Unix timestamps in seconds.  Naive datetimes
    are documented as UTC.
    """

    if value is None:
        raise ValueError(f"{field_name} must not be None")

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(float(value), tz=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} must not be empty")
        if text.isdigit() or (text.startswith(("-", "+")) and text[1:].isdigit()):
            return datetime.fromtimestamp(int(text), tz=UTC)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid {field_name}: {value!r}") from exc
    else:
        raise ValueError(f"invalid {field_name}: {value!r}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt


def _duration_from_iso(text: str) -> timedelta | None:
    match = _ISO_DURATION_RE.match(text.upper())
    if not match:
        return None
    parts = {k: float(v) for k, v in match.groupdict().items() if v is not None}
    if not parts:
        return None
    return timedelta(
        days=parts.get("days", 0.0),
        hours=parts.get("hours", 0.0),
        minutes=parts.get("minutes", 0.0),
        seconds=parts.get("seconds", 0.0),
    )


def coerce_timedelta(value: Any, field_name: str = "duration") -> timedelta | None:
    """Coerce seconds, durations, or simple duration strings to timedelta."""

    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise ValueError(f"invalid {field_name}: {value!r}")
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    if isinstance(value, dict):
        allowed = {
            "weeks",
            "days",
            "hours",
            "minutes",
            "seconds",
            "milliseconds",
            "microseconds",
        }
        kwargs = {k: float(v) for k, v in value.items() if k in allowed and v is not None}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown duration fields for {field_name}: {sorted(unknown)}")
        return timedelta(**kwargs)

    text = str(value).strip()
    if not text:
        return None

    iso = _duration_from_iso(text)
    if iso is not None:
        return iso

    matches = list(_DURATION_RE.finditer(text))
    if matches:
        consumed = "".join(m.group(0) for m in matches).strip()
        if consumed != text:
            raise ValueError(f"invalid {field_name}: {value!r}")
        total_seconds = 0.0
        for match in matches:
            raw_unit = match.group("unit").lower()
            factor = _UNIT_SECONDS.get(raw_unit)
            if factor is None and raw_unit.endswith("s"):
                factor = _UNIT_SECONDS.get(raw_unit[:-1])
            if factor is None:
                raise ValueError(f"invalid duration unit in {field_name}: {raw_unit!r}")
            total_seconds += float(match.group("value")) * factor
        return timedelta(seconds=total_seconds)

    try:
        return timedelta(seconds=float(text))
    except ValueError as exc:
        raise ValueError(f"invalid {field_name}: {value!r}") from exc
