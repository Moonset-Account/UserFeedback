"""Stateful reservation table with all-or-nothing group updates."""

from __future__ import annotations

from typing import Any, Iterable

from .model import (
    ConflictReport,
    Placement,
    ReservationOutcome,
    ReservationPlan,
    ReservationRequest,
    UnsatisfiableReservation,
    coerce_requests,
)
from .planner import plan_from_free_ranges, validate_page_size
from .ranges import AddressRange, build_free_page_ranges, coerce_ranges, merge_ranges


class ReservationTable:
    """A mutable physical-memory reservation table.

    A failed :meth:`reserve_group` plans against an internal copy first, so the
    table's existing placements and free ranges remain unchanged.
    """

    def __init__(
        self,
        available_ranges: Iterable[Any] | None = None,
        reserved_ranges: Iterable[Any] | None = None,
        page_size: int = 4096,
        *,
        free_ranges: Iterable[AddressRange | tuple[int, int]] | None = None,
    ) -> None:
        self.page_size = validate_page_size(page_size)

        if free_ranges is not None:
            self._free_ranges = tuple(
                interval
                if isinstance(interval, AddressRange)
                else AddressRange(int(interval[0]), int(interval[1]))
                for interval in free_ranges
            )
        else:
            self._free_ranges = build_free_page_ranges(
                available_ranges or (),
                reserved_ranges or (),
                self.page_size,
            )

        self._initial_free_pages = sum(
            interval.length // self.page_size for interval in self._free_ranges
        )
        self._allocations: dict[str, Placement] = {}

    @classmethod
    def from_free_ranges(
        cls,
        free_ranges: Iterable[AddressRange | tuple[int, int]],
        page_size: int = 4096,
    ) -> "ReservationTable":
        return cls(page_size=page_size, free_ranges=free_ranges)

    @property
    def free_ranges(self) -> tuple[AddressRange, ...]:
        return self._free_ranges

    @property
    def remaining_ranges(self) -> tuple[AddressRange, ...]:
        return self._free_ranges

    @property
    def reservations(self) -> tuple[Placement, ...]:
        return tuple(self._allocations[name] for name in sorted(self._allocations))

    @property
    def placements(self) -> tuple[Placement, ...]:
        return self.reservations

    @property
    def allocation_addresses(self) -> dict[str, int]:
        return {name: placement.start for name, placement in self._allocations.items()}

    @property
    def start_addresses(self) -> dict[str, int]:
        return self.allocation_addresses

    @property
    def base_address(self) -> int | None:
        return min((placement.start for placement in self._allocations.values()), default=None)

    @property
    def allocated_pages(self) -> int:
        return sum(placement.pages for placement in self._allocations.values())

    @property
    def remaining_pages(self) -> int:
        return sum(interval.length // self.page_size for interval in self._free_ranges)

    @property
    def largest_contiguous_pages(self) -> int:
        if not self._free_ranges:
            return 0
        return max(interval.length // self.page_size for interval in self._free_ranges)

    def placement_for(self, name: str) -> Placement:
        return self._allocations[name]

    def reserve_group(self, requests: Any) -> ReservationPlan:
        """Atomically reserve every request and return the successful plan."""

        request_tuple = coerce_requests(requests)
        duplicates = tuple(
            request.name for request in request_tuple if request.name in self._allocations
        )
        if duplicates:
            report = ConflictReport(
                blockers=duplicates,
                message=(
                    "预留名称 "
                    + ", ".join(repr(name) for name in duplicates)
                    + " 已存在；未覆盖原预留"
                ),
                reasons={name: "duplicate reservation name" for name in duplicates},
            )
            raise UnsatisfiableReservation(report)

        free_pages_before = self.remaining_pages
        plan = plan_from_free_ranges(
            self._free_ranges,
            self.page_size,
            request_tuple,
            initial_free_pages=free_pages_before,
        )

        # Commit only after the complete exact plan has succeeded.
        self._free_ranges = plan.free_ranges
        for placement in plan.placements:
            self._allocations[placement.name] = placement
        return plan

    def try_reserve_group(self, requests: Any) -> ReservationOutcome:
        """Like :meth:`reserve_group`, but return an outcome instead of raising."""

        try:
            plan = self.reserve_group(requests)
        except UnsatisfiableReservation as exc:
            return ReservationOutcome(ok=False, conflict=exc.report)
        return ReservationOutcome(ok=True, plan=plan)

    def reserve(
        self,
        name: str,
        pages: int,
        alignment: int | None = None,
        max_address: int | None = None,
        *,
        alignment_pages: int | None = None,
        address_limit: int | None = None,
        address_bits: int | None = None,
    ) -> Placement:
        """Reserve one request and return its placement."""

        request = ReservationRequest(
            name=name,
            pages=pages,
            alignment=alignment,
            max_address=max_address,
            alignment_pages=alignment_pages,
            address_limit=address_limit,
            address_bits=address_bits,
        )
        return self.reserve_group([request]).placements[0]

    def release(self, name: str) -> Placement:
        """Release a previously reserved placement."""

        placement = self._allocations.pop(name)
        self._free_ranges = merge_ranges((*self._free_ranges, placement.range))
        return placement

    def snapshot(self) -> tuple[tuple[AddressRange, ...], dict[str, Placement]]:
        """Return an opaque restorable snapshot of table state."""

        return self._free_ranges, dict(self._allocations)

    def restore(self, snapshot: tuple[tuple[AddressRange, ...], dict[str, Placement]]) -> None:
        """Restore a snapshot produced by :meth:`snapshot`."""

        free_ranges, allocations = snapshot
        self._free_ranges = tuple(free_ranges)
        self._allocations = dict(allocations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_size": self.page_size,
            "base_address": self.base_address,
            "allocated_pages": self.allocated_pages,
            "remaining_pages": self.remaining_pages,
            "largest_contiguous_pages": self.largest_contiguous_pages,
            "free_ranges": [(r.start, r.end) for r in self._free_ranges],
            "allocations": {
                name: placement.to_dict() for name, placement in self._allocations.items()
            },
        }
