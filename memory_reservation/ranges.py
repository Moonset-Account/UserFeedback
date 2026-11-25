"""Address-range parsing and interval arithmetic for the reservation planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


_START_KEYS = ("start", "start_addr", "base", "base_addr", "address", "addr", "low")
_END_KEYS = ("end", "end_addr", "stop", "stop_addr", "high")
_LENGTH_KEYS = ("length", "byte_length", "size", "len")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, order=True)
class AddressRange:
    """A half-open physical address range ``[start, end)`` in bytes."""

    start: int
    end: int
    label: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not _is_int(self.start) or not _is_int(self.end):
            raise TypeError("AddressRange start and end must be integers")
        if self.start < 0 or self.end < 0:
            raise ValueError("addresses must be non-negative")
        if self.end < self.start:
            raise ValueError(f"range end {self.end} precedes start {self.start}")

    @property
    def base(self) -> int:
        return self.start

    @property
    def stop(self) -> int:
        return self.end

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def size(self) -> int:
        return self.length

    @classmethod
    def from_length(cls, start: int, length: int, label: str = "") -> "AddressRange":
        if not _is_int(length) or length < 0:
            raise ValueError("length must be a non-negative integer")
        return cls(start, start + length, label)

    def intersects(self, other: "AddressRange") -> bool:
        return self.start < other.end and other.start < self.end

    def intersection(self, other: "AddressRange") -> "AddressRange | None":
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return AddressRange(start, end) if start < end else None

    def contains_range(self, other: "AddressRange") -> bool:
        return self.start <= other.start and other.end <= self.end

    def page_count(self, page_size: int) -> int:
        if self.start % page_size or self.end % page_size:
            raise ValueError("page_count is only defined for page-aligned ranges")
        return self.length // page_size

    def __str__(self) -> str:  # pragma: no cover - diagnostic formatting
        label = f" {self.label}" if self.label else ""
        return f"[0x{self.start:x}, 0x{self.end:x}){label}"


def _first_mapping_value(data: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def coerce_range(value: Any, *, page_size: int | None = None) -> AddressRange:
    """Convert a range-like value to :class:`AddressRange`.

    Accepted forms:

    * :class:`AddressRange`
    * ``(start, end)`` or ``(start, end, label)`` -- half-open
    * mappings containing ``start``/``end``, ``base``/``length``, or firmware
      names such as ``base_addr``/``length``
    * mappings with ``base`` plus ``pages`` when ``page_size`` is supplied
    * arbitrary objects with ``start`` and ``end`` or ``base`` and ``length``
      attributes
    """

    if isinstance(value, AddressRange):
        return value

    if isinstance(value, Mapping):
        label = str(value.get("label") or value.get("name") or value.get("type") or "")
        start = _first_mapping_value(value, _START_KEYS)
        end = _first_mapping_value(value, _END_KEYS)
        length = _first_mapping_value(value, _LENGTH_KEYS)
        pages = value.get("pages")

        if start is None:
            raise ValueError(f"range mapping has no start/base key: {value!r}")

        if end is not None:
            return AddressRange(int(start), int(end), label)
        if length is not None:
            return AddressRange.from_length(int(start), int(length), label)
        if pages is not None:
            if page_size is None:
                raise ValueError("page_size is required to convert a pages-based range")
            return AddressRange.from_length(int(start), int(pages) * int(page_size), label)
        raise ValueError(f"range mapping has no end/length/pages: {value!r}")

    if isinstance(value, (tuple, list)):
        if len(value) == 2:
            start, end = value
            return AddressRange(int(start), int(end))
        if len(value) == 3:
            start, end, label = value
            return AddressRange(int(start), int(end), str(label))
        raise ValueError(f"range tuple must have 2 or 3 elements: {value!r}")

    start = getattr(value, "start", None)
    if start is None:
        start = getattr(value, "base", None)
    end = getattr(value, "end", None)
    if end is None:
        end = getattr(value, "stop", None)

    if start is not None and end is not None:
        return AddressRange(int(start), int(end))

    length = getattr(value, "length", None)
    if length is None:
        length = getattr(value, "size", None)
    if start is not None and length is not None:
        return AddressRange.from_length(int(start), int(length))

    raise TypeError(f"cannot interpret {value!r} as an address range")


def coerce_ranges(values: Iterable[Any] | None, *, page_size: int | None = None) -> tuple[AddressRange, ...]:
    if values is None:
        return ()
    return tuple(coerce_range(value, page_size=page_size) for value in values)


def merge_ranges(ranges: Iterable[AddressRange]) -> tuple[AddressRange, ...]:
    """Merge overlapping or adjacent ranges."""

    ordered = sorted(ranges)
    if not ordered:
        return ()

    merged: list[AddressRange] = [ordered[0]]
    for current in ordered[1:]:
        previous = merged[-1]
        if current.start <= previous.end:
            merged[-1] = AddressRange(
                previous.start,
                max(previous.end, current.end),
                previous.label or current.label,
            )
        else:
            merged.append(current)
    return tuple(merged)


def subtract_ranges(
    source: Iterable[AddressRange], cuts: Iterable[AddressRange]
) -> tuple[AddressRange, ...]:
    """Subtract all cut intervals from source intervals."""

    result = list(source)
    for cut in merge_ranges(cuts):
        reduced: list[AddressRange] = []
        for interval in result:
            if cut.end <= interval.start or cut.start >= interval.end:
                reduced.append(interval)
                continue

            left_end = min(cut.start, interval.end)
            if interval.start < left_end:
                reduced.append(AddressRange(interval.start, left_end))

            right_start = max(cut.end, interval.start)
            if right_start < interval.end:
                reduced.append(AddressRange(right_start, interval.end))
        result = list(merge_ranges(reduced))
    return tuple(merge_ranges(result))


def full_page_ranges(ranges: Iterable[AddressRange], page_size: int) -> tuple[AddressRange, ...]:
    """Return interiors consisting of complete page frames.

    An unaligned firmware boundary means the boundary page is not wholly
    described by that range, so it is not offered as allocatable memory.
    """

    if not _is_int(page_size) or page_size <= 0:
        raise ValueError("page_size must be a positive integer")

    complete: list[AddressRange] = []
    for value in merge_ranges(ranges):
        start = ((value.start + page_size - 1) // page_size) * page_size
        end = (value.end // page_size) * page_size
        if start < end:
            complete.append(AddressRange(start, end, value.label))
    return merge_ranges(complete)


def expand_reserved_to_pages(
    ranges: Iterable[AddressRange], page_size: int
) -> tuple[AddressRange, ...]:
    """Expand every non-empty reserved byte range to whole page frames.

    For example, with a 4 KiB page, a reserved range ``[0x3800, 0x3c00)``
    removes the complete page ``[0x3000, 0x4000)``.
    """

    if not _is_int(page_size) or page_size <= 0:
        raise ValueError("page_size must be a positive integer")

    expanded: list[AddressRange] = []
    for value in ranges:
        if value.end == value.start:
            continue
        start = (value.start // page_size) * page_size
        end = ((value.end + page_size - 1) // page_size) * page_size
        expanded.append(AddressRange(start, end, value.label))
    return merge_ranges(expanded)


def build_free_page_ranges(
    available_ranges: Iterable[Any],
    reserved_ranges: Iterable[Any],
    page_size: int,
) -> tuple[AddressRange, ...]:
    """Build page-aligned free ranges with reserved firmware ranges winning."""

    available = coerce_ranges(available_ranges, page_size=page_size)
    reserved = coerce_ranges(reserved_ranges, page_size=page_size)

    usable = full_page_ranges(merge_ranges(available), page_size)
    reserved_pages = expand_reserved_to_pages(reserved, page_size)
    free = subtract_ranges(usable, reserved_pages)

    # Defensive filter: every returned interval is page aligned and non-empty.
    return tuple(
        interval
        for interval in merge_ranges(free)
        if interval.start % page_size == 0
        and interval.end % page_size == 0
        and interval.length >= page_size
    )
