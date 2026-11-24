"""Public allocation API."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace

from .anonymization import anonymous_number
from .eligibility import build_unassigned_report, pair_options
from .errors import AllocationError, ValidationError
from .models import AllocationResult, Assignment, Exhibit, Reviewer
from .normalization import _string_set, normalize_inputs
from .solver import solve_optimal

_VALID_TIE_BREAKS = {"lexicographic", "priority_sum", "sum"}
_VALID_DIRECTIONS = {"larger", "smaller", "higher", "lower"}
_VALID_SCOPES = {"case", "review", "reviewer", "seat"}
_VALID_MODES = {"each", "collective"}


def _apply_recusal_maps(
    reviewers: tuple[Reviewer, ...],
    source_group_recusals: Mapping[str, Iterable[str]] | None,
    exhibit_recusals: Mapping[str, Iterable[str]] | None,
) -> tuple[Reviewer, ...]:
    """Merge dispatch-supplied group/exhibit recusal maps into reviewers."""

    if source_group_recusals is not None and not isinstance(
        source_group_recusals, Mapping
    ):
        raise ValidationError("source_group_recusals must be a mapping")
    if exhibit_recusals is not None and not isinstance(
        exhibit_recusals, Mapping
    ):
        raise ValidationError("exhibit_recusals must be a mapping")

    reviewer_ids = {reviewer.reviewer_id for reviewer in reviewers}
    extra_groups: dict[str, set[str]] = {
        reviewer_id: set() for reviewer_id in reviewer_ids
    }
    extra_exhibits: dict[str, set[str]] = {
        reviewer_id: set() for reviewer_id in reviewer_ids
    }

    for source_group, raw_ids in (source_group_recusals or {}).items():
        group = str(source_group).strip()
        if not group:
            raise ValidationError("source_group_recusals contains an empty group")
        for reviewer_id in _string_set(raw_ids, "source_group_recusals ids"):
            if reviewer_id not in reviewer_ids:
                raise ValidationError(
                    f"recusal references unknown reviewer_id: {reviewer_id}"
                )
            extra_groups[reviewer_id].add(group)

    for exhibit_id, raw_ids in (exhibit_recusals or {}).items():
        exhibit = str(exhibit_id).strip()
        if not exhibit:
            raise ValidationError("exhibit_recusals contains an empty exhibit")
        for reviewer_id in _string_set(raw_ids, "exhibit_recusals ids"):
            if reviewer_id not in reviewer_ids:
                raise ValidationError(
                    f"recusal references unknown reviewer_id: {reviewer_id}"
                )
            extra_exhibits[reviewer_id].add(exhibit)

    return tuple(
        replace(
            reviewer,
            exposed_source_groups=(
                reviewer.exposed_source_groups
                | frozenset(extra_groups[reviewer.reviewer_id])
            ),
            recused_exhibit_ids=(
                reviewer.recused_exhibit_ids
                | frozenset(extra_exhibits[reviewer.reviewer_id])
            ),
        )
        for reviewer in reviewers
    )


def allocate(
    exhibits: Iterable[Exhibit | Mapping[str, object]],
    reviewers: Iterable[Reviewer | Mapping[str, object]],
    *,
    tie_break: str = "lexicographic",
    priority_direction: str = "larger",
    anonymous_scope: str = "case",
    anonymous_secret: str | bytes | None = None,
    qualification_mode: str = "each",
    source_group_recusals: Mapping[str, Iterable[str]] | None = None,
    exhibit_recusals: Mapping[str, Iterable[str]] | None = None,
) -> AllocationResult:
    """Allocate two different-lab reviewers to the maximum number of exhibits.

    The returned object contains named IDs because it is intended for
    server-side/dispatch use.  Send reviewers only the result of
    :func:`forensic_allocator.reviewer_tasks`; send dispatch staff
    :func:`forensic_allocator.dispatch_view`.
    """

    if tie_break not in _VALID_TIE_BREAKS:
        raise ValidationError(f"unsupported tie_break: {tie_break}")
    if priority_direction not in _VALID_DIRECTIONS:
        raise ValidationError(
            "priority_direction must be 'larger' or 'smaller'"
        )
    if anonymous_scope not in _VALID_SCOPES:
        raise ValidationError("anonymous_scope must be 'case' or 'review'")
    if qualification_mode not in _VALID_MODES:
        raise ValidationError(
            "qualification_mode must be 'each' or 'collective'"
        )
    if anonymous_secret is not None and not (
        isinstance(anonymous_secret, (str, bytes)) and len(anonymous_secret) > 0
    ):
        raise ValidationError("anonymous_secret must be a non-empty string/bytes")

    normalized_tie_break = (
        "priority_sum" if tie_break in {"sum", "priority_sum"} else "lexicographic"
    )
    direction = (
        "smaller" if priority_direction in {"smaller", "lower"} else "larger"
    )
    scope = "review" if anonymous_scope in {"review", "reviewer", "seat"} else "case"

    exhibit_tuple, base_reviewer_tuple = normalize_inputs(exhibits, reviewers)
    reviewer_tuple = _apply_recusal_maps(
        base_reviewer_tuple,
        source_group_recusals,
        exhibit_recusals,
    )
    reviewer_ids = tuple(reviewer.reviewer_id for reviewer in reviewer_tuple)

    def priority_weight(exhibit: Exhibit) -> float:
        value = float(exhibit.priority)
        return value if direction == "larger" else -value

    ranked_entries = sorted(
        enumerate(exhibit_tuple),
        key=lambda entry: (-priority_weight(entry[1]), entry[0]),
    )
    ranked_exhibits = tuple(exhibit for _, exhibit in ranked_entries)
    priority_weights = tuple(
        priority_weight(exhibit) for exhibit in ranked_exhibits
    )
    options_by_rank = tuple(
        pair_options(exhibit, reviewer_tuple, qualification_mode)
        for exhibit in ranked_exhibits
    )

    signature, maximum_coverage = solve_optimal(
        ranked_exhibits,
        reviewer_tuple,
        options_by_rank,
        priority_weights=priority_weights,
        tie_break=normalized_tie_break,
    )

    selected_pairs: dict[str, tuple[int, int]] = {}
    options_by_exhibit: dict[str, tuple[tuple[int, int], ...]] = {}
    for rank, (original_index, exhibit) in enumerate(ranked_entries):
        options_by_exhibit[exhibit.exhibit_id] = options_by_rank[rank]
        option_index = signature[rank]
        if option_index >= 0:
            selected_pairs[exhibit.exhibit_id] = options_by_rank[rank][option_index]

    loads: dict[str, int] = {reviewer_id: 0 for reviewer_id in reviewer_ids}
    used_tokens: set[str] = set()
    assignments: list[Assignment] = []

    # Emit in the caller's exhibit order for predictable output.
    for exhibit in exhibit_tuple:
        pair = selected_pairs.get(exhibit.exhibit_id)
        if pair is None:
            continue
        left_index, right_index = pair
        left = reviewer_tuple[left_index]
        right = reviewer_tuple[right_index]

        if scope == "case":
            token = anonymous_number(
                "case-v1",
                exhibit.exhibit_id,
                secret=anonymous_secret,
                used=used_tokens,
            )
            tokens = (token, token)
        else:
            tokens = (
                anonymous_number(
                    "review-seat-v1",
                    exhibit.exhibit_id,
                    left.reviewer_id,
                    secret=anonymous_secret,
                    used=used_tokens,
                ),
                anonymous_number(
                    "review-seat-v1",
                    exhibit.exhibit_id,
                    right.reviewer_id,
                    secret=anonymous_secret,
                    used=used_tokens,
                ),
            )

        assignment = Assignment(
            exhibit_id=exhibit.exhibit_id,
            reviewer_ids=(left.reviewer_id, right.reviewer_id),
            reviewer_names=(
                left.name or left.reviewer_id,
                right.name or right.reviewer_id,
            ),
            labs=(left.lab, right.lab),
            anonymous_case_numbers=tokens,
        )
        assignments.append(assignment)
        loads[left.reviewer_id] += 1
        loads[right.reviewer_id] += 1

    capacities = {
        reviewer.reviewer_id: reviewer.capacity for reviewer in reviewer_tuple
    }

    # Defensive invariant check: the optimizer must never emit an invalid pair.
    for assignment in assignments:
        if assignment.labs[0] == assignment.labs[1]:
            raise AllocationError("internal error: same-lab assignment emitted")
        for reviewer_id in assignment.reviewer_ids:
            if loads[reviewer_id] > capacities[reviewer_id]:
                raise AllocationError(
                    f"internal error: capacity exceeded for {reviewer_id}"
                )

    unassigned = tuple(
        build_unassigned_report(
            exhibit,
            reviewer_tuple,
            options_by_exhibit[exhibit.exhibit_id],
            loads,
            qualification_mode,
        )
        for exhibit in exhibit_tuple
        if exhibit.exhibit_id not in selected_pairs
    )

    return AllocationResult(
        assignments=tuple(assignments),
        unassigned=unassigned,
        coverage_count=len(assignments),
        maximum_coverage=maximum_coverage,
        coverage_exhibit_ids=tuple(item.exhibit_id for item in assignments),
        exhibits=exhibit_tuple,
        reviewers=reviewer_tuple,
        loads=loads,
        capacities=capacities,
        anonymous_scope=scope,
        priority_direction=direction,
        tie_break=normalized_tie_break,
    )
