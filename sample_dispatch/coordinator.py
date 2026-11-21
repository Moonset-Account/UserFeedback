"""Deterministic global-batch sample dispatch for a training coordinator.

The coordinator owns three pieces of state:

1. a fixed per-epoch sample order (a sequence of sample ids),
2. a fixed global batch size, and
3. the current rank membership.

Global steps are cut from the sample order by the global batch size only, never
by the rank count.  Inside one global step, samples are handed to ranks
round-robin by their position in that global batch.  Step boundaries therefore
stay stable across membership changes, which is what makes "reassign from the
last committed position" reproduce exactly the single-rank sequential order.

Commit protocol
---------------
* A step may be fetched (``fetch``) ahead of the commit watermark; the number
  of in-flight steps is bounded by ``max_inflight_steps``.
* A step commits only after every member of the current generation has
  acknowledged it, including members whose shard is empty.
* Steps commit strictly in order.  A fully acknowledged later step waits until
  every earlier step has committed.
* Every membership change bumps the generation and revokes all uncommitted
  steps (including prefetched ones).  Acknowledgements carrying an older
  generation are rejected, as are acknowledgements from ranks that are no
  longer members.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

RankId = Any
SampleId = Any


class CoordinatorError(Exception):
    """Base exception for dispatch coordination failures."""


class InvalidConfigurationError(CoordinatorError):
    """Raised when the coordinator is configured with unusable parameters."""


class UnknownRankError(CoordinatorError):
    """Raised when an operation names a rank outside the current membership."""


@dataclass(frozen=True)
class ShardAssignment:
    """The slice of one global step that a single rank is asked to process."""

    epoch: int
    generation: int
    step_index: int
    rank: RankId
    participants: Tuple[RankId, ...]
    global_start: int
    global_end: int
    positions: Tuple[int, ...]
    sample_ids: Tuple[SampleId, ...]
    global_batch_size: int
    is_last_step: bool

    # ``global_batch_size`` is the actual length of this global batch: equal to
    # the configured batch size everywhere except possibly the final step.

    @property
    def is_empty(self) -> bool:
        """True when the rank has no sample in this step but must still ack."""
        return len(self.positions) == 0


@dataclass(frozen=True)
class CommittedStep:
    """Immutable record of one committed global step."""

    epoch: int
    step_index: int
    generation: int
    start_position: int
    end_position: int
    sample_ids: Tuple[SampleId, ...]
    shard_positions: Dict[RankId, Tuple[int, ...]]
    shard_samples: Dict[RankId, Tuple[SampleId, ...]]
    last_in_epoch: bool

    @property
    def position_count(self) -> int:
        return self.end_position - self.start_position


@dataclass(frozen=True)
class AckResult:
    """Outcome of one acknowledgement attempt."""

    accepted: bool
    reason: str
    rank: RankId
    generation: int
    step_index: int
    committed: Tuple[CommittedStep, ...] = ()
    epoch_finished: bool = False


@dataclass(frozen=True)
class MembershipChange:
    """Report returned by :meth:`TrainingCoordinator.set_membership`."""

    changed: bool
    epoch: int
    previous_ranks: Tuple[RankId, ...]
    ranks: Tuple[RankId, ...]
    generation: int
    revoked_steps: Tuple[int, ...]
    revoked_position_count: int
    resume_position: int
    next_step_index: int


@dataclass
class _PendingStep:
    """Mutable bookkeeping for a step that has not committed yet."""

    step_index: int
    generation: int
    start: int
    end: int
    shards: Dict[RankId, ShardAssignment]
    acks: Set[RankId] = field(default_factory=set)


def _normalize_ranks(ranks: Iterable[RankId]) -> Tuple[RankId, ...]:
    """Validate and canonically order rank ids.

    Canonical ordering makes round-robin placement reproducible across calls
    and across processes.  Rank ids must therefore be hashable and mutually
    comparable (integers and strings both work).
    """

    try:
        values = tuple(ranks)
    except TypeError as exc:  # pragma: no cover - defensive
        raise InvalidConfigurationError("ranks must be an iterable of rank ids") from exc
    if not values:
        raise InvalidConfigurationError("at least one rank is required")
    try:
        unique = set(values)
    except TypeError as exc:
        raise InvalidConfigurationError("rank ids must be hashable") from exc
    if len(unique) != len(values):
        raise InvalidConfigurationError("rank ids must be unique")
    try:
        return tuple(sorted(unique))
    except TypeError as exc:
        raise InvalidConfigurationError("rank ids must be mutually orderable") from exc


class TrainingCoordinator:
    """Assigns global-batch samples to ranks and commits steps in order.

    Parameters
    ----------
    sample_order:
        The deterministic sample order for one epoch.  Any sequence works; the
        coordinator never reorders it.
    batch_size:
        Fixed global batch size.  Only the final batch of an epoch may be
        smaller.
    ranks:
        Current rank membership.  Stored in canonical (sorted) order.
    max_inflight_steps:
        How many global steps may exist at once.  ``1`` means only the step at
        the commit watermark may be handed out; the default ``2`` lets a rank
        prefetch the next step while the current one is still being confirmed.
    epoch:
        Informational epoch number carried on assignments.
    """

    def __init__(
        self,
        sample_order: Sequence[SampleId],
        *,
        batch_size: int,
        ranks: Iterable[RankId],
        max_inflight_steps: int = 2,
        epoch: int = 0,
    ) -> None:
        try:
            order = tuple(sample_order)
        except TypeError as exc:  # pragma: no cover - defensive
            raise InvalidConfigurationError("sample_order must be a sequence") from exc
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise InvalidConfigurationError("batch_size must be a positive integer")
        if (
            isinstance(max_inflight_steps, bool)
            or not isinstance(max_inflight_steps, int)
            or max_inflight_steps <= 0
        ):
            raise InvalidConfigurationError("max_inflight_steps must be a positive integer")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise InvalidConfigurationError("epoch must be a non-negative integer")

        self._order: Tuple[SampleId, ...] = order
        self._batch_size = batch_size
        self._max_inflight_steps = max_inflight_steps
        self._ranks = _normalize_ranks(ranks)
        self._epoch = epoch
        self._generation = 1
        self._committed_steps = 0
        self._committed_positions = 0
        self._inflight: Dict[int, _PendingStep] = {}
        self._issued: Dict[RankId, Set[int]] = {rank: set() for rank in self._ranks}
        self._commit_log: List[CommittedStep] = []

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------
    @property
    def sample_order(self) -> Tuple[SampleId, ...]:
        return self._order

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def max_inflight_steps(self) -> int:
        return self._max_inflight_steps

    @property
    def ranks(self) -> Tuple[RankId, ...]:
        return self._ranks

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def step_count(self) -> int:
        if not self._order:
            return 0
        return (len(self._order) + self._batch_size - 1) // self._batch_size

    @property
    def committed_step_count(self) -> int:
        return self._committed_steps

    @property
    def consumed_positions(self) -> int:
        return self._committed_positions

    @property
    def total_positions(self) -> int:
        return len(self._order)

    @property
    def epoch_finished(self) -> bool:
        return self._committed_positions >= len(self._order)

    @property
    def commit_log(self) -> Tuple[CommittedStep, ...]:
        return tuple(self._commit_log)

    @property
    def pending_step_indices(self) -> Tuple[int, ...]:
        return tuple(sorted(self._inflight))

    def committed_samples(self) -> Tuple[SampleId, ...]:
        """Sample ids of every committed step, in commit order."""
        return tuple(sid for step in self._commit_log for sid in step.sample_ids)

    def step_bounds(self, step_index: int) -> Tuple[int, int]:
        """Return ``[start, end)`` positions of ``step_index`` in the epoch."""
        if (
            isinstance(step_index, bool)
            or not isinstance(step_index, int)
            or step_index < 0
            or step_index >= self.step_count
        ):
            raise IndexError(f"step {step_index!r} is outside the epoch")
        start = step_index * self._batch_size
        end = min(start + self._batch_size, len(self._order))
        return start, end

    def acks_for(self, step_index: int) -> frozenset:
        pending = self._inflight.get(step_index)
        if pending is None:
            return frozenset()
        return frozenset(pending.acks)

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------
    def set_membership(self, ranks: Iterable[RankId]) -> MembershipChange:
        """Replace the rank membership.

        Any step that has not committed is revoked, including fully prefetched
        and partially acknowledged ones.  The next fetch restarts at the last
        committed position under the new rank count.  The generation is bumped
        so that acknowledgements issued under the old membership are ignored.
        """
        normalized = _normalize_ranks(ranks)
        previous = self._ranks
        if normalized == previous:
            return MembershipChange(
                changed=False,
                epoch=self._epoch,
                previous_ranks=previous,
                ranks=normalized,
                generation=self._generation,
                revoked_steps=(),
                revoked_position_count=0,
                resume_position=self._committed_positions,
                next_step_index=self._committed_steps,
            )

        revoked = tuple(sorted(self._inflight))
        revoked_positions = sum(
            self._inflight[step_index].end - self._inflight[step_index].start
            for step_index in revoked
        )
        self._inflight.clear()
        self._issued = {rank: set() for rank in normalized}
        self._ranks = normalized
        self._generation += 1
        return MembershipChange(
            changed=True,
            epoch=self._epoch,
            previous_ranks=previous,
            ranks=normalized,
            generation=self._generation,
            revoked_steps=revoked,
            revoked_position_count=revoked_positions,
            resume_position=self._committed_positions,
            next_step_index=self._committed_steps,
        )

    def remove_rank(self, rank: RankId) -> MembershipChange:
        if rank not in self._ranks:
            raise UnknownRankError(f"rank {rank!r} is not in the current membership")
        return self.set_membership(tuple(r for r in self._ranks if r != rank))

    def add_rank(self, rank: RankId) -> MembershipChange:
        if rank in self._ranks:
            return self.set_membership(self._ranks)
        return self.set_membership(tuple(self._ranks) + (rank,))

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def fetch(self, rank: RankId, limit: int = 1) -> Tuple[ShardAssignment, ...]:
        """Hand new step assignments to ``rank``.

        Returns at most ``limit`` assignments whose step lies in the in-flight
        window and that were not handed to this rank in the current generation.
        An empty tuple is a valid answer: either the rank already holds every
        assignment in the window, or the epoch is finished.
        """
        if rank not in self._ranks:
            raise UnknownRankError(f"rank {rank!r} is not in the current membership")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise InvalidConfigurationError("limit must be a positive integer")
        if self.epoch_finished:
            return ()

        window_end = min(self._committed_steps + self._max_inflight_steps, self.step_count)
        issued: List[ShardAssignment] = []
        for step_index in range(self._committed_steps, window_end):
            if len(issued) >= limit:
                break
            if step_index in self._issued[rank]:
                continue
            pending = self._ensure_pending(step_index)
            assignment = pending.shards[rank]
            self._issued[rank].add(step_index)
            issued.append(assignment)
        return tuple(issued)

    def _ensure_pending(self, step_index: int) -> _PendingStep:
        pending = self._inflight.get(step_index)
        if pending is not None:
            return pending
        start, end = self.step_bounds(step_index)
        pending = _PendingStep(
            step_index=step_index,
            generation=self._generation,
            start=start,
            end=end,
            shards=self._build_shards(step_index, start, end),
        )
        self._inflight[step_index] = pending
        return pending

    def _build_shards(
        self, step_index: int, start: int, end: int
    ) -> Dict[RankId, ShardAssignment]:
        batch_length = end - start
        per_rank_positions: Dict[RankId, List[int]] = {rank: [] for rank in self._ranks}
        for offset in range(batch_length):
            rank = self._ranks[offset % len(self._ranks)]
            per_rank_positions[rank].append(start + offset)

        is_last = end == len(self._order)
        shards: Dict[RankId, ShardAssignment] = {}
        for rank in self._ranks:
            positions = tuple(per_rank_positions[rank])
            shards[rank] = ShardAssignment(
                epoch=self._epoch,
                generation=self._generation,
                step_index=step_index,
                rank=rank,
                participants=self._ranks,
                global_start=start,
                global_end=end,
                positions=positions,
                sample_ids=tuple(self._order[position] for position in positions),
                global_batch_size=batch_length,
                is_last_step=is_last,
            )
        return shards

    # ------------------------------------------------------------------
    # Commit protocol
    # ------------------------------------------------------------------
    def ack(self, rank: RankId, generation: int, step_index: int) -> AckResult:
        """Acknowledge that ``rank`` finished its shard of ``step_index``.

        Rejected acknowledgements never mutate state.  A rejected late ack from
        an old generation or a departed rank has no effect on the commit
        watermark or on the current generation's quorum.
        """
        if rank not in self._ranks:
            return self._ack_result(False, "unknown_rank", rank, generation, step_index)
        if generation != self._generation:
            return self._ack_result(False, "stale_generation", rank, generation, step_index)
        if isinstance(step_index, bool) or not isinstance(step_index, int) or step_index < 0:
            return self._ack_result(False, "invalid_step", rank, generation, step_index)
        if step_index < self._committed_steps:
            return self._ack_result(False, "already_committed", rank, generation, step_index)

        pending = self._inflight.get(step_index)
        if pending is None or pending.generation != self._generation:
            return self._ack_result(False, "not_assigned", rank, generation, step_index)
        if rank not in pending.shards:
            return self._ack_result(False, "not_participant", rank, generation, step_index)
        if step_index not in self._issued.get(rank, ()):
            # No ticket was handed out for this step in this generation, so the
            # acknowledgement cannot refer to work done for the current ticket.
            return self._ack_result(False, "not_issued", rank, generation, step_index)

        if rank in pending.acks:
            return self._ack_result(True, "duplicate_ack", rank, generation, step_index)

        pending.acks.add(rank)
        committed = self._drain_commits()
        return AckResult(
            accepted=True,
            reason="accepted",
            rank=rank,
            generation=generation,
            step_index=step_index,
            committed=committed,
            epoch_finished=self.epoch_finished,
        )

    def _ack_result(
        self,
        accepted: bool,
        reason: str,
        rank: RankId,
        generation: int,
        step_index: int,
    ) -> AckResult:
        return AckResult(
            accepted=accepted,
            reason=reason,
            rank=rank,
            generation=generation,
            step_index=step_index,
            committed=(),
            epoch_finished=self.epoch_finished,
        )

    def _drain_commits(self) -> Tuple[CommittedStep, ...]:
        committed: List[CommittedStep] = []
        while True:
            step_index = self._committed_steps
            pending = self._inflight.get(step_index)
            if pending is None:
                break
            if pending.start != self._committed_positions:
                raise CoordinatorError(
                    "internal error: pending step does not start at the commit watermark"
                )
            if not set(pending.shards).issubset(pending.acks):
                break

            del self._inflight[step_index]
            for issued in self._issued.values():
                issued.discard(step_index)
            self._committed_steps += 1
            self._committed_positions = pending.end
            record = CommittedStep(
                epoch=self._epoch,
                step_index=step_index,
                generation=pending.generation,
                start_position=pending.start,
                end_position=pending.end,
                sample_ids=self._order[pending.start : pending.end],
                shard_positions={
                    rank: assignment.positions
                    for rank, assignment in pending.shards.items()
                },
                shard_samples={
                    rank: assignment.sample_ids
                    for rank, assignment in pending.shards.items()
                },
                last_in_epoch=(pending.end == len(self._order)),
            )
            self._commit_log.append(record)
            committed.append(record)
        return tuple(committed)

    # ------------------------------------------------------------------
    # Epoch lifecycle
    # ------------------------------------------------------------------
    def start_next_epoch(
        self, sample_order: Optional[Sequence[SampleId]] = None
    ) -> int:
        """Reset for the next epoch after the current one fully committed."""
        if not self.epoch_finished:
            raise CoordinatorError(
                "cannot start the next epoch before the current epoch is fully committed"
            )
        if sample_order is not None:
            try:
                self._order = tuple(sample_order)
            except TypeError as exc:  # pragma: no cover - defensive
                raise InvalidConfigurationError("sample_order must be a sequence") from exc
        self._epoch += 1
        self._generation += 1
        self._committed_steps = 0
        self._committed_positions = 0
        self._commit_log = []
        self._inflight.clear()
        self._issued = {rank: set() for rank in self._ranks}
        return self._epoch
