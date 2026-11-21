"""Data model for the speculative shuffle scheduler.

The scheduler models a batch job as a DAG of *shuffle partitions*.  A map
partition (source) has no upstreams; every other partition consumes the
currently winning Shuffle block of the upstream partitions it actually read.

Every attempt is launched for a specific partition *epoch*.  A partition's
epoch only grows.  A success callback is only allowed to install a block if
the attempt still belongs to the partition's current epoch, which is what
makes stale rounds harmless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True, order=True)
class BlockId:
    """Identity of one Shuffle block: partition + epoch + attempt."""

    partition: str
    epoch: int
    attempt: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.partition}@e{self.epoch}#{self.attempt}"


@dataclass(frozen=True)
class Block:
    """A materialised Shuffle block held by an executor."""

    id: BlockId
    executor: str
    payload: Any = None

    @property
    def partition(self) -> str:
        return self.id.partition

    @property
    def epoch(self) -> int:
        return self.id.epoch

    @property
    def attempt(self) -> str:
        return self.id.attempt


class StartStatus(str, Enum):
    STARTED = "started"
    ALREADY_DONE = "already_done"
    NOT_READY = "not_ready"
    DUPLICATE_ATTEMPT = "duplicate_attempt"
    UNKNOWN_PARTITION = "unknown_partition"


class CompletionStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED_UNKNOWN_ATTEMPT = "rejected_unknown_attempt"
    REJECTED_STALE_EPOCH = "rejected_stale_epoch"
    REJECTED_ALREADY_WON = "rejected_already_won"
    REJECTED_DUPLICATE = "rejected_duplicate"
    REJECTED_STALE_INPUTS = "rejected_stale_inputs"
    REJECTED_INVALID_READ = "rejected_invalid_read"


class LossStatus(str, Enum):
    APPLIED = "applied"
    NO_LOSS = "no_loss"
    DUPLICATE_EVENT = "duplicate_event"
    STALE_BLOCK = "stale_block"
    UNKNOWN_BLOCK = "unknown_block"


@dataclass(frozen=True)
class StartResult:
    status: StartStatus
    partition: str
    attempt_id: str

    @property
    def started(self) -> bool:
        return self.status is StartStatus.STARTED


@dataclass(frozen=True)
class Completion:
    status: CompletionStatus
    partition: str
    attempt_id: str
    block: Block | None = None
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.status is CompletionStatus.ACCEPTED


@dataclass(frozen=True)
class LossReport:
    status: LossStatus
    event_id: str | None
    executor: str | None
    lost_blocks: list[BlockId]
    reopened: list[str]
    invalidated: list[str]
    preserved: list[str]

    @property
    def applied(self) -> bool:
        return self.status is LossStatus.APPLIED


@dataclass
class Attempt:
    """A single task attempt launched against one partition epoch."""

    attempt_id: str
    partition: str
    epoch: int
    executor: str
    # Winning upstream blocks observed when the attempt was launched.  This is
    # the default read set if the callback does not report one explicitly.
    snapshot: dict[str, BlockId] = field(default_factory=dict)
    settled: bool = False
    # Set when the partition was reopened underneath this attempt.  The attempt
    # no longer holds an executor slot, but a callback that arrives later is
    # still classified as belonging to a stale round instead of being silently
    # dropped.
    orphaned: bool = False


@dataclass
class Partition:
    """Scheduler-side state for one shuffle partition."""

    pid: str
    upstreams: tuple[str, ...]
    kind: str  # "source" | "narrow" | "shuffle"

    epoch: int = 0
    winning: Block | None = None
    status: str = "pending"  # "pending" | "running" | "done"

    # Upstream partitions actually read by the current committed result and the
    # winning block that was read from each of them.
    consumed: dict[str, BlockId] = field(default_factory=dict)

    in_flight: set[str] = field(default_factory=set)

    result_count: int = 0
    launch_count: int = 0

    @property
    def has_result(self) -> bool:
        return self.winning is not None

    @property
    def recompute_count(self) -> int:
        """How many times this partition had to recompute its result."""
        return max(0, self.result_count - 1)
