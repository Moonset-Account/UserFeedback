"""Audit retention planner.

Given the original-source, time, occupancy and event relations of each log,
issue the evidence combination that must be retained for each filed incident
so the archive job can restore what operations looked like at the time.
"""

from .model import (
    CaseSpec,
    EvidenceChoice,
    EvidenceLink,
    EvidenceRequirement,
    LogRecord,
    RetentionCapacityError,
    RetentionInputError,
    RetentionPlan,
    RetentionRequest,
    RetainedLog,
    RetainedSource,
    UnmetEvidence,
    DEFAULT_REQUIREMENTS,
)
from .planner import build_plan

__version__ = "1.0.0"

__all__ = [
    "DEFAULT_REQUIREMENTS",
    "CaseSpec",
    "EvidenceChoice",
    "EvidenceLink",
    "EvidenceRequirement",
    "LogRecord",
    "RetentionCapacityError",
    "RetentionInputError",
    "RetentionPlan",
    "RetentionRequest",
    "RetainedLog",
    "RetainedSource",
    "UnmetEvidence",
    "build_plan",
]
