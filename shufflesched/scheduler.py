"""Speculative-shuffle scheduler.

Design summary
--------------
A job is a DAG of :class:`~shufflesched.model.Partition` objects.  A *source*
(map) partition has no upstreams; a *narrow* or *shuffle* partition consumes one
or more upstream partitions (narrow parent/child within a stage, or a shuffle
boundary between stages).

Every partition carries an ``epoch`` that only ever grows.  Launching an
attempt pins the epoch it belongs to.  A success callback may install a block
only when its attempt still belongs to the partition's *current* epoch.  Losing
a winning block reopens that partition and bumps its epoch, therefore any late
callback from the previous round is rejected rather than allowed to overwrite
the new block.

A committed result stores its *read set*: the upstream partitions the result
actually read and the winning :class:`~shufflesched.model.BlockId` it saw for
each of them.  A dependent is invalidated only when its own read set contains a
block that was actually lost; unrelated results are preserved untouched.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from typing import Any, Iterable, Mapping

from .errors import (
    DuplicateAttempt,
    DuplicatePartition,
    InvalidReadSet,
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
from .requests import (
    AttemptSpec,
    CompletionSpec,
    LossEvent,
    ReadSet,
    executor_id_of,
)

__all__ = ["ShuffleScheduler"]


class ShuffleScheduler:
    """Tracks shuffle attempts, winning blocks and dependent recomputation."""

    def __init__(self, *, allow_narrow: bool = True) -> None:
        self._partitions: "OrderedDict[str, Partition]" = OrderedDict()
        self._attempts: dict[str, Attempt] = {}
        self._applied_events: set[str] = set()
        self._allow_narrow = allow_narrow
        self._executor_blocks: dict[str, set[BlockId]] = {}
        self._sealed = False

    # ==================================================================
    # DAG construction
    # ==================================================================
    def add_partition(
        self,
        pid: str,
        upstreams: Iterable[str] = (),
        *,
        kind: str | None = None,
    ) -> Partition:
        if pid in self._partitions:
            raise DuplicatePartition(pid)
        ups = tuple(dict.fromkeys(upstreams))
        if kind is None:
            kind = "source" if not ups else "shuffle"
        if kind not in {"source", "narrow", "shuffle"}:
            raise ValueError(f"invalid partition kind: {kind!r}")
        if kind == "source" and ups:
            raise ValueError(f"source partition {pid!r} cannot have upstreams")
        if kind != "source" and not ups:
            raise ValueError(f"partition {pid!r} of kind {kind!r} needs upstreams")
        if kind == "narrow" and not self._allow_narrow:
            raise ValueError("narrow dependencies are disabled")
        part = Partition(pid=pid, upstreams=ups, kind=kind)
        self._partitions[pid] = part
        return part

    def seal(self) -> "ShuffleScheduler":
        """Validate the DAG.  Attempts may only be launched afterwards."""
        for pid, part in self._partitions.items():
            for up in part.upstreams:
                if up not in self._partitions:
                    raise UnknownDependency(pid, up)
        colour: dict[str, int] = {}

        def visit(node: str, stack: list[str]) -> None:
            state = colour.get(node, 0)
            if state == 1:
                raise ValueError(
                    "dependency cycle detected: " + " -> ".join(stack + [node])
                )
            if state == 2:
                return
            colour[node] = 1
            for up in self._partitions[node].upstreams:
                visit(up, stack + [node])
            colour[node] = 2

        for pid in self._partitions:
            visit(pid, [])
        self._sealed = True
        return self

    # ==================================================================
    # Introspection
    # ==================================================================
    def partitions(self) -> list[str]:
        return list(self._partitions)

    def partition(self, pid: str) -> Partition:
        try:
            return self._partitions[pid]
        except KeyError:
            raise UnknownPartition(pid) from None

    def winning_block(self, pid: str) -> Block | None:
        return self.partition(pid).winning

    def winning_id(self, pid: str) -> BlockId | None:
        block = self.partition(pid).winning
        return block.id if block else None

    def consumed(self, pid: str) -> dict[str, BlockId]:
        """Read set recorded by the winning result of ``pid``."""
        return dict(self.partition(pid).consumed)

    def committed(self, pid: str) -> bool:
        return self.partition(pid).winning is not None

    def recompute_count(self, pid: str) -> int:
        return self.partition(pid).recompute_count

    def attempts_for(self, pid: str) -> list[str]:
        return [a.attempt_id for a in self._attempts.values() if a.partition == pid]

    def pending_attempts(self, pid: str) -> list[str]:
        return [
            a.attempt_id
            for a in self._attempts.values()
            if a.partition == pid and not a.settled and not a.orphaned
        ]

    def attempt(self, attempt_id: str) -> Attempt:
        try:
            return self._attempts[attempt_id]
        except KeyError:
            raise UnknownAttempt(attempt_id) from None

    # ==================================================================
    # Readiness
    # ==================================================================
    def _check_reads_legal(self, part: Partition, reads: Mapping[str, BlockId]) -> None:
        """A result may only read partitions the DAG declares as its inputs."""
        if part.kind == "source":
            if reads:
                raise InvalidReadSet(part.pid, next(iter(reads)), "source reads no upstreams")
            return
        upstreams = set(part.upstreams)
        for up in reads:
            if up not in upstreams:
                raise InvalidReadSet(part.pid, up, "not a declared input of this partition")

    def _dependency_ready(self, part: Partition) -> bool:
        if part.kind == "source":
            return True
        return all(
            self._partitions[up].winning is not None for up in part.upstreams
        )

    def current_read_set(self, pid: str) -> dict[str, BlockId]:
        """Winning blocks a fresh launch of ``pid`` would read right now."""
        part = self.partition(pid)
        reads: dict[str, BlockId] = {}
        for up in part.upstreams:
            winner = self._partitions[up].winning
            if winner is None:
                raise InvalidReadSet(pid, up, "upstream has no winning block")
            reads[up] = winner.id
        return reads

    def is_ready(self, pid: str) -> bool:
        return self._dependency_ready(self.partition(pid))

    def ready_partitions(self) -> list[str]:
        return [
            pid
            for pid, part in self._partitions.items()
            if part.winning is None and self._dependency_ready(part)
        ]

    def launchable(self) -> list[str]:
        """Ready partitions with no attempt currently in flight."""
        return [
            pid for pid in self.ready_partitions()
            if not self._partitions[pid].in_flight
        ]

    # ==================================================================
    # Launching attempts
    # ==================================================================
    def request_attempt(self, *, partition: str, attempt_id: str,
                        executor: Any) -> Attempt:
        """Register an attempt (one of possibly two speculative attempts)."""
        executor_str = executor_id_of(executor)
        part = self.partition(partition)
        if not self._sealed:
            self.seal()
        if attempt_id in self._attempts:
            raise DuplicateAttempt(attempt_id)
        if not self._dependency_ready(part):
            raise InvalidReadSet(partition, "<launch>", "upstream has no winning block")
        if part.winning is not None:
            snapshot = dict(part.consumed)
        elif part.kind == "source":
            snapshot = {}
        else:
            snapshot = self.current_read_set(partition)
        attempt = Attempt(
            attempt_id=attempt_id,
            partition=partition,
            epoch=part.epoch,
            executor=executor_str,
            snapshot=snapshot,
        )
        self._attempts[attempt_id] = attempt
        part.in_flight.add(attempt_id)
        part.launch_count += 1
        if part.status == "pending":
            part.status = "running"
        return attempt

    def start_attempt(self, spec: AttemptSpec) -> StartResult:
        """Idempotent launch: re-requesting a known attempt reports its state."""
        if spec.attempt_id in self._attempts:
            attempt = self._attempts[spec.attempt_id]
            if attempt.partition != spec.partition:
                raise InvalidReadSet(
                    spec.partition,
                    spec.attempt_id,
                    "attempt id is bound to another partition",
                )
            if attempt.settled:
                return StartResult(StartStatus.ALREADY_DONE, spec.partition, spec.attempt_id)
            return StartResult(StartStatus.DUPLICATE_ATTEMPT, spec.partition, spec.attempt_id)
        if spec.partition not in self._partitions:
            return StartResult(StartStatus.UNKNOWN_PARTITION, spec.partition, spec.attempt_id)
        part = self.partition(spec.partition)
        if not self._dependency_ready(part):
            return StartResult(StartStatus.NOT_READY, spec.partition, spec.attempt_id)
        self.request_attempt(
            partition=spec.partition,
            attempt_id=spec.attempt_id,
            executor=spec.executor,
        )
        return StartResult(StartStatus.STARTED, spec.partition, spec.attempt_id)

    def start(self, partition: str, attempt_id: str, executor: Any) -> StartResult:
        return self.start_attempt(
            AttemptSpec(
                attempt_id=attempt_id,
                partition=partition,
                executor=executor_id_of(executor),
            )
        )

    def active_attempts(self, pid: str) -> list[str]:
        return sorted(self.partition(pid).in_flight)

    # ==================================================================
    # Completion handling
    # ==================================================================
    def complete(
        self,
        attempt_id: str,
        executor: Any | None = None,
        reads: Mapping[str, Any] | Iterable[Any] | None = None,
        payload: Any = None,
    ) -> Completion:
        return self.complete_attempt(
            CompletionSpec(
                attempt_id=attempt_id,
                executor=executor_id_of(executor) if executor is not None else "",
                reads=reads,
                payload=payload,
            )
        )

    def complete_attempt(self, spec: CompletionSpec) -> Completion:
        attempt = self._attempts.get(spec.attempt_id)
        if attempt is None:
            return Completion(
                CompletionStatus.REJECTED_UNKNOWN_ATTEMPT,
                "",
                spec.attempt_id,
                detail="unknown attempt",
            )
        part = self._partitions[attempt.partition]

        if spec.executor and spec.executor != attempt.executor:
            return Completion(
                CompletionStatus.REJECTED_UNKNOWN_ATTEMPT,
                attempt.partition,
                spec.attempt_id,
                detail="executor does not own this attempt",
            )

        if attempt.settled:
            return Completion(
                CompletionStatus.REJECTED_DUPLICATE,
                attempt.partition,
                spec.attempt_id,
                detail="attempt already settled",
            )

        # Epoch guard: a callback from an older round can never install.
        if attempt.epoch != part.epoch:
            self._settle(attempt)
            return Completion(
                CompletionStatus.REJECTED_STALE_EPOCH,
                attempt.partition,
                spec.attempt_id,
                detail=f"attempt epoch {attempt.epoch} != current {part.epoch}",
            )

        # Orphaned attempts belong to a superseded round even if the epoch
        # happens to line up; they no longer own a slot and cannot install.
        if attempt.orphaned:
            self._settle(attempt)
            return Completion(
                CompletionStatus.REJECTED_STALE_EPOCH,
                attempt.partition,
                spec.attempt_id,
                detail="attempt was orphaned by a recomputation",
            )

        # First success in the current epoch wins; a second success loses.
        if part.winning is not None and part.winning.epoch == part.epoch:
            self._settle(attempt)
            return Completion(
                CompletionStatus.REJECTED_ALREADY_WON,
                attempt.partition,
                spec.attempt_id,
                detail="another attempt already provided this epoch's block",
            )

        read_set = self._resolve_reads(attempt, spec.reads, part)
        if isinstance(read_set, Completion):
            self._settle(attempt)
            return read_set

        try:
            self._check_reads_legal(part, read_set)
        except InvalidReadSet as exc:
            self._settle(attempt)
            return Completion(
                CompletionStatus.REJECTED_INVALID_READ,
                attempt.partition,
                spec.attempt_id,
                detail=str(exc),
            )

        try:
            self._validate_reads_current(attempt.partition, read_set)
        except InvalidReadSet as exc:
            self._settle(attempt)
            return Completion(
                CompletionStatus.REJECTED_STALE_INPUTS,
                attempt.partition,
                spec.attempt_id,
                detail=str(exc),
            )

        block = Block(
            id=BlockId(attempt.partition, attempt.epoch, attempt.attempt_id),
            executor=attempt.executor,
            payload=spec.payload,
        )
        self._install(part, attempt, block, read_set)
        return Completion(
            CompletionStatus.ACCEPTED,
            attempt.partition,
            attempt.attempt_id,
            block=block,
        )

    def _resolve_reads(self, attempt: Attempt, reads: Any, part: Partition):
        """Return a BlockId mapping, or a rejecting Completion on bad input."""
        if reads is None:
            if part.kind == "source":
                return {}
            return dict(attempt.snapshot)
        normalised: dict[str, BlockId] = {}
        for up, value in ReadSet(reads).items():
            up = str(up)
            if isinstance(value, BlockId):
                normalised[up] = value
                continue
            if isinstance(value, str):
                other = self._attempts.get(value)
                if other is None or other.partition != up:
                    return Completion(
                        CompletionStatus.REJECTED_INVALID_READ,
                        attempt.partition,
                        attempt.attempt_id,
                        detail=f"cannot resolve read {value!r} for {up!r}",
                    )
                normalised[up] = BlockId(other.partition, other.epoch, other.attempt_id)
                continue
            return Completion(
                CompletionStatus.REJECTED_INVALID_READ,
                attempt.partition,
                attempt.attempt_id,
                detail=f"cannot interpret read value {value!r}",
            )
        # The winning block is one block for a partition, so a read set that
        # names the same upstream twice is malformed.
        return normalised

    def _validate_reads_current(
        self, pid: str, reads: Mapping[str, BlockId]
    ) -> None:
        for up, read_block in reads.items():
            upstream = self.partition(up)
            winner = upstream.winning
            if winner is None:
                raise InvalidReadSet(pid, up, "upstream has no winning block")
            if read_block != winner.id:
                raise InvalidReadSet(
                    pid,
                    up,
                    f"read {read_block} but winning block is {winner.id}",
                )

    def _settle(self, attempt: Attempt) -> None:
        attempt.settled = True
        self._partitions[attempt.partition].in_flight.discard(attempt.attempt_id)

    def _orphan(self, attempt: Attempt) -> None:
        """Release an attempt's slot while keeping it recognisably stale."""
        attempt.orphaned = True
        self._partitions[attempt.partition].in_flight.discard(attempt.attempt_id)

    def _install(
        self,
        part: Partition,
        attempt: Attempt,
        block: Block,
        reads: Mapping[str, BlockId],
    ) -> None:
        attempt.settled = True
        part.in_flight.discard(attempt.attempt_id)
        previous = part.winning
        part.winning = block
        part.consumed = dict(reads)
        part.status = "done"
        part.result_count += 1
        # A superseded block is no longer held anywhere; forgetting it keeps a
        # later executor-loss report from naming blocks that cannot be lost.
        if previous is not None:
            held = self._executor_blocks.get(previous.executor)
            if held is not None:
                held.discard(previous.id)
                if not held:
                    self._executor_blocks.pop(previous.executor, None)
        self._executor_blocks.setdefault(block.executor, set()).add(block.id)

    # ==================================================================
    # Loss handling
    # ==================================================================
    def _children(self, pid: str) -> list[str]:
        return [p.pid for p in self._partitions.values() if pid in p.upstreams]

    def _affected(self, lost: set[BlockId]) -> set[str]:
        """Committed partitions whose read set transitively touches a lost block."""
        affected: set[str] = set()
        queue: deque[str] = deque()

        for pid, part in self._partitions.items():
            if part.winning is not None and any(
                b in lost for b in part.consumed.values()
            ):
                affected.add(pid)
                queue.append(pid)

        while queue:
            pid = queue.popleft()
            for child in self._children(pid):
                if child in affected:
                    continue
                child_part = self._partitions[child]
                if child_part.winning is None:
                    continue
                # A committed child necessarily read its parent, but guard
                # against a genuinely unused dependency all the same.
                if pid in child_part.consumed:
                    affected.add(child)
                    queue.append(child)
        return affected

    def report_executor_lost(
        self, event_id: str, executor: Any, ts: int = 0
    ) -> LossReport:
        return self.report_loss(
            LossEvent(event_id=event_id, executor=executor_id_of(executor), ts=ts)
        )

    def report_loss(self, event: LossEvent) -> LossReport:
        if event.event_id in self._applied_events:
            return LossReport(
                LossStatus.DUPLICATE_EVENT,
                event.event_id,
                event.executor,
                [],
                [],
                [],
                [],
            )

        lost_blocks = sorted(self._executor_blocks.get(event.executor, set()))
        if not lost_blocks:
            # Record the event so a repeat is still recognised as a duplicate.
            self._applied_events.add(event.event_id)
            return LossReport(
                LossStatus.NO_LOSS,
                event.event_id,
                event.executor,
                [],
                [],
                [],
                [],
            )

        self._applied_events.add(event.event_id)
        lost_set = set(lost_blocks)

        # 1. Which committed results have to be invalidated.
        affected = self._affected(lost_set)

        # 2. Partitions whose winning block was itself lost must reopen.  A
        #    source partition owns a lost block but has an empty read set, so it
        #    is not covered by `_affected` and has to be added explicitly.
        owners = sorted(
            pid
            for pid, part in self._partitions.items()
            if part.winning is not None and part.winning.id in lost_set
        )

        invalidated_set = set(affected) | set(owners)
        preserved = sorted(
            pid
            for pid, part in self._partitions.items()
            if part.winning is not None and pid not in invalidated_set
        )

        # 3. Invalidate the transitive closure; bump epochs so late callbacks
        #    from before the loss cannot re-install the old block.  In-flight
        #    attempts are orphaned: they release their slot for the relaunch but
        #    are still recognised as stale if they report success later.
        invalidated = sorted(invalidated_set)
        for pid in invalidated:
            part = self._partitions[pid]
            part.winning = None
            part.consumed = {}
            part.epoch += 1
            part.status = "pending"
            for aid in list(part.in_flight):
                self._orphan(self._attempts[aid])

        # 4. Attempts still running on the dead executor can never report; free
        #    their slots on any partition they occupy.
        self._executor_blocks.pop(event.executor, None)
        for pid, part in self._partitions.items():
            for aid in list(part.in_flight):
                if self._attempts[aid].executor == event.executor:
                    self._orphan(self._attempts[aid])

        return LossReport(
            LossStatus.APPLIED,
            event.event_id,
            event.executor,
            lost_blocks,
            sorted(owners),
            invalidated,
            preserved,
        )

    # ==================================================================
    # Source convenience
    # ==================================================================
    def provide_source(
        self,
        partition: str,
        attempt_id: str,
        executor: Any,
        payload: Any = None,
    ) -> Completion:
        """Register a concrete map-output block for a source partition."""
        part = self.partition(partition)
        if part.kind != "source":
            raise ValueError(f"partition {partition!r} is not a source")
        if attempt_id not in self._attempts:
            self.request_attempt(
                partition=partition, attempt_id=attempt_id, executor=executor
            )
        return self.complete_attempt(
            CompletionSpec(
                attempt_id=attempt_id,
                executor=executor_id_of(executor),
                payload=payload,
            )
        )

    # ==================================================================
    # Rendering
    # ==================================================================
    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            pid: {
                "kind": part.kind,
                "epoch": part.epoch,
                "winning": str(part.winning.id) if part.winning else None,
                "consumed": {k: str(v) for k, v in part.consumed.items()},
                "status": part.status,
                "result_count": part.result_count,
                "in_flight": sorted(part.in_flight),
            }
            for pid, part in self._partitions.items()
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ShuffleScheduler partitions={len(self._partitions)}>"
