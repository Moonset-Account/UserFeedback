"""Exact, transactional interval placement planner."""

from __future__ import annotations

from functools import lru_cache
from math import lcm
from typing import Any, Iterable

from .model import (
    ConflictReport,
    Placement,
    ReservationPlan,
    ReservationRequest,
    UnsatisfiableReservation,
    coerce_requests,
)
from .ranges import AddressRange, build_free_page_ranges, subtract_ranges


_INF = 10**100


def validate_page_size(page_size: Any) -> int:
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
        raise ValueError("page_size must be a positive integer")
    return page_size


def effective_alignment(request: ReservationRequest, page_size: int) -> int:
    """Alignment actually required of a page-frame start.

    A placement must satisfy both page alignment and the request's byte/page
    alignment.  Their least common multiple is therefore used.
    """

    alignment = page_size
    if request.alignment is not None:
        alignment = lcm(alignment, request.alignment)
    if request.alignment_pages is not None:
        alignment = lcm(alignment, request.alignment_pages * page_size)
    return alignment


def earliest_start(
    interval: AddressRange | tuple[int, int],
    request: ReservationRequest,
    page_size: int,
) -> int | None:
    """Return the earliest legal start for this request in one free interval."""

    if isinstance(interval, AddressRange):
        interval_start, interval_end = interval.start, interval.end
    else:
        interval_start, interval_end = interval

    alignment = effective_alignment(request, page_size)
    start = ((interval_start + alignment - 1) // alignment) * alignment
    end = start + request.pages * page_size

    limit = request.exclusive_address_limit
    if limit is not None:
        interval_end = min(interval_end, limit)

    if end <= interval_end:
        return start
    return None


def canonical_candidate_starts(
    request: ReservationRequest,
    free_ranges: Iterable[AddressRange | tuple[int, int]],
    page_size: int,
) -> tuple[int, ...]:
    """Return the earliest legal candidate in each free interval.

    Keeping only the earliest candidate per gap is complete for the global
    planner: a block that is the leftmost remaining block can always be
    shifted left to this candidate without hurting blocks to its right.
    """

    starts: list[int] = []
    for interval in free_ranges:
        start = earliest_start(interval, request, page_size)
        if start is not None:
            starts.append(start)
    return tuple(starts)


def iter_all_candidate_starts(
    request: ReservationRequest,
    free_ranges: Iterable[AddressRange],
    page_size: int,
):
    """Yield every aligned legal start.  Intended for small maps/tests."""

    alignment = effective_alignment(request, page_size)
    length = request.pages * page_size
    limit = request.exclusive_address_limit

    for interval in free_ranges:
        end_bound = interval.end
        if limit is not None:
            end_bound = min(end_bound, limit)
        start = ((interval.start + alignment - 1) // alignment) * alignment
        while start + length <= end_bound:
            yield start
            start += alignment


def _split_free_key(
    free_key: tuple[tuple[int, int], ...],
    index: int,
    allocated_start: int,
    allocated_end: int,
) -> tuple[tuple[int, int], ...]:
    interval_start, interval_end = free_key[index]
    replacement: list[tuple[int, int]] = []
    if interval_start < allocated_start:
        replacement.append((interval_start, allocated_start))
    if allocated_end < interval_end:
        replacement.append((allocated_end, interval_end))
    return free_key[:index] + tuple(replacement) + free_key[index + 1 :]


def solve_assignment(
    free_ranges: Iterable[AddressRange | tuple[int, int]],
    requests: Iterable[ReservationRequest],
    page_size: int,
) -> dict[int, int] | None:
    """Find a non-overlapping assignment, or return ``None``.

    The search branches over every request and every free gap as the next
    leftmost canonical placement.  This is exact rather than first-fit-only:
    trying a high/early region before a low-address-only device can fail and
    backtrack to the constrained placement.
    """

    page_size = validate_page_size(page_size)
    request_tuple = tuple(requests)
    free_key = tuple(
        (interval.start, interval.end) if isinstance(interval, AddressRange) else tuple(interval)
        for interval in free_ranges
    )

    @lru_cache(maxsize=None)
    def search(
        current_free: tuple[tuple[int, int], ...],
        remaining: tuple[int, ...],
    ) -> dict[int, int] | None:
        if not remaining:
            return {}

        free_pages = sum((end - start) // page_size for start, end in current_free)
        requested_pages = sum(request_tuple[index].pages for index in remaining)
        if requested_pages > free_pages:
            return None

        actions: list[tuple[int, int, int]] = []
        option_counts: dict[int, int] = {}

        for request_index in remaining:
            request = request_tuple[request_index]
            options = 0
            for interval_index, interval in enumerate(current_free):
                start = earliest_start(interval, request, page_size)
                if start is not None:
                    options += 1
                    actions.append((request_index, interval_index, start))
            if options == 0:
                return None
            option_counts[request_index] = options

        def action_key(action: tuple[int, int, int]):
            request_index, _interval_index, start = action
            request = request_tuple[request_index]
            limit = request.exclusive_address_limit
            return (
                start,
                option_counts[request_index],
                limit if limit is not None else _INF,
                -request.pages,
                request.name,
                action[1],
            )

        for request_index, interval_index, start in sorted(actions, key=action_key):
            interval_start, interval_end = current_free[interval_index]
            end = start + request_tuple[request_index].pages * page_size
            if start < interval_start or end > interval_end:
                continue  # Defensive; earliest_start already checked this.

            next_free = _split_free_key(current_free, interval_index, start, end)
            next_remaining = tuple(index for index in remaining if index != request_index)
            subassignment = search(next_free, next_remaining)
            if subassignment is not None:
                result = dict(subassignment)
                result[request_index] = start
                return result

        return None

    return search(free_key, tuple(range(len(request_tuple))))


def can_satisfy(
    requests: Iterable[Any],
    free_ranges: Iterable[AddressRange],
    page_size: int,
) -> bool:
    """Return whether the request set can be placed in the supplied free ranges."""

    request_tuple = coerce_requests(requests)
    return solve_assignment(free_ranges, request_tuple, page_size) is not None


def diagnose_request(
    request: ReservationRequest,
    free_ranges: Iterable[AddressRange],
    page_size: int,
) -> dict[str, Any]:
    """Build structured diagnostics for one request against current free memory."""

    free_tuple = tuple(free_ranges)
    alignment = effective_alignment(request, page_size)
    length = request.byte_length(page_size)
    limit = request.exclusive_address_limit
    starts = canonical_candidate_starts(request, free_tuple, page_size)
    max_free_pages = max(
        (interval.length // page_size for interval in free_tuple),
        default=0,
    )

    interval_facts = []
    for interval in free_tuple:
        candidate = earliest_start(interval, request, page_size)
        interval_facts.append(
            {
                "range": (interval.start, interval.end),
                "pages": interval.length // page_size,
                "earliest_candidate": candidate,
            }
        )

    if starts:
        summary = "单独可以放置；若整组失败，原因是并发争用、碎片化或对齐布局冲突"
    elif not free_tuple:
        summary = "没有完整、可用且未被保留范围覆盖的可分配页"
    else:
        explanations: list[str] = []
        if request.pages > max_free_pages:
            explanations.append(
                f"需要 {request.pages} 个连续页，但最大空闲段只有 {max_free_pages} 页"
            )

        capped = limit is not None
        has_memory_under_limit = False
        has_raw_length_fit = False
        has_aligned_length_fit = False

        for interval in free_tuple:
            if request.pages <= interval.length // page_size:
                has_raw_length_fit = True
            aligned_start = ((interval.start + alignment - 1) // alignment) * alignment
            aligned_end = aligned_start + length
            if aligned_end <= interval.end:
                has_aligned_length_fit = True
            if limit is not None and aligned_start < limit:
                has_memory_under_limit = True

        if capped and not has_memory_under_limit:
            explanations.append(
                f"可寻址上限过低：排他上限为 0x{limit:x}，其下没有对齐的可用起点"
            )
        elif capped and has_raw_length_fit and not starts:
            explanations.append(
                f"存在足够大的空闲段，但放在排他上限 0x{limit:x} 内后长度或对齐不满足"
            )

        if not has_aligned_length_fit and request.pages <= max_free_pages:
            explanations.append(f"对齐要求 0x{alignment:x} 使连续段没有合法对齐起点")

        if not explanations:
            explanations.append("没有同时满足连续页数、对齐和地址上限的合法起点")
        summary = "；".join(explanations)

    return {
        "name": request.name,
        "can_place_alone": bool(starts),
        "canonical_candidate_starts": starts,
        "requested_pages": request.pages,
        "requested_bytes": length,
        "effective_alignment": alignment,
        "exclusive_address_limit": limit,
        "max_free_run_pages": max_free_pages,
        "intervals": tuple(interval_facts),
        "summary": summary,
    }


def build_conflict_report(
    requests: Iterable[ReservationRequest],
    free_ranges: Iterable[AddressRange],
    page_size: int,
) -> ConflictReport:
    """Find a minimal unsatisfiable request subset and explain it."""

    request_tuple = tuple(requests)
    free_tuple = tuple(free_ranges)

    blockers = list(request_tuple)
    for candidate in request_tuple:
        trial = [request for request in blockers if request.name != candidate.name]
        if solve_assignment(free_tuple, trial, page_size) is None:
            blockers = trial

    blocker_names = tuple(request.name for request in blockers)

    incompatible_pairs: list[tuple[str, str]] = []
    for left_index, left in enumerate(request_tuple):
        for right in request_tuple[left_index + 1 :]:
            pair_assignment = solve_assignment(free_tuple, (left, right), page_size)
            if pair_assignment is None:
                incompatible_pairs.append((left.name, right.name))

    diagnostics: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    for request in blockers:
        info = diagnose_request(request, free_tuple, page_size)
        diagnostics[request.name] = info
        reasons[request.name] = info["summary"]

    if len(blocker_names) == 1:
        message = f"请求 {blocker_names[0]!r} 找不到满足连续页数、对齐和地址上限的落点"
    else:
        quoted = ", ".join(repr(name) for name in blocker_names)
        message = (
            f"请求 {quoted} 互相卡住：不存在能同时满足全部约束的区间安排"
            "（已给出一个最小不可满足集合；原预留状态保持不变）"
        )

    return ConflictReport(
        blockers=blocker_names,
        message=message,
        incompatible_pairs=tuple(incompatible_pairs),
        reasons=reasons,
        diagnostics=diagnostics,
    )


def plan_from_free_ranges(
    free_ranges: Iterable[AddressRange],
    page_size: Any,
    requests: Any,
    *,
    initial_free_pages: int | None = None,
) -> ReservationPlan:
    """Plan a group against already-constructed current free ranges."""

    page_size = validate_page_size(page_size)
    request_tuple = coerce_requests(requests)
    free_tuple = tuple(free_ranges)

    assignment = solve_assignment(free_tuple, request_tuple, page_size)
    if assignment is None:
        report = build_conflict_report(request_tuple, free_tuple, page_size)
        raise UnsatisfiableReservation(report)

    placements: list[Placement] = []
    reserved_intervals: list[AddressRange] = []
    for index, request in enumerate(request_tuple):
        start = assignment[index]
        alignment = effective_alignment(request, page_size)
        placement = Placement(
            name=request.name,
            start=start,
            pages=request.pages,
            page_size=page_size,
            alignment=alignment,
            address_limit=request.exclusive_address_limit,
        )
        placements.append(placement)
        reserved_intervals.append(placement.range)

    final_free = subtract_ranges(free_tuple, reserved_intervals)
    return ReservationPlan(
        placements=tuple(placements),
        free_ranges=final_free,
        page_size=page_size,
        initial_free_pages=initial_free_pages,
    )


def plan_reservations(
    available_ranges: Iterable[Any],
    reserved_ranges: Iterable[Any],
    page_size: int,
    requests: Any,
) -> ReservationPlan:
    """Build free pages from firmware ranges and atomically plan all requests."""

    page_size = validate_page_size(page_size)
    free_ranges = build_free_page_ranges(available_ranges, reserved_ranges, page_size)
    initial_free_pages = sum(interval.length // page_size for interval in free_ranges)
    return plan_from_free_ranges(
        free_ranges,
        page_size,
        requests,
        initial_free_pages=initial_free_pages,
    )
