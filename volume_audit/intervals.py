"""Half-open numeric interval and coverage calculations.

All ranges in this package use the conventional ``[start, end)`` form: the
start coordinate is included and the end coordinate is excluded. Thus adjacent
ranges such as ``[0, 50)`` and ``[50, 100)`` cover the whole ``[0, 100)``
target. Explicit inclusive block/LBA fields are converted by the normalization
layer before they reach this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

Number = int | float

_START_KEYS = (
    "start",
    "start_offset",
    "start_lba",
    "start_block",
    "first_block",
    "offset",
    "lo",
    "from",
)
_END_KEYS = (
    "end",
    "end_offset",
    "end_boundary",
    "end_lba",
    "limit",
    "hi",
    "to",
)
_SIZE_KEYS = (
    "size",
    "length",
    "size_bytes",
    "byte_length",
    "range_length",
    "block_count",
    "blocks",
    "sector_count",
    "sectors",
    "count",
)
_LAST_INCLUSIVE_KEYS = ("last_block", "last_lba", "last_sector")


@dataclass(frozen=True)
class Interval:
    """A non-empty half-open range."""

    start: Number
    end: Number

    def __post_init__(self) -> None:
        if isinstance(self.start, bool) or isinstance(self.end, bool):
            raise ValueError("boolean values are not valid interval coordinates")
        if not isinstance(self.start, (int, float)) or not isinstance(self.end, (int, float)):
            raise TypeError("interval coordinates must be numeric")
        if self.end < self.start:
            raise ValueError(f"interval end {self.end!r} precedes start {self.start!r}")

    @property
    def length(self) -> Number:
        return self.end - self.start

    def to_dict(self) -> dict[str, Number]:
        return {"start": self.start, "end": self.end}

    def clips(self, target: "Interval") -> "Interval | None":
        start = max(self.start, target.start)
        end = min(self.end, target.end)
        if end <= start:
            return None
        return Interval(start, end)


def _fold_key(key: Any) -> str:
    return str(key).strip().lower().replace("_", "").replace("-", "").replace(" ", "")


def _first_key(mapping: Mapping[str, Any], keys: Sequence[str]) -> tuple[str | None, Any]:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return key, mapping[key]

    folded = {_fold_key(key): value for key, value in mapping.items()}
    for key in keys:
        folded_key = _fold_key(key)
        if folded_key in folded and folded[folded_key] is not None:
            return key, folded[folded_key]
    return None, None


def _nested_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def coerce_interval(value: Any, *, field_name: str = "interval") -> Interval:
    """Coerce a tuple, sequence, or mapping into an :class:`Interval`.

    Mapping inputs accept common offset/LBA/size aliases. A nested
    ``range``/``interval``/``extent`` object is also accepted.
    """

    if isinstance(value, Interval):
        return value

    if isinstance(value, Mapping):
        source = value
        nested = None
        for key in ("range", "interval", "extent", "encrypted_range", "coverage_range"):
            nested = _nested_mapping(value.get(key))
            if nested is not None:
                source = nested
                break

        _, start = _first_key(source, _START_KEYS)
        end_key, end = _first_key(source, _END_KEYS)
        _, size = _first_key(source, _SIZE_KEYS)
        _, last = _first_key(source, _LAST_INCLUSIVE_KEYS)

        # Some records put start/end under the parent and size in a nested map.
        if start is None:
            _, start = _first_key(value, _START_KEYS)
        if end is None and size is None and last is None:
            end_key, end = _first_key(value, _END_KEYS)
            _, size = _first_key(value, _SIZE_KEYS)
            _, last = _first_key(value, _LAST_INCLUSIVE_KEYS)

        if start is None:
            raise ValueError(f"{field_name} is missing a start coordinate: {value!r}")

        inclusive = bool(source.get("end_inclusive", source.get("inclusive_end", False)))
        if last is not None and end is None:
            end = last
            inclusive = True

        if end is not None:
            end_value = end
            if inclusive and isinstance(end_value, int):
                end_value = end_value + 1
        elif size is not None:
            end_value = start + size
        else:
            raise ValueError(f"{field_name} is missing end/size: {value!r}")

        try:
            start_num = start if isinstance(start, (int, float)) else float(start)
            end_num = end_value if isinstance(end_value, (int, float)) else float(end_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} has non-numeric boundaries: {value!r}") from exc

        return Interval(start_num, end_num)

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) == 2:
            return Interval(value[0], value[1])
        if len(value) == 3:
            # Common (start, length, unit) shape.
            return Interval(value[0], value[0] + value[1])

    raise TypeError(f"cannot interpret {field_name}: {value!r}")


def merge_intervals(intervals: Iterable[Any]) -> list[Interval]:
    """Merge overlapping or adjacent intervals.

    Empty intervals are discarded. Inputs may use any shape accepted by
    :func:`coerce_interval`.
    """

    normalized: list[Interval] = []
    for item in intervals:
        interval = coerce_interval(item)
        if interval.end > interval.start:
            normalized.append(interval)

    normalized.sort(key=lambda item: (item.start, item.end))
    merged: list[Interval] = []
    for interval in normalized:
        if not merged or interval.start > merged[-1].end:
            merged.append(Interval(interval.start, interval.end))
        elif interval.end > merged[-1].end:
            previous = merged[-1]
            merged[-1] = Interval(previous.start, interval.end)
    return merged


def subtract_covered(target: Any, covered: Iterable[Any]) -> list[Interval]:
    """Return the portions of ``target`` not covered by the supplied ranges."""

    target_interval = coerce_interval(target, field_name="target")
    if target_interval.end <= target_interval.start:
        return []

    clipped: list[Interval] = []
    for raw in covered:
        interval = coerce_interval(raw)
        piece = interval.clips(target_interval)
        if piece is not None:
            clipped.append(piece)

    merged = merge_intervals(clipped)
    gaps: list[Interval] = []
    cursor = target_interval.start

    for interval in merged:
        if interval.start > cursor:
            gaps.append(Interval(cursor, interval.start))
        if interval.end > cursor:
            cursor = interval.end
    if cursor < target_interval.end:
        gaps.append(Interval(cursor, target_interval.end))
    return gaps


def calculate_coverage(target: Any, confirmed_intervals: Iterable[Any]) -> dict[str, Any]:
    """Calculate exact covered and uncovered parts of a target range.

    The returned dictionary is JSON-friendly and includes both exact unit
    counts and a floating-point convenience ratio.
    """

    target_interval = coerce_interval(target, field_name="target")
    raw_intervals = list(confirmed_intervals)

    clipped: list[Interval] = []
    for raw in raw_intervals:
        interval = coerce_interval(raw)
        piece = interval.clips(target_interval)
        if piece is not None:
            clipped.append(piece)

    covered = merge_intervals(clipped)
    uncovered = subtract_covered(target_interval, covered)

    total = target_interval.length
    covered_units = sum(item.length for item in covered)
    uncovered_units = sum(item.length for item in uncovered)
    fully_covered = not uncovered and total > 0

    if total > 0:
        ratio = covered_units / total
        # Avoid output like 0.9999999999999999 when exact integer coverage is full.
        if fully_covered:
            ratio = 1.0
    else:
        ratio = 0.0

    return {
        "target": target_interval.to_dict(),
        "covered": [item.to_dict() for item in covered],
        "uncovered": [item.to_dict() for item in uncovered],
        "covered_ranges": [item.to_dict() for item in covered],
        "uncovered_ranges": [item.to_dict() for item in uncovered],
        "gaps": [item.to_dict() for item in uncovered],
        "covered_units": covered_units,
        "uncovered_units": uncovered_units,
        "total_units": total,
        "coverage_ratio": ratio,
        "fully_covered": fully_covered,
        "has_any_coverage": covered_units > 0,
    }
