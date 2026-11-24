"""Time-window helpers for validity and SoD overlap calculations.

The default representation is a half-open interval ``[start, end)``.  Calendar
dates supplied as an ``end``/``valid_to`` value are treated as inclusive dates,
because IAM validity dates such as ``2026-01-31`` normally include that day.
Callers can always pass datetimes and explicit inclusivity flags to avoid that
convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Tuple

UTC = timezone.utc

_START_KEYS = (
    "start",
    "start_at",
    "starts_at",
    "start_date",
    "starts_on",
    "valid_from",
    "valid_start",
    "effective_from",
    "effective_start",
    "effective_start_date",
    "from_date",
    "from",
    "begin",
    "begin_at",
    "begin_date",
    "not_before",
    "生效时间",
    "生效日期",
    "开始时间",
    "开始日期",
)
_END_KEYS = (
    "end",
    "end_at",
    "ends_at",
    "end_date",
    "ends_on",
    "valid_to",
    "valid_until",
    "valid_end",
    "effective_to",
    "effective_end",
    "effective_end_date",
    "to_date",
    "to",
    "not_after",
    "expires_at",
    "expire_at",
    "expiry_date",
    "expires_on",
    "失效时间",
    "失效日期",
    "结束时间",
    "结束日期",
)


def ensure_utc(value: datetime) -> datetime:
    """Return an aware datetime in UTC, assuming naive values mean UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def comparison_key(value: Any, *, is_end: bool, inclusive: bool) -> Any:
    """Return a comparable key that understands date-vs-datetime boundaries."""

    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        if is_end and inclusive:
            return datetime(
                value.year, value.month, value.day, 23, 59, 59, 999999, tzinfo=UTC
            )
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return value


def parse_boundary(
    value: Any,
    *,
    is_end: bool = False,
) -> Tuple[Any, Optional[bool]]:
    """Parse a time boundary.

    Returns ``(parsed_value, suggested_inclusivity)``.  The suggestion is mostly
    useful for date-only end values, which are treated as inclusive calendar
    dates.  ``None`` means unbounded and is always inclusive in the direction
    that matters.
    """

    if value is None or value == "":
        return None, None

    if isinstance(value, TimeWindow):  # pragma: no cover - defensive convenience
        return value, None

    if isinstance(value, datetime):
        return ensure_utc(value), None

    if isinstance(value, date):
        # Preserve date granularity.  An end date is inclusive by convention
        # when it came from a validity-period field.
        return value, True if is_end else True

    if isinstance(value, (int, float)):
        return value, None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, None
        # Date-only values stay as dates so reports can return the same calendar
        # dates supplied by the caller.
        if len(text) == 10:
            try:
                return date.fromisoformat(text), True if is_end else True
            except ValueError:
                pass
        # Common "Z" suffix is supported by modern fromisoformat, but replacing
        # it also works on packaging combinations where that is less reliable.
        iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(iso_text)
        except ValueError as exc:
            raise ValueError(f"unsupported date/time value: {value!r}") from exc
        return ensure_utc(parsed), None

    raise TypeError(f"unsupported time boundary type: {type(value).__name__}")


@dataclass(frozen=True)
class TimeWindow:
    """An interval with explicit endpoint inclusivity.

    ``None`` for ``start`` or ``end`` means unbounded on that side.
    """

    start: Any = None
    end: Any = None
    start_inclusive: bool = True
    end_inclusive: bool = False

    def __post_init__(self) -> None:
        start, _ = parse_boundary(self.start, is_end=False)
        end, _ = parse_boundary(self.end, is_end=True)

        # If date and datetime boundaries are mixed in one resulting interval,
        # promote dates to datetimes using the endpoint semantics.
        start_is_date = isinstance(start, date) and not isinstance(start, datetime)
        end_is_date = isinstance(end, date) and not isinstance(end, datetime)
        if (
            start is not None
            and end is not None
            and (
                (start_is_date and isinstance(end, datetime))
                or (end_is_date and isinstance(start, datetime))
            )
        ):
            if start_is_date:
                start = datetime(start.year, start.month, start.day, tzinfo=UTC)
            if end_is_date:
                if self.end_inclusive:
                    end = datetime(
                        end.year,
                        end.month,
                        end.day,
                        23,
                        59,
                        59,
                        999999,
                        tzinfo=UTC,
                    )
                else:
                    end_day = datetime(end.year, end.month, end.day, tzinfo=UTC)
                    end = end_day + timedelta(days=1)
                    object.__setattr__(self, "end_inclusive", False)

        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

        if start is not None and end is not None:
            start_key = comparison_key(start, is_end=False, inclusive=self.start_inclusive)
            end_key = comparison_key(end, is_end=True, inclusive=self.end_inclusive)
            if start_key > end_key:
                raise ValueError("TimeWindow start must not be after end")
            if start_key == end_key and not (
                self.start_inclusive and self.end_inclusive
            ):
                raise ValueError("TimeWindow is empty because equal endpoints are excluded")

    @classmethod
    def unbounded(cls) -> "TimeWindow":
        return cls(None, None, True, True)

    @classmethod
    def from_value(cls, value: Any) -> "TimeWindow":
        """Coerce a dict, sequence, string pair, or window into a window."""

        if value is None:
            return cls.unbounded()
        if isinstance(value, TimeWindow):
            return value
        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                raise ValueError("time-window sequence must contain [start, end]")
            return cls(value[0], value[1])
        if isinstance(value, dict):
            start_raw = _first_present(value, _START_KEYS)
            end_raw = _first_present(value, _END_KEYS)
            start, start_suggestion = parse_boundary(start_raw, is_end=False)
            end, end_suggestion = parse_boundary(end_raw, is_end=True)

            explicit_inclusive = value.get("inclusive")
            start_inc = value.get(
                "start_inclusive",
                value.get("inclusive_start", explicit_inclusive if explicit_inclusive is not None else True),
            )
            end_inc = value.get(
                "end_inclusive",
                value.get("inclusive_end", explicit_inclusive if explicit_inclusive is not None else None),
            )
            if start_inc is None:
                start_inc = start_suggestion if start_suggestion is not None else True
            if end_inc is None:
                end_inc = end_suggestion if end_suggestion is not None else False
            return cls(
                start=start,
                end=end,
                start_inclusive=bool(start_inc),
                end_inclusive=bool(end_inc),
            )
        raise TypeError(f"cannot interpret {value!r} as a TimeWindow")

    def intersection(self, other: "TimeWindow") -> Optional["TimeWindow"]:
        """Return the overlap, or ``None`` when the windows do not overlap."""

        other = TimeWindow.from_value(other)

        self_start_key = (
            None
            if self.start is None
            else comparison_key(self.start, is_end=False, inclusive=self.start_inclusive)
        )
        other_start_key = (
            None
            if other.start is None
            else comparison_key(other.start, is_end=False, inclusive=other.start_inclusive)
        )

        # Lower bound = max(start values).
        if self.start is None:
            start = other.start
            start_inc = other.start_inclusive
        elif other.start is None:
            start = self.start
            start_inc = self.start_inclusive
        elif self_start_key > other_start_key:
            start = self.start
            start_inc = self.start_inclusive
        elif other_start_key > self_start_key:
            start = other.start
            start_inc = other.start_inclusive
        else:
            start = self.start
            start_inc = self.start_inclusive and other.start_inclusive

        self_end_key = (
            None
            if self.end is None
            else comparison_key(self.end, is_end=True, inclusive=self.end_inclusive)
        )
        other_end_key = (
            None
            if other.end is None
            else comparison_key(other.end, is_end=True, inclusive=other.end_inclusive)
        )

        # Upper bound = min(end values).
        if self.end is None:
            end = other.end
            end_inc = other.end_inclusive
        elif other.end is None:
            end = self.end
            end_inc = self.end_inclusive
        elif self_end_key < other_end_key:
            end = self.end
            end_inc = self.end_inclusive
        elif other_end_key < self_end_key:
            end = other.end
            end_inc = other.end_inclusive
        else:
            end = self.end
            end_inc = self.end_inclusive and other.end_inclusive

        if start is not None and end is not None:
            start_key = comparison_key(start, is_end=False, inclusive=start_inc)
            end_key = comparison_key(end, is_end=True, inclusive=end_inc)
            if start_key > end_key:
                return None
            if start_key == end_key and not (start_inc and end_inc):
                return None

        try:
            return TimeWindow(start, end, start_inc, end_inc)
        except ValueError:
            return None

    def overlaps(self, other: "TimeWindow") -> bool:
        return self.intersection(other) is not None

    def to_dict(self) -> dict:
        return {
            "start": serialize_boundary(self.start),
            "end": serialize_boundary(self.end),
            "start_inclusive": self.start_inclusive,
            "end_inclusive": self.end_inclusive,
        }


def _first_present(data: dict, keys: Tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def serialize_boundary(value: Any) -> Any:
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):  # pragma: no cover - dates are converted earlier
        return value.isoformat()
    return value


def window_from_grant(data: dict) -> TimeWindow:
    """Extract a validity window from common grant/request shapes."""

    validity = data.get("validity") or data.get("validity_period") or data.get("period")
    if validity is not None:
        return TimeWindow.from_value(validity)

    # Allow {"validity": {"from": ..., "to": ...}} already handled, and also
    # top-level aliases.
    return TimeWindow.from_value(data)
