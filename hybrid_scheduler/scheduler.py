"""High-level stateful hybrid scheduler and legal-boundary action derivation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .model import (
    ClusterState,
    Loan,
    Phase,
    TaskKind,
    TaskRuntime,
    TaskSpec,
    TaskStatus,
)
from .planner import (
    ActionGroup,
    PlannerConfig,
    ReclamationPlan,
    ReleaseAction,
    ResumeCandidate,
    ReturnGrant,
    plan_reclamation,
    plan_resume_order,
)
from .resources import ResourceVector, as_vector


def legal_boundary_candidates(
    spec: TaskSpec,
    current_phase_id: str | None = None,
    *,
    savepoint_required: bool = False,
) -> tuple[tuple[Phase, float], ...]:
    """Return all reachable legal boundaries and shortest subsequent duration.

    ``distance`` excludes the current phase's remaining time: a boundary equal
    to the current phase has distance zero.  The caller adds
    ``runtime.phase_remaining()`` to obtain a conservative wait.
    """
    current_id = current_phase_id or spec.phases[0].id
    successors: dict[str, list[str]] = {phase.id: [] for phase in spec.phases}
    for phase in spec.phases:
        for dependency in phase.depends_on:
            successors[dependency].append(phase.id)
    for key in successors:
        successors[key].sort()

    distances: dict[str, float] = {current_id: 0.0}
    unvisited = {phase.id for phase in spec.phases}
    while unvisited:
        node = min(
            (node for node in unvisited if node in distances),
            key=lambda n: (distances[n], n),
            default=None,
        )
        if node is None:
            break
        unvisited.remove(node)
        for nxt in successors[node]:
            candidate = distances[node] + spec.phase(nxt).duration
            if candidate < distances.get(nxt, float("inf")):
                distances[nxt] = candidate

    candidates = [
        (phase, distances[phase.id])
        for phase in spec.phases
        if phase.id in distances
        and phase.legal_boundary
        and (not savepoint_required or phase.savepoint_allowed)
    ]
    candidates.sort(key=lambda item: (item[1], item[0].id))
    return tuple(candidates)


def next_legal_boundary(
    spec: TaskSpec,
    current_phase_id: str | None = None,
    *,
    savepoint_required: bool = False,
) -> tuple[Phase, float]:
    """Return the nearest legal boundary and conservative time to reach it."""
    candidates = legal_boundary_candidates(
        spec, current_phase_id, savepoint_required=savepoint_required
    )
    if not candidates:
        needed = " with savepoint" if savepoint_required else ""
        raise ValueError(f"task {spec.id!r} has no reachable legal phase boundary{needed}")
    return candidates[0]


def _distribute_claim(
    grants: tuple[ReturnGrant, ...], claim: ResourceVector
) -> tuple[ReturnGrant, ...]:
    """Split a partial claim across the loans represented by ``grants``."""
    remaining = claim.as_dict()
    result: list[ReturnGrant] = []
    for grant in grants:
        available = grant.amount.as_dict()
        taken: dict[str, float | int] = {}
        for dimension, need in list(remaining.items()):
            if need <= 0:
                continue
            amount = min(need, available.get(dimension, 0))
            if amount > 0:
                taken[dimension] = amount
                remaining[dimension] = need - amount
        if taken:
            result.append(ReturnGrant(grant.owner_id, ResourceVector(taken), grant.loan_id))
    return tuple(result)


def _order_map(order: Sequence[str] | Mapping[str, int] | None) -> Mapping[str, int]:
    if order is None:
        return {}
    if isinstance(order, Mapping):
        return dict(order)
    return {task_id: index for index, task_id in enumerate(order)}


def _combined_event_order(
    task_id: str, maps: Sequence[Mapping[str, int]], default: int
) -> int:
    values = [mapping[task_id] for mapping in maps if task_id in mapping]
    return min(values) if values else default


@dataclass
class SchedulerTraceRecord:
    time: float
    kind: str
    description: str
    task_id: str | None = None
    plan: ReclamationPlan | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReclaimRequest:
    stream_id: str
    amount: ResourceVector | Mapping[str, Any] | None = None
    borrower_order: tuple[str, ...] | Mapping[str, int] | None = None


class HybridScheduler:
    """Stateful scheduler facade used by tests and discrete event logs."""

    def __init__(
        self,
        capacity: ResourceVector | Mapping[str, Any],
        *,
        now: float = 0.0,
        config: PlannerConfig | None = None,
    ) -> None:
        cap = as_vector(capacity)
        self.state = ClusterState(capacity=cap, available=cap, now=now)
        self.config = config or PlannerConfig()
        self.trace: list[SchedulerTraceRecord] = []
        self.plans: list[ReclamationPlan] = []
        self.deferred_resume: dict[str, ResumeCandidate] = {}

    # ------------------------------------------------------------------ setup
    def submit(
        self,
        spec: TaskSpec,
        *,
        at: float | None = None,
        start: bool = False,
        allocation: ResourceVector | Mapping[str, Any] | None = None,
        event_order: int = 0,
    ) -> TaskRuntime:
        if at is not None:
            self.state.now = at
        runtime = TaskRuntime(spec=spec, status=TaskStatus.PENDING, event_order=event_order)
        self.state.add_task(runtime)
        self._trace(self.state.now, "submit", f"提交{spec.kind.value}任务 {spec.id}", spec.id)
        if start:
            # ``allocation`` is passed to start(); do not also pre-set it on
            # the runtime or the same resources would be accounted twice.
            self.start(spec.id, at=at, allocation=allocation)
        elif allocation is not None:
            raise ValueError("allocation can only be supplied together with start=True")
        return runtime

    def start(
        self,
        task_id: str,
        *,
        at: float | None = None,
        allocation: ResourceVector | Mapping[str, Any] | None = None,
    ) -> TaskRuntime:
        if at is not None:
            self.state.now = at
        runtime = self.state.task(task_id)
        req = as_vector(allocation) if allocation is not None else runtime.current_phase.resources
        if not req.fits_in(self.state.available):
            runtime.status = TaskStatus.RUNNABLE
            self._trace(
                self.state.now,
                "start_blocked",
                f"任务 {task_id} 资源不足，保持可运行状态；需要 {req}，可用 {self.state.available}",
                task_id,
            )
            return runtime
        self.state.allocate(req)
        runtime.allocation = runtime.allocation + req
        runtime.status = TaskStatus.RUNNING
        self._trace(self.state.now, "start", f"启动任务 {task_id}，分配 {req}", task_id)
        return runtime

    def lend(
        self,
        owner_id: str,
        borrower_id: str,
        amount: ResourceVector | Mapping[str, Any],
        *,
        at: float | None = None,
    ) -> Loan:
        loan = self.state.lend(owner_id, borrower_id, amount, at=at)
        self._trace(
            self.state.now,
            "lend",
            f"流任务 {owner_id} 将闲置额度 {amount} 借给批任务 {borrower_id}",
            borrower_id,
            details={"loan_id": loan.loan_id},
        )
        return loan

    def _trace(
        self,
        time: float,
        kind: str,
        description: str,
        task_id: str | None = None,
        plan: ReclamationPlan | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.trace.append(
            SchedulerTraceRecord(
                time=time,
                kind=kind,
                description=description,
                task_id=task_id,
                plan=plan,
                details=details or {},
            )
        )

    # ------------------------------------------------------------- planning
    def build_action_groups(
        self,
        owner_id: str,
        claims: ResourceVector,
        *,
        borrower_order: Sequence[str] | Mapping[str, int] | None = None,
        full_reclaim: bool = True,
    ) -> list[ActionGroup]:
        loans = self.state.outstanding_loans_for_owner(owner_id)
        by_borrower: dict[str, list[Loan]] = {}
        for loan in loans:
            by_borrower.setdefault(loan.borrower_id, []).append(loan)

        order_map: Mapping[str, int]
        if borrower_order is None:
            order_map = {}
        elif isinstance(borrower_order, Mapping):
            order_map = borrower_order
        else:
            order_map = {task_id: index for index, task_id in enumerate(borrower_order)}

        groups: list[ActionGroup] = []
        for borrower_id, borrower_loans in sorted(by_borrower.items()):
            runtime = self.state.task(borrower_id)
            owed = ResourceVector()
            grants: list[ReturnGrant] = []
            for loan in borrower_loans:
                remaining = loan.remaining
                owed = owed + remaining
                grants.append(ReturnGrant(owner_id=owner_id, amount=remaining, loan_id=loan.loan_id))

            event_order = order_map.get(borrower_id, runtime.event_order)
            options = self._build_release_options(
                runtime,
                owed=owed,
                grants=tuple(grants),
                event_order=event_order,
                claim=claims,
            )
            groups.append(
                ActionGroup(
                    task_id=borrower_id,
                    options=tuple(options),
                    required=full_reclaim,
                )
            )
        return groups

    def build_optional_holder_groups(
        self, *, exclude: set[str] | None = None
    ) -> list[ActionGroup]:
        """Include other batch holders as optional buffer-release candidates."""
        excluded = exclude or set()
        groups: list[ActionGroup] = []
        for task_id in sorted(self.state.tasks):
            if task_id in excluded:
                continue
            runtime = self.state.task(task_id)
            if runtime.spec.kind != TaskKind.BATCH:
                continue
            if runtime.status == TaskStatus.COMPLETED or not runtime.allocation:
                continue
            all_loans = self.state.outstanding_loans_for_borrower(task_id)
            owed = ResourceVector()
            grants: list[ReturnGrant] = []
            for loan in all_loans:
                remaining = loan.remaining
                owed = owed + remaining
                grants.append(ReturnGrant(loan.owner_id, remaining, loan.loan_id))
            try:
                options = self._build_release_options(
                    runtime,
                    owed=owed,
                    grants=tuple(grants),
                    event_order=runtime.event_order + 10_000,
                    # A zero claim disables the partial-return shortcut.
                    claim=ResourceVector(),
                )
            except ValueError:
                # A holder without any legal release path simply cannot be
                # offered as a blocker-breaker.
                continue
            if options:
                groups.append(ActionGroup(task_id=task_id, options=tuple(options), required=False))
        return groups

    def _build_release_options(
        self,
        runtime: TaskRuntime,
        *,
        owed: ResourceVector,
        grants: tuple[ReturnGrant, ...],
        event_order: int,
        claim: ResourceVector,
    ) -> list[ReleaseAction]:
        spec = runtime.spec
        options: list[ReleaseAction] = []
        remaining_current = runtime.phase_remaining()
        retained_after_full_return = runtime.allocation - owed

        # Enumerate every reachable legal boundary.  The search/optimizer can
        # choose a later boundary if an earlier boundary's buffer is held in a
        # way that creates a cycle or misses a resource declaration.
        boundaries = legal_boundary_candidates(spec, runtime.current_phase_id)
        for boundary, future_cost in boundaries:
            advance = remaining_current + future_cost
            can_continue = (
                boundary.continue_resources is not None
                and boundary.continue_resources.fits_in(retained_after_full_return)
            )
            if can_continue:
                options.append(
                    ReleaseAction(
                        task_id=runtime.id,
                        action_id=f"{runtime.id}:continue:{boundary.id}",
                        release=owed,
                        buffer=boundary.checkpoint_buffer,
                        returns=grants,
                        boundary_phase=boundary.id,
                        advance_duration=advance,
                        checkpoint_duration=boundary.checkpoint_duration,
                        suspend=False,
                        checkpoint=False,
                        deadline=spec.deadline,
                        priority=spec.priority,
                        event_order=event_order,
                        description=f"批任务 {runtime.id} 在阶段 {boundary.id} 释放借用额度并继续",
                    )
                )

        # Recovery alternatives: insert a savepoint at any legal savepoint
        # boundary and suspend, releasing the task's whole allocation.
        savepoint_boundaries = legal_boundary_candidates(
            spec, runtime.current_phase_id, savepoint_required=True
        )
        all_grants: list[ReturnGrant] = []
        for loan in self.state.outstanding_loans_for_borrower(runtime.id):
            all_grants.append(ReturnGrant(loan.owner_id, loan.remaining, loan.loan_id))
        for sp_phase, sp_future in savepoint_boundaries:
            sp_advance = remaining_current + sp_future
            options.append(
                ReleaseAction(
                    task_id=runtime.id,
                    action_id=f"{runtime.id}:suspend:{sp_phase.id}",
                    release=runtime.allocation,
                    buffer=sp_phase.checkpoint_buffer,
                    returns=tuple(all_grants),
                    boundary_phase=sp_phase.id,
                    advance_duration=sp_advance,
                    checkpoint_duration=sp_phase.checkpoint_duration,
                    suspend=True,
                    checkpoint=True,
                    savepoint_id=f"{runtime.id}:{sp_phase.id}:sp",
                    resume_needs=runtime.allocation,
                    deadline=spec.deadline,
                    priority=spec.priority,
                    event_order=event_order,
                    description=f"批任务 {runtime.id} 在 {sp_phase.id} 插入保存点，释放全部资源并暂停",
                )
            )

        if not options:
            raise ValueError(f"batch task {runtime.id!r} has neither continuable boundary nor savepoint")

        # For a partial reclaim that this borrower can cover alone, add an
        # exact partial-return option at the earliest boundary where the task
        # can continue after the smaller release.
        if bool(claim) and claim != owed and claim.fits_in(owed):
            retained_after_partial = runtime.allocation - claim
            partial_boundary = next(
                (
                    (phase, future_cost)
                    for phase, future_cost in boundaries
                    if phase.continue_resources is not None
                    and phase.continue_resources.fits_in(retained_after_partial)
                ),
                None,
            )
            if partial_boundary is not None:
                boundary, future_cost = partial_boundary
                partial_grants = _distribute_claim(grants, claim)
                options.insert(
                    0,
                    ReleaseAction(
                        task_id=runtime.id,
                        action_id=f"{runtime.id}:partial:{boundary.id}",
                        release=claim,
                        buffer=boundary.checkpoint_buffer,
                        returns=partial_grants,
                        boundary_phase=boundary.id,
                        advance_duration=remaining_current + future_cost,
                        checkpoint_duration=boundary.checkpoint_duration,
                        suspend=False,
                        checkpoint=False,
                        deadline=spec.deadline,
                        priority=spec.priority,
                        event_order=event_order,
                        description=f"批任务 {runtime.id} 在 {boundary.id} 部分释放 {claim} 并继续",
                    ),
                )

        # Stable preference: non-suspending first, then lower buffer and
        # earlier boundary.  The global search still compares all alternatives.
        return sorted(
            options,
            key=lambda a: (
                a.suspend,
                tuple(sorted(a.buffer.as_dict().items())),
                a.advance_duration + a.checkpoint_duration,
                a.action_id or "",
            ),
        )

    def reclaim_many(
        self,
        requests: Sequence[ReclaimRequest | Mapping[str, Any]],
        *,
        at: float | None = None,
        apply: bool = True,
        borrower_order: Sequence[str] | Mapping[str, int] | None = None,
    ) -> ReclamationPlan:
        """Plan several simultaneous stream reclamations as one resource wave.

        Granting each stream independently can consume memory that another
        coordinated release chain needs as a checkpoint buffer.  Same-time
        reclamation requests therefore get a combined claim map and a single
        release/grant order.  Input permutations only affect deterministic tie
        breaking.
        """
        if at is not None:
            self.state.now = at
        parsed: list[ReclaimRequest] = []
        claims: dict[str, ResourceVector] = {}
        for raw in requests:
            req = raw if isinstance(raw, ReclaimRequest) else ReclaimRequest(**raw)
            owner = self.state.task(req.stream_id)
            if owner.spec.kind != TaskKind.STREAM:
                raise ValueError(f"reclaim owner {req.stream_id!r} must be a stream task")
            outstanding = self.state.outstanding_by_owner(req.stream_id)
            claim = outstanding if req.amount is None else as_vector(req.amount)
            if req.amount is not None and not claim.fits_in(outstanding):
                raise ValueError(
                    f"reclaim claim {claim} exceeds outstanding loans {outstanding} "
                    f"for {req.stream_id!r}"
                )
            parsed.append(req)
            claims[req.stream_id] = claim

        global_order = _order_map(borrower_order)
        borrower_loans: dict[str, list[Loan]] = {}
        order_maps: list[Mapping[str, int]] = [global_order] if global_order else []
        all_full = True
        for req in parsed:
            outstanding = self.state.outstanding_by_owner(req.stream_id)
            all_full &= claims[req.stream_id] == outstanding
            if req.borrower_order is not None:
                order_maps.append(_order_map(req.borrower_order))
            for loan in self.state.outstanding_loans_for_owner(req.stream_id):
                borrower_loans.setdefault(loan.borrower_id, []).append(loan)

        groups: list[ActionGroup] = []
        for borrower_id, loans in sorted(borrower_loans.items()):
            owed = ResourceVector()
            grants: list[ReturnGrant] = []
            for loan in loans:
                remaining = loan.remaining
                owed = owed + remaining
                grants.append(ReturnGrant(loan.owner_id, remaining, loan.loan_id))
            event_order = _combined_event_order(borrower_id, order_maps, self.state.task(borrower_id).event_order)
            try:
                options = self._build_release_options(
                    self.state.task(borrower_id),
                    owed=owed,
                    grants=tuple(grants),
                    event_order=event_order,
                    # Combined partial claims are resolved conservatively by
                    # coverage/search rather than one-claim shortcut.
                    claim=ResourceVector(),
                )
            except ValueError as exc:
                plan = ReclamationPlan(
                    feasible=False,
                    release_order=(),
                    steps=[],
                    selected_actions={},
                    claims=claims,
                    returned={},
                    available_before=self.state.available,
                    available_after_release=self.state.available,
                    available_after_grants=self.state.available,
                    resume=None,
                    suspended=(),
                    cycle=None,
                    deadline_violations=(),
                    reason=f"存在无法到达合法释放/保存点边界的借用者: {exc}",
                )
                self.plans.append(plan)
                self._trace(self.state.now, "reclaim_infeasible", plan.reason, borrower_id, plan=plan)
                return plan
            groups.append(ActionGroup(borrower_id, tuple(options), required=all_full))

        involved = set(borrower_loans)
        groups.extend(self.build_optional_holder_groups(exclude=involved))
        plan = plan_reclamation(
            available=self.state.available,
            groups=groups,
            claims=claims,
            capacity=self.state.capacity,
            now=self.state.now,
            config=self.config,
        )
        self.plans.append(plan)
        owner_text = ",".join(sorted(claims))
        self._trace(
            self.state.now,
            "reclaim_many",
            (
                f"同时回收 {owner_text}: "
                + (f"可行，释放顺序 {plan.release_order}" if plan.feasible else f"无解，{plan.reason}")
            ),
            owner_text,
            plan=plan,
            details={"owners": list(claims)},
        )
        if plan.feasible and apply:
            self.apply_plan(plan)
        return plan

    def reclaim(
        self,
        stream_id: str,
        *,
        at: float | None = None,
        amount: ResourceVector | Mapping[str, Any] | None = None,
        borrower_order: Sequence[str] | Mapping[str, int] | None = None,
        extra_groups: Sequence[ActionGroup] = (),
        apply: bool = True,
        resume_candidates: Sequence[Any] = (),
    ) -> ReclamationPlan:
        if at is not None:
            self.state.now = at
        owner = self.state.task(stream_id)
        if owner.spec.kind != TaskKind.STREAM:
            raise ValueError(f"reclaim owner {stream_id!r} must be a stream task")

        outstanding = self.state.outstanding_by_owner(stream_id)
        claim = outstanding if amount is None else as_vector(amount)
        if amount is not None and not claim.fits_in(outstanding):
            raise ValueError(
                f"reclaim claim {claim} exceeds outstanding loans {outstanding} for {stream_id!r}"
            )
        full_reclaim = amount is None or claim == outstanding

        try:
            groups = self.build_action_groups(
                stream_id,
                claim,
                borrower_order=borrower_order,
                full_reclaim=full_reclaim,
            )
            involved = {group.task_id for group in groups}
            groups.extend(self.build_optional_holder_groups(exclude=involved))
        except ValueError as exc:
            plan = ReclamationPlan(
                feasible=False,
                release_order=(),
                steps=[],
                selected_actions={},
                claims={stream_id: claim},
                returned={},
                available_before=self.state.available,
                available_after_release=self.state.available,
                available_after_grants=self.state.available,
                resume=None,
                suspended=(),
                cycle=None,
                deadline_violations=(),
                reason=f"存在无法到达合法释放/保存点边界的借用者: {exc}",
            )
            self.plans.append(plan)
            self._trace(self.state.now, "reclaim_infeasible", plan.reason, stream_id, plan=plan)
            return plan
        groups.extend(extra_groups)
        plan = plan_reclamation(
            available=self.state.available,
            groups=groups,
            claims={stream_id: claim},
            capacity=self.state.capacity,
            resume_candidates=resume_candidates,
            now=self.state.now,
            config=self.config,
        )
        self.plans.append(plan)
        self._trace(
            self.state.now,
            "reclaim",
            (
                f"流任务 {stream_id} 回收 {claim}: "
                + (f"可行，释放顺序 {plan.release_order}" if plan.feasible else f"无解，{plan.reason}")
            ),
            stream_id,
            plan=plan,
        )
        if plan.feasible and apply:
            self.apply_plan(plan)
        return plan

    # ------------------------------------------------------------- mutation
    def apply_plan(self, plan: ReclamationPlan) -> None:
        if not plan.feasible:
            raise ValueError("refusing to apply an infeasible reclamation plan")
        for step in plan.steps:
            if step.kind == "release" and step.action is not None:
                self._apply_release(step.action)
            elif step.kind == "grant" and step.task_id is not None:
                amount = ResourceVector(step.details.get("claim", {}))
                self.state.allocate(amount)
                stream = self.state.task(step.task_id)
                stream.allocation = stream.allocation + amount
                stream.status = TaskStatus.RUNNING
            elif step.kind == "resume" and step.task_id is not None:
                needs = ResourceVector(step.details.get("needs", {}))
                if needs.fits_in(self.state.available):
                    self.state.allocate(needs)
                    task = self.state.task(step.task_id)
                    task.allocation = task.allocation + needs
                    task.status = TaskStatus.RUNNING
                    self.deferred_resume.pop(task.id, None)
                else:
                    task = self.state.task(step.task_id)
                    task.status = TaskStatus.PAUSED
                    self.deferred_resume[task.id] = ResumeCandidate(
                        task_id=task.id,
                        needs=needs,
                        deadline=step.details.get("deadline"),
                        priority=int(step.details.get("priority", 0)),
                    )
        # Preserve deferred candidates reported by planner.
        if plan.resume is not None:
            for task_id in plan.resume.deferred:
                action = plan.selected_actions.get(task_id)
                if action is not None and action.resume_needs:
                    self.deferred_resume[task_id] = ResumeCandidate(
                        task_id=task_id,
                        needs=action.resume_needs,
                        deadline=action.deadline,
                        priority=action.priority,
                        event_order=action.event_order,
                    )

    def _apply_release(self, action: ReleaseAction) -> None:
        runtime = self.state.task(action.task_id)
        if not action.release.fits_in(runtime.allocation):
            raise ValueError(
                f"plan release {action.release} exceeds allocation {runtime.allocation} of {action.task_id}"
            )
        runtime.allocation = runtime.allocation - action.release
        self.state.release(action.release)

        for grant in action.returns:
            if grant.loan_id is None:
                continue
            loan = next((item for item in self.state.loans if item.loan_id == grant.loan_id), None)
            if loan is None:
                continue
            loan.returned = loan.returned + grant.amount
        if action.boundary_phase is not None:
            runtime.completed_phases.add(action.boundary_phase)
        if action.suspend:
            runtime.status = TaskStatus.PAUSED
            if action.savepoint_id and action.savepoint_id not in runtime.savepoints:
                runtime.savepoints.append(action.savepoint_id)
        else:
            runtime.status = TaskStatus.RUNNING

    def finish(self, task_id: str, *, at: float | None = None) -> None:
        if at is not None:
            self.state.now = at
        runtime = self.state.task(task_id)
        held = runtime.allocation
        if held:
            self.state.release(held)
        runtime.allocation = ResourceVector()
        runtime.status = TaskStatus.COMPLETED
        self._trace(self.state.now, "finish", f"任务 {task_id} 完成并释放 {held}", task_id)

    def try_resume_deferred(self, *, at: float | None = None) -> tuple[str, ...]:
        if at is not None:
            self.state.now = at
        candidates = list(self.deferred_resume.values())
        result = plan_resume_order(
            self.state.available,
            candidates,
            now=self.state.now,
            config=self.config,
        )
        for step in result.steps:
            needs = ResourceVector(step.details["needs"])
            self.state.allocate(needs)
            runtime = self.state.task(step.task_id or "")
            runtime.allocation = runtime.allocation + needs
            runtime.status = TaskStatus.RUNNING
            self.deferred_resume.pop(runtime.id, None)
            self._trace(self.state.now, "resume", step.description, runtime.id)
        return result.resumed
