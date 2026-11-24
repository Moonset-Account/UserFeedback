"""Role-specific views: minimal reviewer portal and named dispatch report."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import UnknownReviewerError
from .models import AllocationResult


@dataclass(frozen=True, slots=True)
class ReviewerTaskView:
    """The only task field sent to a reviewer-facing frontend."""

    anonymous_case_number: str


def reviewer_tasks(
    result: AllocationResult, reviewer_id: str
) -> tuple[ReviewerTaskView, ...]:
    """Return only the authenticated reviewer's own anonymous task numbers.

    The caller is responsible for binding ``reviewer_id`` to an authenticated
    session; this function never returns named assignments or a co-reviewer.
    """

    if reviewer_id not in result.capacities:
        raise UnknownReviewerError(f"unknown reviewer_id: {reviewer_id}")

    tasks: list[ReviewerTaskView] = []
    for assignment in result.assignments:
        if reviewer_id not in assignment.reviewer_ids:
            continue
        index = assignment.reviewer_ids.index(reviewer_id)
        tasks.append(
            ReviewerTaskView(
                anonymous_case_number=assignment.anonymous_case_numbers[index]
            )
        )
    return tuple(tasks)


def reviewer_case_numbers(
    result: AllocationResult, reviewer_id: str
) -> tuple[str, ...]:
    """Convenience accessor for JSON/string-based frontends."""

    return tuple(task.anonymous_case_number for task in reviewer_tasks(result, reviewer_id))


def reviewer_task_payload(
    result: AllocationResult, reviewer_id: str
) -> list[dict[str, str]]:
    """JSON-serializable minimal payload for the reviewer frontend."""

    return [
        {"anonymous_case_number": task.anonymous_case_number}
        for task in reviewer_tasks(result, reviewer_id)
    ]


def dispatch_view(result: AllocationResult) -> dict[str, object]:
    """Named allocation and bottleneck report for authorized dispatch staff."""

    exhibit_by_id = {item.exhibit_id: item for item in result.exhibits}
    reviewer_by_id = {item.reviewer_id: item for item in result.reviewers}

    assignments: list[dict[str, object]] = []
    for assignment in result.assignments:
        item = assignment.to_dispatch_dict()
        exhibit = exhibit_by_id[assignment.exhibit_id]
        item["priority"] = exhibit.priority
        item["source_group"] = exhibit.source_group
        item["reviewer_details"] = [
            {
                "reviewer_id": reviewer_id,
                "name": reviewer_by_id[reviewer_id].name
                or reviewer_by_id[reviewer_id].reviewer_id,
                "lab": reviewer_by_id[reviewer_id].lab,
                "anonymous_case_number": assignment.anonymous_case_numbers[index],
            }
            for index, reviewer_id in enumerate(assignment.reviewer_ids)
        ]
        assignments.append(item)

    reviewer_loads: dict[str, dict[str, object]] = {}
    for reviewer_id, reviewer in reviewer_by_id.items():
        assigned = result.loads[reviewer_id]
        reviewer_loads[reviewer_id] = {
            "name": reviewer.name or reviewer.reviewer_id,
            "lab": reviewer.lab,
            "capacity": reviewer.capacity,
            "assigned": assigned,
            "remaining_capacity": reviewer.capacity - assigned,
            "active": reviewer.active,
        }

    anonymous_directory: list[dict[str, str]] = []
    for assignment in result.assignments:
        for index, reviewer_id in enumerate(assignment.reviewer_ids):
            anonymous_directory.append(
                {
                    "anonymous_case_number": assignment.anonymous_case_numbers[index],
                    "exhibit_id": assignment.exhibit_id,
                    "reviewer_id": reviewer_id,
                    "reviewer_name": reviewer_by_id[reviewer_id].name or reviewer_id,
                }
            )

    return {
        "coverage_count": result.coverage_count,
        "maximum_coverage": result.maximum_coverage,
        "coverage_exhibit_ids": list(result.coverage_exhibit_ids),
        "priority_direction": result.priority_direction,
        "tie_break": result.tie_break,
        "anonymous_scope": result.anonymous_scope,
        "assignments": assignments,
        "anonymous_directory": anonymous_directory,
        "reviewer_loads": reviewer_loads,
        "unassigned": [item.to_dict() for item in result.unassigned],
    }
