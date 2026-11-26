"""Domain objects for the hybrid stream/batch scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .resources import ResourceVector, as_vector


class TaskKind(str, Enum):
    STREAM = "stream"
    BATCH = "batch"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNABLE = "runnable"
    RUNNING = "running"
    WAITING_BUFFER = "waiting_buffer"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class EventKind(str, Enum):
    SUBMIT = "submit"
    START = "start"
    LEND = "lend"
    RECLAIM = "reclaim"
    STREAM_FINISH = "stream_finish"
    BATCH_FINISH = "batch_finish"
    OFFER_IDLE_QUOTA = "offer_idle_quota"
    TICK = "tick"


@dataclass(frozen=True)
class Phase:
    """A task phase/node.

    ``depends_on`` contains IDs of phases that must complete first.  A phase
    with ``legal_boundary=True`` may release borrowed quota after it completes.
    ``savepoint_allowed`` means a resumable savepoint may be inserted there.
    """

    id: str
    duration: float = 0.0
    depends_on: tuple[str, ...] = ()
    resources: ResourceVector = field(default_factory=ResourceVector.zero)
    legal_boundary: bool = False
    savepoint_allowed: bool = False
    checkpoint_buffer: ResourceVector = field(default_factory=ResourceVector.zero)
    checkpoint_duration: float = 0.0
    # Resources required if the task releases quota but keeps running.  When
    # omitted, the scheduler assumes the phase needs its declared resources.
    continue_resources: ResourceVector | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("phase id must be non-empty")
        if self.duration < 0 or self.checkpoint_duration < 0:
            raise ValueError(f"phase {self.id!r} durations must be non-negative")
        object.__setattr__(self, "resources", as_vector(self.resources))
        object.__setattr__(self, "checkpoint_buffer", as_vector(self.checkpoint_buffer))
        if self.continue_resources is not None:
            object.__setattr__(self, "continue_resources", as_vector(self.continue_resources))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))


@dataclass(frozen=True)
class TaskSpec:
    id: str
    kind: TaskKind
    phases: tuple[Phase, ...]
    deadline: float | None = None
    priority: int = 0
    # Stream quota/entitlement.  For batch tasks this can describe a resource
    # cap, but loans always name the stream owner explicitly.
    quota: ResourceVector = field(default_factory=ResourceVector.zero)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("task id must be non-empty")
        kind = TaskKind(self.kind)
        object.__setattr__(self, "kind", kind)
        phases = tuple(self.phases)
        if not phases:
            raise ValueError(f"task {self.id!r} must declare at least one phase")
        ids = [phase.id for phase in phases]
        if len(ids) != len(set(ids)):
            raise ValueError(f"task {self.id!r} has duplicate phase ids: {ids}")
        phase_ids = set(ids)
        for phase in phases:
            missing = [dep for dep in phase.depends_on if dep not in phase_ids]
            if missing:
                raise ValueError(
                    f"phase {phase.id!r} in task {self.id!r} depends on unknown phases {missing}"
                )
        _assert_acyclic(self.id, phases)
        object.__setattr__(self, "phases", phases)
        object.__setattr__(self, "quota", as_vector(self.quota))
        if self.deadline is not None and self.deadline < 0:
            raise ValueError(f"task {self.id!r} deadline must be non-negative")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def phase(self, phase_id: str) -> Phase:
        for phase in self.phases:
            if phase.id == phase_id:
                return phase
        raise KeyError(f"task {self.id!r} has no phase {phase_id!r}")

    def boundary_phases(self, *, savepoint_required: bool = False) -> tuple[Phase, ...]:
        return tuple(
            phase
            for phase in self.phases
            if phase.legal_boundary and (not savepoint_required or phase.savepoint_allowed)
        )


def _assert_acyclic(task_id: str, phases: tuple[Phase, ...]) -> None:
    graph = {phase.id: tuple(phase.depends_on) for phase in phases}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        status = state.get(node, 0)
        if status == 1:
            cycle_start = stack.index(node)
            cycle = stack[cycle_start:] + [node]
            raise ValueError(f"task {task_id!r} has a phase dependency cycle: {' -> '.join(cycle)}")
        if status == 2:
            return
        state[node] = 1
        stack.append(node)
        for dependency in graph[node]:
            visit(dependency)
        stack.pop()
        state[node] = 2

    for phase in phases:
        visit(phase.id)


def topological_phase_ids(spec: TaskSpec) -> tuple[str, ...]:
    """Return a deterministic topological order of phase IDs."""
    dependencies = {phase.id: set(phase.depends_on) for phase in spec.phases}
    remaining = set(dependencies)
    done: set[str] = set()
    result: list[str] = []
    while remaining:
        ready = sorted(node for node in remaining if dependencies[node] <= done)
        if not ready:  # pragma: no cover - validated in TaskSpec
            raise ValueError(f"phase graph for task {spec.id!r} is cyclic")
        chosen = ready[0]
        result.append(chosen)
        remaining.remove(chosen)
        done.add(chosen)
    return tuple(result)


@dataclass
class TaskRuntime:
    spec: TaskSpec
    current_phase_id: str | None = None
    status: TaskStatus = TaskStatus.PENDING
    allocation: ResourceVector = field(default_factory=ResourceVector.zero)
    elapsed_in_phase: float = 0.0
    completed_phases: set[str] = field(default_factory=set)
    savepoints: list[str] = field(default_factory=list)
    event_order: int = 0

    def __post_init__(self) -> None:
        if self.current_phase_id is None:
            self.current_phase_id = self.spec.phases[0].id
        elif self.current_phase_id not in {p.id for p in self.spec.phases}:
            raise KeyError(f"task {self.spec.id!r} has no phase {self.current_phase_id!r}")
        self.allocation = as_vector(self.allocation)
        self.status = TaskStatus(self.status)

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def current_phase(self) -> Phase:
        assert self.current_phase_id is not None
        return self.spec.phase(self.current_phase_id)

    def phase_remaining(self) -> float:
        return max(0.0, self.current_phase.duration - self.elapsed_in_phase)


@dataclass
class Loan:
    owner_id: str
    borrower_id: str
    amount: ResourceVector
    loan_id: str
    created_at: float = 0.0
    returned: ResourceVector = field(default_factory=ResourceVector.zero)

    def __post_init__(self) -> None:
        self.amount = as_vector(self.amount)
        self.returned = as_vector(self.returned)

    @property
    def remaining(self) -> ResourceVector:
        return self.amount.subtract(self.returned, strict=False)

    @property
    def is_closed(self) -> bool:
        return not bool(self.remaining)


@dataclass
class ClusterState:
    capacity: ResourceVector
    available: ResourceVector
    tasks: dict[str, TaskRuntime] = field(default_factory=dict)
    loans: list[Loan] = field(default_factory=list)
    now: float = 0.0
    next_loan_seq: int = 1

    def __post_init__(self) -> None:
        self.capacity = as_vector(self.capacity)
        self.available = as_vector(self.available)
        if not self.available.fits_in(self.capacity):
            raise ValueError("available resources cannot exceed capacity")

    def task(self, task_id: str) -> TaskRuntime:
        try:
            return self.tasks[task_id]
        except KeyError as exc:
            raise KeyError(f"unknown task {task_id!r}") from exc

    def can_allocate(self, requirements: ResourceVector | Mapping[str, Any]) -> bool:
        return as_vector(requirements).fits_in(self.available)

    def allocate(self, requirements: ResourceVector | Mapping[str, Any]) -> None:
        req = as_vector(requirements)
        if not req.fits_in(self.available):
            raise ValueError(f"insufficient resources: need {req}, available {self.available}")
        self.available = self.available - req

    def release(self, resources: ResourceVector | Mapping[str, Any]) -> None:
        amount = as_vector(resources)
        new_available = self.available + amount
        if not new_available.fits_in(self.capacity):
            raise ValueError(
                f"release {amount} would exceed capacity {self.capacity}; available was {self.available}"
            )
        self.available = new_available

    def add_task(self, runtime: TaskRuntime) -> None:
        if runtime.id in self.tasks:
            raise ValueError(f"task {runtime.id!r} already exists")
        self.tasks[runtime.id] = runtime

    def lend(
        self,
        owner_id: str,
        borrower_id: str,
        amount: ResourceVector | Mapping[str, Any],
        *,
        at: float | None = None,
    ) -> Loan:
        if owner_id not in self.tasks or borrower_id not in self.tasks:
            raise KeyError("both stream owner and batch borrower must be submitted before lending")
        owner = self.tasks[owner_id]
        borrower = self.tasks[borrower_id]
        if owner.spec.kind != TaskKind.STREAM:
            raise ValueError(f"loan owner {owner_id!r} must be a stream task")
        if borrower.spec.kind != TaskKind.BATCH:
            raise ValueError(f"loan borrower {borrower_id!r} must be a batch task")
        vec = as_vector(amount)
        if not vec:
            raise ValueError("loan amount must be non-zero")
        if not vec.fits_in(owner.spec.quota) and bool(owner.spec.quota):
            # Quota is an entitlement, not a hard accounting pool.  This check
            # catches malformed synthetic inputs while allowing quota=0 in
            # compact tests that focus on resource vectors.
            raise ValueError(f"loan {vec} exceeds declared quota {owner.spec.quota} of {owner_id!r}")
        self.allocate(vec)
        borrower.allocation = borrower.allocation + vec
        loan = Loan(
            loan_id=f"loan-{self.next_loan_seq}",
            owner_id=owner_id,
            borrower_id=borrower_id,
            amount=vec,
            created_at=self.now if at is None else at,
        )
        self.next_loan_seq += 1
        self.loans.append(loan)
        return loan

    def outstanding_by_owner(self, owner_id: str) -> ResourceVector:
        total = ResourceVector()
        for loan in self.loans:
            if loan.owner_id == owner_id and not loan.is_closed:
                total = total + loan.remaining
        return total

    def outstanding_loans_for_owner(self, owner_id: str) -> tuple[Loan, ...]:
        return tuple(loan for loan in self.loans if loan.owner_id == owner_id and not loan.is_closed)

    def outstanding_loans_for_borrower(self, borrower_id: str) -> tuple[Loan, ...]:
        return tuple(loan for loan in self.loans if loan.borrower_id == borrower_id and not loan.is_closed)

    def snapshot(self) -> "ClusterState":
        """Return a shallow planning snapshot with copied mutable containers."""
        return ClusterState(
            capacity=self.capacity,
            available=self.available,
            tasks=dict(self.tasks),
            loans=list(self.loans),
            now=self.now,
            next_loan_seq=self.next_loan_seq,
        )
