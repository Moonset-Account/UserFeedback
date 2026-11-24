"""Small-batch forensic exhibit double-review allocator.

Public API
----------
allocate:
    Compute the maximum-coverage allocation with priority tie-breaking.
reviewer_tasks / reviewer_case_numbers:
    Minimal frontend view for one authenticated reviewer.
dispatch_view:
    Named allocation and diagnostics for authorized dispatch staff.
"""

from __future__ import annotations

from .api import allocate
from .errors import AllocationError, UnknownReviewerError, ValidationError
from .models import (
    AllocationResult,
    Assignment,
    CandidatePairStatus,
    Exhibit,
    Reviewer,
    ReviewerQualificationGap,
    UnassignedReport,
)
from .views import (
    ReviewerTaskView,
    dispatch_view,
    reviewer_case_numbers,
    reviewer_task_payload,
    reviewer_tasks,
)

__all__ = [
    "allocate",
    "AllocationError",
    "AllocationResult",
    "Assignment",
    "CandidatePairStatus",
    "dispatch_view",
    "Exhibit",
    "Reviewer",
    "ReviewerQualificationGap",
    "ReviewerTaskView",
    "UnknownReviewerError",
    "UnassignedReport",
    "ValidationError",
    "reviewer_case_numbers",
    "reviewer_task_payload",
    "reviewer_tasks",
]

__version__ = "1.0.0"
