"""Speculative shuffle scheduling primitives.

The public entry point is :class:`~shufflesched.scheduler.ShuffleScheduler`,
which tracks speculative map attempts, the Shuffle block that wins a round, and
the dependent results that must be recomputed when a winning block is lost.
"""

from .errors import (
    DuplicateAttempt,
    DuplicatePartition,
    InvalidReadSet,
    SchedulerError,
    UnknownAttempt,
    UnknownDependency,
    UnknownPartition,
)
from .model import (
    Attempt,
    Block,
    BlockId,
    Completion,
    CompletionStatus,
    LossReport,
    LossStatus,
    Partition,
    StartResult,
    StartStatus,
)
from .requests import AttemptSpec, CompletionSpec, LossEvent, SourceWrite, executor_id_of
from .scheduler import ShuffleScheduler

__all__ = [
    "ShuffleScheduler",
    "Attempt",
    "Block",
    "BlockId",
    "Completion",
    "CompletionStatus",
    "LossReport",
    "LossStatus",
    "Partition",
    "StartResult",
    "StartStatus",
    "AttemptSpec",
    "CompletionSpec",
    "LossEvent",
    "SourceWrite",
    "executor_id_of",
    "SchedulerError",
    "UnknownPartition",
    "DuplicatePartition",
    "UnknownDependency",
    "DuplicateAttempt",
    "UnknownAttempt",
    "InvalidReadSet",
]

__version__ = "0.1.0"
