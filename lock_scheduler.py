"""Core logic for the cross-timezone workspace lock scheduler.

Rules implemented (see README.md):
- Weekly shifts are local wall-clock periods, half-open [start, end).
- Cross-midnight shifts (end <= start) are attributed to the start date and
  end on the following local day.
- DST transitions (zoneinfo):
    * ambiguous local time (fall-back overlap) -> the EARLIER instant wins;
    * nonexistent local time (spring-forward gap) -> shifted FORWARD to the
      first valid wall-clock moment.
- Temporary extensions are absolute UTC intervals, half-open [start, end).
- A workspace must be locked when the current instant is outside every
  allowed interval (weekly shifts + temporary extensions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc

_WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------

def parse_weekday(value) -> int:
    """Accept 0-6 (Monday=0) or case-insensitive mon..sun names."""
    if isinstance(value, bool):
        raise ValueError(f"invalid weekday: {value!r}")
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError(f"weekday int out of range 0-6: {value!r}")
    key = str(value).strip().lower()[:3]
    if key in _WEEKDAY_NAMES:
        return _WEEKDAY_NAMES.index(key)
    raise ValueError(f"invalid weekday: {value!r}")


def parse_hhmm(text: str) -> time:
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {text!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"wall time out of range: {text!r}")
    return time(hh, mm)


def parse_utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp that must carry an explicit offset."""
    s = str(text).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"UTC timestamp needs an explicit offset: {text!r}")
    return dt.astimezone(UTC)


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# DST-aware local wall clock -> UTC conversion
# ---------------------------------------------------------------------------

def _round_trips(wall: datetime, tz: ZoneInfo, fold: int) -> datetime | None:
    """Return the UTC instant for `wall` under `fold` if it round-trips."""
    aware = wall.replace(tzinfo=tz, fold=fold)
    utc = aware.astimezone(UTC)
    if utc.astimezone(tz).replace(tzinfo=None) == wall:
        return utc
    return None


def local_wall_to_utc(wall: datetime, tz: ZoneInfo) -> datetime:
    """Convert a naive local wall-clock time to a UTC instant.

    - Ambiguous wall time (fall-back overlap): pick the EARLIER instant.
    - Nonexistent wall time (spring-forward gap): shift FORWARD minute by
      minute to the first valid wall-clock moment.
    """
    if wall.tzinfo is not None:
        raise ValueError("wall time must be naive")
    candidates = [u for f in (0, 1) if (u := _round_trips(wall, tz, f)) is not None]
    if candidates:
        return min(candidates)
    probe = wall
    for _ in range(24 * 60):  # gaps are far shorter than a day
        probe += timedelta(minutes=1)
        utc = _round_trips(probe, tz, 0)
        if utc is not None:
            return utc
    raise ValueError(f"cannot resolve wall time {wall.isoformat()} in {tz.key}")


# ---------------------------------------------------------------------------
# schedule model
# ---------------------------------------------------------------------------

@dataclass
class Interval:
    """A half-open [start, end) allowed interval on the UTC timeline."""

    start: datetime
    end: datetime
    kind: str  # "weekly_shift" | "temporary_extension"
    detail: dict = field(default_factory=dict)

    def covers(self, instant: datetime) -> bool:
        return self.start <= instant < self.end


def weekly_intervals(shifts: list[dict], tz: ZoneInfo, day_from, day_to) -> list[Interval]:
    """Materialize weekly shifts as UTC intervals for each local date in range.

    A shift whose end <= start crosses midnight: it belongs to the local date
    it starts on and ends on the next local day.
    """
    out: list[Interval] = []
    day = day_from
    while day <= day_to:
        for shift in shifts:
            if shift["weekday"] != day.weekday():
                continue
            start_wall = datetime.combine(day, shift["start"])
            if shift["end"] > shift["start"]:
                end_day = day
            else:  # cross-midnight (or 24h when equal): attribute to start date
                end_day = day + timedelta(days=1)
            end_wall = datetime.combine(end_day, shift["end"])
            out.append(
                Interval(
                    start=local_wall_to_utc(start_wall, tz),
                    end=local_wall_to_utc(end_wall, tz),
                    kind="weekly_shift",
                    detail={
                        "weekday": _WEEKDAY_NAMES[day.weekday()],
                        "local_date": day.isoformat(),
                        "local_start": shift["start"].strftime("%H:%M"),
                        "local_end": shift["end"].strftime("%H:%M"),
                    },
                )
            )
        day += timedelta(days=1)
    return out


def extension_intervals(extensions: list[dict]) -> list[Interval]:
    out: list[Interval] = []
    for ext in extensions:
        start = parse_utc(ext["start_utc"])
        end = parse_utc(ext["end_utc"])
        if not start < end:
            raise ValueError(f"extension start must be before end: {ext!r}")
        out.append(
            Interval(
                start=start,
                end=end,
                kind="temporary_extension",
                detail={"label": ext.get("label", "")},
            )
        )
    return out


def merge_intervals(intervals: list[Interval]) -> list[tuple[datetime, datetime]]:
    """Union of half-open intervals; touching intervals merge (no state flip)."""
    spans = sorted((iv.start, iv.end) for iv in intervals)
    merged: list[list[datetime]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------

def _normalize_shifts(raw_shifts: list[dict]) -> list[dict]:
    return [
        {
            "weekday": parse_weekday(s["weekday"]),
            "start": parse_hhmm(s["start"]),
            "end": parse_hhmm(s["end"]),
        }
        for s in raw_shifts
    ]


def evaluate(config: dict) -> dict:
    """Evaluate lock state for every workspace at config['now_utc']."""
    tz = ZoneInfo(config["timezone"])
    now = parse_utc(config["now_utc"])
    shifts = _normalize_shifts(config.get("weekly_shifts", []))
    extensions = config.get("extensions", [])

    local_today = now.astimezone(tz).date()
    # Window: yesterday's cross-midnight shifts may still be active; a weekly
    # schedule repeats every 7 days, so +10 days always reaches the next switch.
    day_from = local_today - timedelta(days=2)
    day_to = local_today + timedelta(days=10)

    intervals = weekly_intervals(shifts, tz, day_from, day_to)
    intervals += extension_intervals(extensions)

    covering = [iv for iv in intervals if iv.covers(now)]
    locked = not covering

    if covering:
        # An explicit temporary extension outranks the standing weekly shift.
        covering.sort(key=lambda iv: 0 if iv.kind == "temporary_extension" else 1)
        chosen = covering[0]
        basis = {
            "type": chosen.kind,
            "utc_start": fmt_utc(chosen.start),
            "utc_end": fmt_utc(chosen.end),
            **chosen.detail,
        }
    else:
        basis = {
            "type": "none",
            "detail": "outside weekly shifts and temporary extensions",
        }

    next_transition = None
    for start, end in merge_intervals(intervals):
        if start <= now < end:
            next_transition = end
            break
        if start > now:
            next_transition = start
            break

    workspaces = []
    for ws in config.get("workspaces", []):
        workspaces.append(
            {
                "workspace_id": ws["id"],
                "locked": locked,
                "basis": basis,
                "next_transition_utc": fmt_utc(next_transition) if next_transition else None,
                "next_state": ("locked" if not locked else "unlocked") if next_transition else None,
            }
        )

    return {
        "case_id": config.get("case_id", ""),
        "timezone": config["timezone"],
        "now_utc": fmt_utc(now),
        "workspaces": workspaces,
    }
