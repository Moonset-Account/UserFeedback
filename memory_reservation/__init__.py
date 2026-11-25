"""Physical-memory interval reservation toolkit for VM boot planning."""

from __future__ import annotations

from .model import (
    ConflictReport,
    Placement,
    ReservationOutcome,
    ReservationPlan,
    ReservationRequest,
    UnsatisfiableReservation,
    coerce_request,
    coerce_requests,
)
from .planner import (
    can_satisfy,
    canonical_candidate_starts,
    diagnose_request,
    iter_all_candidate_starts,
    plan_from_free_ranges,
    plan_reservations,
    solve_assignment,
)
from .ranges import (
    AddressRange,
    build_free_page_ranges,
    coerce_range,
    coerce_ranges,
    expand_reserved_to_pages,
    full_page_ranges,
    merge_ranges,
    subtract_ranges,
)
from .table import ReservationTable

ReservationConflict = UnsatisfiableReservation
IntervalReservation = ReservationTable
reserve_intervals = plan_reservations

__version__ = "1.0.0"

__all__ = [
    "AddressRange",
    "ConflictReport",
    "IntervalReservation",
    "Placement",
    "ReservationConflict",
    "ReservationOutcome",
    "ReservationPlan",
    "ReservationRequest",
    "ReservationTable",
    "UnsatisfiableReservation",
    "build_free_page_ranges",
    "can_satisfy",
    "canonical_candidate_starts",
    "coerce_range",
    "coerce_ranges",
    "coerce_request",
    "coerce_requests",
    "diagnose_request",
    "expand_reserved_to_pages",
    "full_page_ranges",
    "iter_all_candidate_starts",
    "merge_ranges",
    "plan_from_free_ranges",
    "plan_reservations",
    "reserve_intervals",
    "reserve_ranges",
    "solve_assignment",
    "subtract_ranges",
]


def reserve_ranges(
    available_ranges,
    reserved_ranges,
    page_size,
    requests,
    *,
    raise_on_conflict: bool = False,
):
    """Functional wrapper returning a JSON-ish dictionary.

    Success returns :meth:`ReservationPlan.to_dict`.  Conflict returns
    :meth:`ConflictReport.to_dict` by default; pass ``raise_on_conflict=True``
    to receive :class:`UnsatisfiableReservation` instead.
    """

    try:
        plan = plan_reservations(available_ranges, reserved_ranges, page_size, requests)
    except UnsatisfiableReservation as exc:
        if raise_on_conflict:
            raise
        return exc.report.to_dict()
    return plan.to_dict()
