"""Pure planning algorithms for quota reclamation and recovery.

The central resource-ordering observation is:

* a release action needs a temporary buffer vector ``buffer``;
* after the boundary/checkpoint it frees ``release`` resources;
* therefore an action is runnable when ``buffer <= available``;
* running it changes the logical available pool to ``available + release``.

Released quota is not granted to the reclaiming stream until the coordinated
release chain has obtained all buffers.  That lets one borrower's released
memory become the next borrower's checkpoint buffer while preserving the
eventual owner accounting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Mapping

from .resources import ResourceVector, as_vector


@dataclass(frozen=True)
class ReturnGrant:
    owner_id: str
    amount: ResourceVector
    loan_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", as_vector(self.amount))


@dataclass(frozen=True)
class ReleaseAction:
    """One legal way for a task to release resources at a phase boundary."""

    task_id: str
    release: ResourceVector = field(default_factory=ResourceVector.zero)
    buffer: ResourceVector = field(default_factory=ResourceVector.zero)
    returns: tuple[ReturnGrant, ...] = ()
    action_id: str | None = None
    boundary_phase: str | None = None
    advance_duration: float = 0.0
    checkpoint_duration: float = 0.0
    suspend: bool = False
    checkpoint: bool = False
    savepoint_id: str | None = None
    resume_needs: ResourceVector | None = None
    deadline: float | None = None
    priority: int = 0
    event_order: int = 0
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "release", as_vector(self.release))
        object.__setattr__(self, "buffer", as_vector(self.buffer))
        if self.resume_needs is not None:
            object.__setattr__(self, "resume_needs", as_vector(self.resume_needs))
        returns = tuple(self.returns)
        object.__setattr__(self, "returns", returns)
        if self.action_id is None:
            suffix = self.boundary_phase or ("suspend" if self.suspend else "release")
            object.__setattr__(self, "action_id", f"{self.task_id}:{suffix}")

    @property
    def suspension_count(self) -> int:
        return 1 if self.suspend else 0

    @property
    def checkpoint_count(self) -> int:
        return 1 if self.checkpoint or self.savepoint_id is not None else 0

    def returns_by_owner(self) -> dict[str, ResourceVector]:
        result: dict[str, ResourceVector] = {}
        for grant in self.returns:
            result[grant.owner_id] = result.get(grant.owner_id, ResourceVector()) + grant.amount
        return result


@dataclass(frozen=True)
class ActionGroup:
    """Alternative release actions for one task.

    A required group must choose one option.  An optional group may choose no
    action; this is used for another holder that only needs to be disrupted if
    it unlocks a required reclamation.
    """

    task_id: str
    options: tuple[ReleaseAction, ...]
    required: bool = False

    def __post_init__(self) -> None:
        opts = tuple(self.options)
        for option in opts:
            if option.task_id != self.task_id:
                raise ValueError(
                    f"action group {self.task_id!r} contains action for {option.task_id!r}"
                )
        if self.required and not opts:
            raise ValueError(f"required action group {self.task_id!r} has no options")
        object.__setattr__(self, "options", opts)


@dataclass(frozen=True)
class ResumeCandidate:
    task_id: str
    needs: ResourceVector
    deadline: float | None = None
    priority: int = 0
    event_order: int = 0
    resume_duration: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "needs", as_vector(self.needs))


@dataclass(frozen=True)
class PlannerConfig:
    enforce_deadlines: bool = False
    max_configurations: int = 4096
    max_resume_subsets: int = 4096
    # If true, equal choices retain simultaneous event/input order.  If false,
    # task id is used, making output independent of permuted event input.
    preserve_event_order: bool = True


@dataclass(frozen=True)
class WaitEdge:
    from_task: str
    to_task: str
    dimension: str
    shortfall: ResourceVector
    releasable: ResourceVector


@dataclass(frozen=True)
class ResourceCycle:
    nodes: tuple[str, ...]
    edges: tuple[WaitEdge, ...]
    available: ResourceVector
    shortfalls: Mapping[str, ResourceVector]
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": list(self.nodes),
            "message": self.message,
            "available": self.available.to_json(),
            "shortfalls": {k: v.to_json() for k, v in self.shortfalls.items()},
            "edges": [
                {
                    "from": edge.from_task,
                    "to": edge.to_task,
                    "dimension": edge.dimension,
                    "shortfall": edge.shortfall.to_json(),
                    "releasable": edge.releasable.to_json(),
                }
                for edge in self.edges
            ],
        }


@dataclass
class PlanStep:
    order: int
    kind: str
    task_id: str | None
    description: str
    action: ReleaseAction | None = None
    resources_before: ResourceVector | None = None
    resources_after: ResourceVector | None = None
    logical_time: float = 0.0
    finish_time: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "kind": self.kind,
            "task_id": self.task_id,
            "description": self.description,
            "logical_time": self.logical_time,
            "finish_time": self.finish_time,
            "resources_before": None if self.resources_before is None else self.resources_before.to_json(),
            "resources_after": None if self.resources_after is None else self.resources_after.to_json(),
            "details": self.details,
        }


@dataclass
class DeadlineViolation:
    task_id: str
    deadline: float
    estimated_time: float
    late_by: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "deadline": self.deadline,
            "estimated_time": self.estimated_time,
            "late_by": self.late_by,
            "reason": self.reason,
        }


@dataclass
class ReleaseOrderResult:
    feasible: bool
    order: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    available_after: ResourceVector
    cycle: ResourceCycle | None = None
    wait_edges: tuple[WaitEdge, ...] = ()
    deadline_violations: tuple[DeadlineViolation, ...] = ()


@dataclass
class ResumeOrderResult:
    resumed: tuple[str, ...]
    deferred: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    available_after: ResourceVector


@dataclass
class ReclamationPlan:
    feasible: bool
    release_order: tuple[str, ...]
    steps: tuple[PlanStep, ...]
    selected_actions: Mapping[str, ReleaseAction]
    claims: Mapping[str, ResourceVector]
    returned: Mapping[str, ResourceVector]
    available_before: ResourceVector
    available_after_release: ResourceVector
    available_after_grants: ResourceVector
    resume: ResumeOrderResult | None
    suspended: tuple[str, ...]
    cycle: ResourceCycle | None
    deadline_violations: tuple[DeadlineViolation, ...]
    reason: str = ""
    configurations_evaluated: int = 0

    @property
    def resume_order(self) -> tuple[str, ...]:
        return self.resume.resumed if self.resume else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "reason": self.reason,
            "release_order": list(self.release_order),
            "resume_order": list(self.resume_order),
            "suspended": list(self.suspended),
            "claims": {k: v.to_json() for k, v in self.claims.items()},
            "returned": {k: v.to_json() for k, v in self.returned.items()},
            "available_before": self.available_before.to_json(),
            "available_after_release": self.available_after_release.to_json(),
            "available_after_grants": self.available_after_grants.to_json(),
            "cycle": None if self.cycle is None else self.cycle.to_dict(),
            "deadline_violations": [item.to_dict() for item in self.deadline_violations],
            "steps": [step.to_dict() for step in self.steps],
            "configurations_evaluated": self.configurations_evaluated,
        }


def _tie_key(action: ReleaseAction, config: PlannerConfig) -> tuple[Any, ...]:
    # Non-suspending/non-checkpoint actions are preferred at an equal resource
    # state because they are less disruptive and never reduce available memory.
    event_part = action.event_order if config.preserve_event_order else 0
    return (
        action.suspend,
        action.checkpoint_count,
        action.deadline if action.deadline is not None else float("inf"),
        -action.priority,
        action.advance_duration + action.checkpoint_duration,
        event_part,
        action.task_id,
        action.action_id,
    )


def build_wait_edges(
    actions: list[ReleaseAction] | tuple[ReleaseAction, ...],
    available: ResourceVector,
) -> tuple[list[WaitEdge], dict[str, ResourceVector]]:
    """Build conservative wait-for edges among not-yet-runnable actions."""
    by_id = {action.task_id: action for action in actions}
    shortfalls: dict[str, ResourceVector] = {}
    edges: list[WaitEdge] = []
    for action in actions:
        shortfall = action.buffer.deficit(available)
        if not shortfall:
            continue
        shortfalls[action.task_id] = shortfall
        for dimension, shortage in shortfall.as_dict().items():
            if shortage <= 0:
                continue
            for other in actions:
                if other.task_id == action.task_id:
                    continue
                releasable_dimension = other.release.get(dimension)
                if releasable_dimension > 0:
                    edges.append(
                        WaitEdge(
                            from_task=action.task_id,
                            to_task=other.task_id,
                            dimension=dimension,
                            shortfall=shortfall,
                            releasable=ResourceVector({dimension: releasable_dimension}),
                        )
                    )
    # Deduplicate identical edges while preserving deterministic order.
    unique = {(e.from_task, e.to_task, e.dimension): e for e in edges}
    edge_list = [unique[key] for key in sorted(unique)]
    return edge_list, shortfalls


def _find_cycle_from_edges(edge_list: list[WaitEdge], nodes: list[str]) -> list[str] | None:
    graph: dict[str, list[str]] = {node: [] for node in nodes}
    for edge in edge_list:
        if edge.from_task in graph and edge.to_task in graph:
            graph[edge.from_task].append(edge.to_task)
    for node in graph:
        graph[node] = sorted(set(graph[node]))

    color = {node: 0 for node in nodes}
    path: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = 1
        path.append(node)
        for nxt in graph[node]:
            if color[nxt] == 0:
                found = dfs(nxt)
                if found is not None:
                    return found
            elif color[nxt] == 1:
                start = path.index(nxt)
                return path[start:] + [nxt]
        path.pop()
        color[node] = 2
        return None

    for node in sorted(nodes):
        if color[node] == 0:
            found = dfs(node)
            if found is not None:
                return _canonical_cycle(found)
    return None


def _canonical_cycle(cycle: list[str]) -> list[str]:
    """Rotate a cycle to start at its lexicographically smallest node."""
    if len(cycle) <= 1:
        return cycle
    nodes = cycle[:-1]
    start_index = min(range(len(nodes)), key=lambda i: nodes[i])
    rotated = nodes[start_index:] + nodes[:start_index]
    return rotated + [rotated[0]]


def make_resource_cycle(
    actions: list[ReleaseAction] | tuple[ReleaseAction, ...],
    available: ResourceVector,
) -> ResourceCycle | None:
    edge_list, shortfalls = build_wait_edges(actions, available)
    node_ids = sorted({a.task_id for a in actions})
    cycle_nodes = _find_cycle_from_edges(edge_list, node_ids)
    if cycle_nodes is None:
        return None
    cycle_set = set(cycle_nodes[:-1])
    cycle_edges = tuple(
        edge
        for edge in edge_list
        if edge.from_task in cycle_set and edge.to_task in cycle_set
    )
    compact_nodes = tuple(cycle_nodes)
    dimensions = sorted({edge.dimension for edge in cycle_edges})
    dim_text = ", ".join(dimensions)
    message = (
        "检测到无法打破的资源依赖环: "
        + " -> ".join(cycle_nodes)
        + f"；关键资源维度: {dim_text}。环上每个任务的边界缓冲都依赖另一个任务先释放，"
        "而当前没有可立即执行的合法释放动作。"
    )
    return ResourceCycle(
        nodes=compact_nodes,
        edges=cycle_edges,
        available=available,
        shortfalls={k: shortfalls[k] for k in sorted(shortfalls) if k in cycle_set},
        message=message,
    )


def find_release_order(
    available: ResourceVector | Mapping[str, Any],
    actions: Sequence[ReleaseAction],
    *,
    now: float = 0.0,
    config: PlannerConfig | None = None,
) -> ReleaseOrderResult:
    """Find a resource-feasible legal release order for fixed actions.

    Because every completed action only increases the logical available pool,
    running any currently runnable action cannot make another runnable action
    impossible.  Tie-breaking controls disruption and urgency.  If no action is
    runnable, the remaining wait-for graph contains the blocking cycle (or an
    external resource shortage).
    """
    cfg = config or PlannerConfig()
    avail = as_vector(available)
    remaining: dict[str, ReleaseAction] = {}
    for action in actions:
        if action.task_id in remaining:
            raise ValueError(f"duplicate selected release action for task {action.task_id!r}")
        remaining[action.task_id] = action

    steps: list[PlanStep] = []
    order: list[str] = []
    all_edges: list[WaitEdge] = []
    violations: list[DeadlineViolation] = []
    previous_release_time = now

    while remaining:
        runnable = [action for action in remaining.values() if action.buffer.fits_in(avail)]
        if not runnable:
            remaining_actions = tuple(remaining.values())
            edges, _ = build_wait_edges(remaining_actions, avail)
            cycle = make_resource_cycle(remaining_actions, avail)
            reason = "no runnable boundary release action"
            if cycle is None:
                reason = "resource shortfall has no releasable holder among supplied actions"
            return ReleaseOrderResult(
                feasible=False,
                order=tuple(order),
                steps=tuple(steps),
                available_after=avail,
                cycle=cycle,
                wait_edges=tuple(edges),
                deadline_violations=tuple(violations),
            )

        chosen = min(runnable, key=lambda a: _tie_key(a, cfg))
        before = avail
        boundary_ready = now + chosen.advance_duration
        checkpoint_start = max(boundary_ready, previous_release_time)
        finish_time = checkpoint_start + chosen.checkpoint_duration
        avail = avail + chosen.release
        step = PlanStep(
            order=len(steps) + 1,
            kind="release",
            task_id=chosen.task_id,
            description=chosen.description
            or (
                f"在合法阶段边界执行 {chosen.action_id}，"
                + ("保存点并暂停；" if chosen.suspend else "释放并继续；")
                + f"释放资源 {chosen.release}"
            ),
            action=chosen,
            resources_before=before,
            resources_after=avail,
            logical_time=checkpoint_start,
            finish_time=finish_time,
            details={
                "boundary_phase": chosen.boundary_phase,
                "savepoint_id": chosen.savepoint_id,
                "suspend": chosen.suspend,
                "buffer": chosen.buffer.to_json(),
                "returns": {g.owner_id: g.amount.to_json() for g in chosen.returns},
            },
        )
        steps.append(step)
        order.append(chosen.task_id)
        if chosen.deadline is not None and finish_time > chosen.deadline:
            violations.append(
                DeadlineViolation(
                    task_id=chosen.task_id,
                    deadline=chosen.deadline,
                    estimated_time=finish_time,
                    late_by=finish_time - chosen.deadline,
                    reason="conservative serialized boundary/checkpoint chain misses completion deadline",
                )
            )
        previous_release_time = finish_time
        del remaining[chosen.task_id]

    return ReleaseOrderResult(
        feasible=True,
        order=tuple(order),
        steps=tuple(steps),
        available_after=avail,
        wait_edges=tuple(all_edges),
        deadline_violations=tuple(violations),
    )


def _sum_returns(actions: Sequence[ReleaseAction]) -> dict[str, ResourceVector]:
    result: dict[str, ResourceVector] = {}
    for action in actions:
        for owner, amount in action.returns_by_owner().items():
            result[owner] = result.get(owner, ResourceVector()) + amount
    return result


def _claims_covered(
    returned: Mapping[str, ResourceVector], claims: Mapping[str, ResourceVector]
) -> bool:
    for owner, claim in claims.items():
        claim_vec = as_vector(claim)
        if not claim_vec.fits_in(returned.get(owner, ResourceVector())):
            return False
    return True


def _configuration_cost(actions: Sequence[ReleaseAction]) -> tuple[int, int, int, float]:
    suspended = sum(a.suspension_count for a in actions)
    checkpoints = sum(a.checkpoint_count for a in actions)
    # After disruption/count ties, prefer candidates that reach their legal
    # boundary and finish checkpointing earlier.  Advancement is parallel in a
    # real cluster, so this sum is a deterministic conservative tie cost.
    boundary_time = sum(a.advance_duration + a.checkpoint_duration for a in actions)
    return suspended, checkpoints, len(actions), boundary_time


def plan_resume_order(
    available: ResourceVector | Mapping[str, Any],
    candidates: Sequence[ResumeCandidate],
    *,
    now: float = 0.0,
    config: PlannerConfig | None = None,
) -> ResumeOrderResult:
    """Resume as many paused tasks as resources and deadlines allow.

    If all candidates fit together, EDF/priority/event tie-breaking determines
    the recovery order.  Otherwise a small exact subset search maximizes the
    number resumed and then deadline urgency.
    """
    cfg = config or PlannerConfig()
    avail = as_vector(available)
    candidates = tuple(candidates)
    total_needs = ResourceVector()
    for candidate in candidates:
        total_needs = total_needs + candidate.needs

    chosen_subset: tuple[ResumeCandidate, ...]
    deferred: tuple[str, ...]

    if total_needs.fits_in(avail):
        chosen_subset = candidates
        deferred = ()
    else:
        best: tuple[tuple[int, float, int, tuple[ResumeCandidate, ...]], tuple[ResumeCandidate, ...] | None] = (
            (-1, 0.0, 0, ()),
            None,
        )
        evaluated = 0
        n = len(candidates)
        # Exact bounded search; synthetic schedulers in tests are small.
        for mask in range(1 << n):
            evaluated += 1
            if evaluated > cfg.max_resume_subsets:
                break
            subset = tuple(candidates[i] for i in range(n) if mask & (1 << i))
            need = ResourceVector()
            urgency_penalty = 0.0
            for candidate in subset:
                need = need + candidate.needs
                if candidate.deadline is not None:
                    urgency_penalty += max(0.0, now - candidate.deadline)
            if not need.fits_in(avail):
                continue
            score = (
                len(subset),
                -urgency_penalty,
                sum(c.priority for c in subset),
                tuple(c.task_id for c in subset),
            )
            if best[1] is None or score > best[0]:
                best = (score, subset)  # type: ignore[assignment]
        if best[1] is None:
            # Fallback deterministic fitting pass.
            fitting: list[ResumeCandidate] = []
            remaining_avail = avail
            for candidate in sorted(candidates, key=lambda c: (c.deadline or float("inf"), c.task_id)):
                if candidate.needs.fits_in(remaining_avail):
                    fitting.append(candidate)
                    remaining_avail = remaining_avail - candidate.needs
            chosen_subset = tuple(fitting)
        else:
            chosen_subset = best[1]
        chosen_ids = {c.task_id for c in chosen_subset}
        deferred = tuple(c.task_id for c in candidates if c.task_id not in chosen_ids)

    ordered = sorted(
        chosen_subset,
        key=lambda c: (
            c.deadline if c.deadline is not None else float("inf"),
            -c.priority,
            c.event_order if cfg.preserve_event_order else 0,
            c.task_id,
        ),
    )

    steps: list[PlanStep] = []
    current = avail
    for index, candidate in enumerate(ordered, start=1):
        before = current
        current = current - candidate.needs
        steps.append(
            PlanStep(
                order=index,
                kind="resume",
                task_id=candidate.task_id,
                description=f"从保存点恢复任务 {candidate.task_id}，分配 {candidate.needs}",
                resources_before=before,
                resources_after=current,
                logical_time=now,
                finish_time=now + candidate.resume_duration,
                details={
                    "deadline": candidate.deadline,
                    "priority": candidate.priority,
                    "needs": candidate.needs.to_json(),
                },
            )
        )
    return ResumeOrderResult(
        resumed=tuple(c.task_id for c in ordered),
        deferred=deferred,
        steps=tuple(steps),
        available_after=current,
    )


def plan_reclamation(
    *,
    available: ResourceVector | Mapping[str, Any],
    groups: Sequence[ActionGroup],
    claims: Mapping[str, ResourceVector] | None = None,
    capacity: ResourceVector | Mapping[str, Any] | None = None,
    resume_candidates: Sequence[ResumeCandidate] = (),
    now: float = 0.0,
    config: PlannerConfig | None = None,
) -> ReclamationPlan:
    """Search release alternatives and produce release/grant/resume steps."""
    cfg = config or PlannerConfig()
    avail = as_vector(available)
    claim_vectors = {owner: as_vector(amount) for owner, amount in (claims or {}).items()}
    group_tuple = tuple(groups)

    choices: list[tuple[ReleaseAction | None, ...]] = []
    option_lists: list[list[ReleaseAction | None]] = []
    for group in group_tuple:
        opts: list[ReleaseAction | None] = list(group.options)
        if not group.required:
            opts.append(None)
        option_lists.append(opts)

    best_key: tuple[Any, ...] | None = None
    best_actions: tuple[ReleaseAction, ...] | None = None
    best_order: ReleaseOrderResult | None = None
    best_returned: dict[str, ResourceVector] | None = None
    failure_candidate: tuple[int, tuple[Any, ...], tuple[ReleaseAction, ...], ReleaseOrderResult, dict] | None = None
    evaluated = 0

    for combination in product(*option_lists):
        evaluated += 1
        if evaluated > cfg.max_configurations:
            break
        actions = tuple(action for action in combination if action is not None)
        # Required groups are enforced by option_lists not containing None.
        returned = _sum_returns(actions)
        if not _claims_covered(returned, claim_vectors):
            continue
        order_result = find_release_order(avail, actions, now=now, config=cfg)
        if not order_result.feasible:
            ordered_count = len(order_result.order)
            failure_key = (
                ordered_count,
                -sum(a.suspension_count for a in actions),
                -len(actions),
            )
            if failure_candidate is None or failure_key > failure_candidate[0]:
                failure_candidate = (failure_key, combination, actions, order_result, returned)  # type: ignore[arg-type]
            continue
        if cfg.enforce_deadlines and order_result.deadline_violations:
            deadline_key = (
                len(order_result.order),
                -sum(a.suspension_count for a in actions),
                -len(actions),
            )
            if failure_candidate is None or deadline_key > failure_candidate[0]:
                failure_candidate = (deadline_key, combination, actions, order_result, returned)  # type: ignore[arg-type]
            continue
        cost = _configuration_cost(actions)
        # Include event/task determinism only after substantive cost.
        tie = tuple((a.event_order, a.task_id, a.action_id) for a in sorted(actions, key=lambda x: x.task_id))
        key = cost + tie
        if best_key is None or key < best_key:
            best_key = key
            best_actions = actions
            best_order = order_result
            best_returned = returned

    if best_order is None or best_actions is None or best_returned is None:
        if failure_candidate is not None:
            _, _, actions, order_result, returned = failure_candidate
            cycle = order_result.cycle
            if order_result.feasible and order_result.deadline_violations:
                reason = "资源释放顺序存在，但保守工期预计超过完成期限"
            else:
                reason = (
                    cycle.message
                    if cycle is not None
                    else "不存在同时满足资源缓冲与回收额度要求的合法释放顺序"
                )
            return ReclamationPlan(
                feasible=False,
                release_order=order_result.order,
                steps=list(order_result.steps),
                selected_actions={a.task_id: a for a in actions},
                claims=claim_vectors,
                returned=returned,
                available_before=avail,
                available_after_release=order_result.available_after,
                available_after_grants=order_result.available_after,
                resume=None,
                suspended=tuple(a.task_id for a in actions if a.suspend),
                cycle=cycle,
                deadline_violations=order_result.deadline_violations,
                reason=reason,
                configurations_evaluated=evaluated,
            )
        return ReclamationPlan(
            feasible=False,
            release_order=(),
            steps=[],
            selected_actions={},
            claims=claim_vectors,
            returned={},
            available_before=avail,
            available_after_release=avail,
            available_after_grants=avail,
            resume=None,
            suspended=(),
            cycle=None,
            deadline_violations=(),
            reason="没有可用于满足回收请求的合法释放选项",
            configurations_evaluated=evaluated,
        )

    assert best_order is not None
    steps = list(best_order.steps)
    current_avail = best_order.available_after

    # Grant reclaimed quota to streams only after the coordinated buffer chain.
    grant_index = len(steps)
    for owner in sorted(claim_vectors):
        claim = claim_vectors[owner]
        if not claim.fits_in(current_avail):
            # This should not happen when returned accounting is consistent,
            # but keeping the defensive check makes malformed inputs explicit.
            return ReclamationPlan(
                feasible=False,
                release_order=best_order.order,
                steps=steps,
                selected_actions={a.task_id: a for a in best_actions},
                claims=claim_vectors,
                returned=best_returned,
                available_before=avail,
                available_after_release=best_order.available_after,
                available_after_grants=current_avail,
                resume=None,
                suspended=tuple(a.task_id for a in best_actions if a.suspend),
                cycle=None,
                deadline_violations=best_order.deadline_violations,
                reason=f"释放后仍无法将 {claim} 授予流任务 {owner}",
                configurations_evaluated=evaluated,
            )
        before = current_avail
        current_avail = current_avail - claim
        grant_index += 1
        steps.append(
            PlanStep(
                order=grant_index,
                kind="grant",
                task_id=owner,
                description=f"将回收额度 {claim} 归还并恢复流任务 {owner}",
                resources_before=before,
                resources_after=current_avail,
                logical_time=now,
                details={"claim": claim.to_json()},
            )
        )

    effective_resume_candidates = list(resume_candidates)
    explicit_resume_ids = {candidate.task_id for candidate in effective_resume_candidates}
    for action in best_actions:
        if action.suspend and action.task_id not in explicit_resume_ids and action.resume_needs:
            effective_resume_candidates.append(
                ResumeCandidate(
                    task_id=action.task_id,
                    needs=action.resume_needs,
                    deadline=action.deadline,
                    priority=action.priority,
                    event_order=action.event_order,
                )
            )

    resume_result = plan_resume_order(
        current_avail,
        effective_resume_candidates,
        now=now,
        config=cfg,
    )
    for step in resume_result.steps:
        step.order = len(steps) + 1
        steps.append(step)
    available_after_resume = resume_result.available_after

    return ReclamationPlan(
        feasible=not (cfg.enforce_deadlines and best_order.deadline_violations),
        release_order=best_order.order,
        steps=steps,
        selected_actions={a.task_id: a for a in best_actions},
        claims=claim_vectors,
        returned=best_returned,
        available_before=avail,
        available_after_release=best_order.available_after,
        available_after_grants=current_avail,
        resume=resume_result,
        suspended=tuple(a.task_id for a in best_actions if a.suspend),
        cycle=None,
        deadline_violations=best_order.deadline_violations,
        reason="" if not (cfg.enforce_deadlines and best_order.deadline_violations) else "资源顺序可行，但预计超过完成期限",
        configurations_evaluated=evaluated,
    )


def _avail_after_grants(avail: ResourceVector, claims: Mapping[str, ResourceVector]) -> ResourceVector:
    result = avail
    for claim in claims.values():
        result = result - claim
    return result
