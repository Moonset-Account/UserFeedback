"""Request, result, and conflict data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .ranges import AddressRange


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class ReservationRequest:
    """A single contiguous-memory reservation requirement.

    ``max_address`` is inclusive.  For example, ``max_address=0xffffffff``
    means the last byte may be at ``0xffffffff``.  ``address_limit`` is the
    exclusive byte ceiling; a placement satisfies ``end <= address_limit``.
    """

    name: str
    pages: int
    alignment: int | None = None
    max_address: int | None = None
    alignment_pages: int | None = None
    address_limit: int | None = None
    address_bits: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("request name must be a non-empty string")
        if not _is_int(self.pages) or self.pages <= 0:
            raise ValueError(f"{self.name}: pages must be a positive integer")
        if self.alignment is not None:
            if not _is_int(self.alignment) or self.alignment <= 0:
                raise ValueError(f"{self.name}: alignment must be a positive integer")
        if self.alignment_pages is not None:
            if not _is_int(self.alignment_pages) or self.alignment_pages <= 0:
                raise ValueError(f"{self.name}: alignment_pages must be positive")

        for field_name, value in (
            ("max_address", self.max_address),
            ("address_limit", self.address_limit),
        ):
            if value is not None and (not _is_int(value) or value <= 0):
                raise ValueError(f"{self.name}: {field_name} must be a positive integer")
        if self.address_bits is not None:
            if not _is_int(self.address_bits) or self.address_bits <= 0:
                raise ValueError(f"{self.name}: address_bits must be a positive integer")

    @property
    def exclusive_address_limit(self) -> int | None:
        limits: list[int] = []
        if self.address_limit is not None:
            limits.append(self.address_limit)
        if self.max_address is not None:
            limits.append(self.max_address + 1)
        if self.address_bits is not None:
            limits.append(1 << self.address_bits)
        return min(limits) if limits else None

    def byte_length(self, page_size: int) -> int:
        return self.pages * page_size


@dataclass(frozen=True)
class Placement:
    """The concrete page-aligned address assigned to one request."""

    name: str
    start: int
    pages: int
    page_size: int
    alignment: int
    address_limit: int | None = None

    @property
    def end(self) -> int:
        return self.start + self.pages * self.page_size

    @property
    def base(self) -> int:
        return self.start

    @property
    def first_address(self) -> int:
        return self.start

    @property
    def length(self) -> int:
        return self.pages * self.page_size

    @property
    def size(self) -> int:
        return self.length

    @property
    def range(self) -> AddressRange:
        return AddressRange(self.start, self.end, self.name)

    @property
    def last_address(self) -> int:
        return self.end - 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "last_address": self.last_address,
            "pages": self.pages,
            "page_size": self.page_size,
            "alignment": self.alignment,
            "address_limit": self.address_limit,
        }


@dataclass(frozen=True)
class ReservationPlan:
    """A successful atomic group reservation and the resulting free ranges."""

    placements: tuple[Placement, ...]
    free_ranges: tuple[AddressRange, ...]
    page_size: int
    initial_free_pages: int | None = None

    @property
    def ok(self) -> bool:
        return True

    @property
    def placements_by_name(self) -> dict[str, Placement]:
        return {placement.name: placement for placement in self.placements}

    @property
    def start_addresses(self) -> dict[str, int]:
        """First/base address of every requested use."""

        return {placement.name: placement.start for placement in self.placements}

    @property
    def first_addresses(self) -> dict[str, int]:
        return self.start_addresses

    @property
    def addresses(self) -> dict[str, int]:
        return self.start_addresses

    @property
    def group_start(self) -> int | None:
        """Lowest assigned address in this group."""

        return min((placement.start for placement in self.placements), default=None)

    @property
    def base_address(self) -> int | None:
        return self.group_start

    @property
    def first_address(self) -> int | None:
        return self.group_start

    @property
    def group_base(self) -> int | None:
        return self.group_start

    @property
    def group_end(self) -> int | None:
        return max((placement.end for placement in self.placements), default=None)

    @property
    def allocated_pages(self) -> int:
        return sum(placement.pages for placement in self.placements)

    @property
    def remaining_pages(self) -> int:
        return sum(interval.length // self.page_size for interval in self.free_ranges)

    @property
    def remaining_page_count(self) -> int:
        return self.remaining_pages

    @property
    def remaining_ranges(self) -> tuple[AddressRange, ...]:
        return self.free_ranges

    @property
    def largest_contiguous_pages(self) -> int:
        if not self.free_ranges:
            return 0
        return max(interval.length // self.page_size for interval in self.free_ranges)

    def page_numbers_in_range(self, interval: AddressRange) -> range:
        first = interval.start // self.page_size
        last = interval.end // self.page_size
        return range(first, last)

    def remaining_page_numbers(self) -> tuple[int, ...]:
        """Materialize remaining page-frame numbers.

        Memory maps in tests are small.  For very large maps, iterate
        :attr:`remaining_ranges` instead of calling this method.
        """

        pages: list[int] = []
        for interval in self.free_ranges:
            pages.extend(self.page_numbers_in_range(interval))
        return tuple(pages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "group_start": self.group_start,
            "base_address": self.group_start,
            "start_addresses": self.start_addresses,
            "allocated_pages": self.allocated_pages,
            "remaining_pages": self.remaining_pages,
            "largest_contiguous_pages": self.largest_contiguous_pages,
            "free_ranges": [(r.start, r.end) for r in self.free_ranges],
            "placements": [p.to_dict() for p in self.placements],
        }


@dataclass(frozen=True)
class ConflictReport:
    """A minimal set of requests that cannot all be satisfied."""

    blockers: tuple[str, ...]
    message: str
    incompatible_pairs: tuple[tuple[str, str], ...] = ()
    reasons: Mapping[str, str] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return False

    @property
    def conflicting_requests(self) -> tuple[str, ...]:
        return self.blockers

    @property
    def minimal_blocking_set(self) -> tuple[str, ...]:
        return self.blockers

    def __iter__(self):
        return iter(self.blockers)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "blockers": list(self.blockers),
            "conflicting_requests": list(self.blockers),
            "incompatible_pairs": [list(pair) for pair in self.incompatible_pairs],
            "message": self.message,
            "reasons": dict(self.reasons),
            "diagnostics": dict(self.diagnostics),
        }


class ReservationError(Exception):
    """Base class for reservation API errors."""


class UnsatisfiableReservation(ReservationError):
    """Raised when no legal atomic placement exists."""

    def __init__(self, report: ConflictReport):
        self.report = report
        super().__init__(report.message)


# A concise alias used by some call sites.
ReservationConflict = UnsatisfiableReservation


@dataclass(frozen=True)
class ReservationOutcome:
    """Return type for non-throwing reservation attempts."""

    ok: bool
    plan: ReservationPlan | None = None
    conflict: ConflictReport | None = None

    @property
    def base_address(self) -> int | None:
        return self.plan.group_start if self.plan else None

    @property
    def group_start(self) -> int | None:
        return self.plan.group_start if self.plan else None

    @property
    def remaining_pages(self) -> int | None:
        return self.plan.remaining_pages if self.plan else None

    @property
    def blockers(self) -> tuple[str, ...]:
        return self.conflict.blockers if self.conflict else ()

    def raise_for_failure(self) -> "ReservationOutcome":
        if not self.ok and self.conflict is not None:
            raise UnsatisfiableReservation(self.conflict)
        return self


_REQUEST_NAME_KEYS = ("name", "purpose", "id", "device", "label")
_PAGES_KEYS = ("pages", "page_count", "required_pages", "count", "page_requirement")
_ALIGNMENT_KEYS = ("alignment", "align", "alignment_bytes", "byte_alignment")
_ALIGNMENT_PAGES_KEYS = ("alignment_pages", "align_pages", "page_alignment", "aligned_pages")
_LIMIT_KEYS = ("address_limit", "exclusive_address_limit", "address_end", "limit")
_MAX_ADDRESS_KEYS = (
    "max_address",
    "max_addr",
    "highest_address",
    "highest_addr",
    "addressable_max_address",
)
_ADDRESS_BITS_KEYS = ("address_bits", "addr_bits", "dma_bits", "bits")


def _mapping_first(data: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def coerce_request(value: Any, index: int = 0) -> ReservationRequest:
    """Convert a request mapping/tuple/object to :class:`ReservationRequest`."""

    if isinstance(value, ReservationRequest):
        return value

    default_name = f"request-{index + 1}"

    if isinstance(value, Mapping):
        name = _mapping_first(value, _REQUEST_NAME_KEYS) or default_name
        pages = _mapping_first(value, _PAGES_KEYS)
        if pages is None:
            raise ValueError(f"{name}: request is missing pages")
        return ReservationRequest(
            name=str(name),
            pages=int(pages),
            alignment=_optional_int(_mapping_first(value, _ALIGNMENT_KEYS)),
            max_address=_optional_int(_mapping_first(value, _MAX_ADDRESS_KEYS)),
            alignment_pages=_optional_int(_mapping_first(value, _ALIGNMENT_PAGES_KEYS)),
            address_limit=_optional_int(_mapping_first(value, _LIMIT_KEYS)),
            address_bits=_optional_int(_mapping_first(value, _ADDRESS_BITS_KEYS)),
        )

    if isinstance(value, (tuple, list)):
        if len(value) == 2:
            name, pages = value
            return ReservationRequest(str(name), int(pages))
        if len(value) == 3:
            name, pages, alignment = value
            return ReservationRequest(str(name), int(pages), alignment=_optional_int(alignment))
        if len(value) == 4:
            name, pages, alignment, max_address = value
            return ReservationRequest(
                str(name),
                int(pages),
                alignment=_optional_int(alignment),
                max_address=_optional_int(max_address),
            )
        raise ValueError(f"request tuple has unsupported length {len(value)}: {value!r}")

    name = getattr(value, "name", None) or getattr(value, "purpose", None) or default_name
    pages = getattr(value, "pages", None) or getattr(value, "page_count", None)
    if pages is None:
        raise TypeError(f"cannot interpret {value!r} as a reservation request")
    return ReservationRequest(
        name=str(name),
        pages=int(pages),
        alignment=_optional_int(getattr(value, "alignment", None)),
        max_address=_optional_int(getattr(value, "max_address", None)),
        alignment_pages=_optional_int(getattr(value, "alignment_pages", None)),
        address_limit=_optional_int(getattr(value, "address_limit", None)),
        address_bits=_optional_int(getattr(value, "address_bits", None)),
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def coerce_requests(values: Any) -> tuple[ReservationRequest, ...]:
    if isinstance(values, ReservationRequest):
        values = (values,)
    try:
        iterator = list(values)
    except TypeError as exc:
        raise TypeError("requests must be an iterable of request values") from exc

    requests = tuple(coerce_request(value, index) for index, value in enumerate(iterator))
    names = [request.name for request in requests]
    if len(names) != len(set(names)):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise ValueError(f"reservation request names must be unique: {duplicates}")
    return requests
