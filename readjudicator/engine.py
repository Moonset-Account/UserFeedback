"""Exact maximum-set adjudication, atomic allocation, and diagnostics."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import combinations, permutations
from math import inf
from typing import Any

from .graph import RelayGraph, Route, graph_from_dict
from .ledger import Candidate, Ledger
from .model import (
    DEVICE,
    PRESENCE,
    AdjudicationInputError,
    OperationRequest,
    Proof,
    Requirement,
    operation_from_dict,
    proof_from_dict,
)
from .results import (
    AdjudicationResult,
    ApprovedOperation,
    MissingCapability,
    ProofAssignment,
    RejectedOperation,
)


@dataclass(frozen=True)
class Slot:
    index: int
    label: str
    requirement: Requirement


@dataclass(frozen=True)
class ScheduledOperation:
    operation: OperationRequest
    order: int
    started_at: float
    confirmed_at: float


@dataclass(frozen=True)
class GlobalSlot:
    global_index: int
    at: float
    operation: OperationRequest
    slot: Slot


def prepare_inputs(
    operations: Iterable[OperationRequest | Mapping[str, Any]] = (),
    proofs: Iterable[Proof | Mapping[str, Any]] | Mapping[str, Any] = (),
    *,
    presence_proofs: Iterable[Proof | Mapping[str, Any]] = (),
    device_proofs: Iterable[Proof | Mapping[str, Any]] = (),
    graph: RelayGraph | Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Proof], list[OperationRequest], RelayGraph | None]:
    proof_records: list[Proof] = []

    def add_proof(raw: Any, default_kind: str | None = None) -> None:
        proof = proof_from_dict(raw, default_kind=default_kind)
        proof_records.append(proof)

    if isinstance(proofs, Mapping):
        # Accept {"p1": {...}} as well as a normal list of proof objects.
        for key, value in proofs.items():
            if isinstance(value, Proof):
                proof_records.append(value)
            elif isinstance(value, Mapping):
                item = dict(value)
                item.setdefault("proof_id", key)
                add_proof(item)
            else:
                raise AdjudicationInputError("proof mapping values must be Proof or mappings")
    else:
        for raw in proofs or ():
            add_proof(raw)

    for raw in presence_proofs or ():
        add_proof(raw, PRESENCE)
    for raw in device_proofs or ():
        add_proof(raw, DEVICE)

    proof_map: dict[str, Proof] = {}
    for proof in proof_records:
        if proof.proof_id in proof_map:
            raise AdjudicationInputError(f"duplicate proof id: {proof.proof_id}")
        proof_map[proof.proof_id] = proof

    operation_list = [operation_from_dict(raw) for raw in operations or ()]
    ids = [op.request_id for op in operation_list]
    if len(ids) != len(set(ids)):
        raise AdjudicationInputError("operation request_id/id values must be unique")

    relay = graph_from_dict(graph)
    return proof_map, operation_list, relay


def proof_time_failure(proof: Proof, at: float) -> str | None:
    if proof.valid_from is not None and at < proof.valid_from:
        return "proof_not_yet_valid"
    if proof.expires_at is not None and at > proof.expires_at:
        return "proof_expired"
    return None


def evaluate_candidate_proof(
    req: Requirement,
    proof: Proof,
    op: OperationRequest,
    at: float,
    graph: RelayGraph | None,
    max_routes: int | None = None,
) -> tuple[list[Route], str | None]:
    failure = proof_time_failure(proof, at)
    if failure:
        return [], failure
    if req.proof_kind is not None and proof.kind != req.proof_kind:
        return [], "proof_kind_mismatch"
    if req.capability not in proof.capability_names:
        return [], "missing_capability"
    if req.identity_matches_request:
        if op.identity is None:
            return [], "request_identity_unknown"
        if proof.identity != op.identity:
            return [], "identity_mismatch"
    if req.identity_must_differ_from_request and op.identity is not None and proof.identity == op.identity:
        return [], "identity_mismatch"
    if req.allowed_identities and proof.identity not in req.allowed_identities:
        return [], "identity_not_allowed"

    attr_map = proof.attribute_map
    for key, value in req.attributes:
        if attr_map.get(key) != value:
            return [], "attribute_mismatch"

    if graph is None:
        routes = [Route(())]
    else:
        routes = graph.routes(proof.node, op.entry_node, proof.kind, max_routes=max_routes)
    if not routes:
        return [], "no_relay_route"

    affordable: list[Route] = []
    for route in routes:
        costs = route.total_authorization_costs()
        ok = True
        for key, amount in costs.items():
            total = proof.authorizations.get(key, 0)
            if total is not None and total < amount:
                ok = False
                break
        if ok:
            affordable.append(route)
    if not affordable:
        return [], "insufficient_authorization"
    return affordable, None


def candidate_list(
    slot: Slot,
    op: OperationRequest,
    at: float,
    graph: RelayGraph | None,
    ledger: Ledger,
    max_routes: int | None,
) -> list[Candidate]:
    result: list[Candidate] = []
    for proof in ledger.proofs.values():
        routes, _failure = evaluate_candidate_proof(slot.requirement, proof, op, at, graph, max_routes)
        for route in routes:
            candidate = Candidate(proof, route)
            if ledger.can_reserve(candidate):
                result.append(candidate)
    result.sort(key=lambda c: c.sort_key)
    return result


def build_slots(op: OperationRequest) -> tuple[Slot, ...]:
    slots: list[Slot] = []
    index = 0
    for req in op.requirements:
        for _repeat in range(req.count):
            slots.append(Slot(index=index, label=req.label, requirement=req))
            index += 1
    return tuple(slots)


def allocate_operation(
    op: OperationRequest,
    ledger: Ledger,
    at: float,
    graph: RelayGraph | None = None,
    max_routes: int | None = None,
) -> tuple[ProofAssignment, ...] | None:
    """Allocate every slot atomically; return None without leaving holds."""
    slots = build_slots(op)
    entry_snapshot = ledger.snapshot()
    assignments: list[ProofAssignment] = []

    def search(remaining: tuple[Slot, ...]) -> bool:
        if not remaining:
            return True

        candidate_sets: list[tuple[Slot, list[Candidate]]] = []
        for slot in remaining:
            candidates = candidate_list(slot, op, at, graph, ledger, max_routes)
            if not candidates:
                return False
            candidate_sets.append((slot, candidates))

        chosen_pos = min(range(len(candidate_sets)), key=lambda i: (len(candidate_sets[i][1]), candidate_sets[i][0].index))
        chosen_slot, candidates = candidate_sets[chosen_pos]
        next_remaining = tuple(s for s in remaining if s.index != chosen_slot.index)

        for candidate in candidates:
            snapshot = ledger.snapshot()
            if not ledger.reserve(candidate):
                ledger.restore(snapshot)
                continue

            route = candidate.route
            if route.edges:
                nodes = route.nodes
                edge_ids = route.edge_ids
            else:
                nodes = (candidate.proof.node, op.entry_node)
                edge_ids = ()
            assignment = ProofAssignment(
                slot_index=chosen_slot.index,
                requirement=chosen_slot.label,
                capability=chosen_slot.requirement.capability,
                proof_id=candidate.proof.proof_id,
                proof_kind=candidate.proof.kind,
                identity=candidate.proof.identity,
                route_edges=edge_ids,
                route_nodes=nodes,
                authorization_costs=route.total_authorization_costs(),
            )
            assignments.append(assignment)
            if search(next_remaining):
                return True
            assignments.pop()
            ledger.restore(snapshot)
        return False

    try:
        ok = search(slots)
    except Exception:
        ledger.restore(entry_snapshot)
        raise

    if not ok:
        ledger.restore(entry_snapshot)
        return None

    assignments.sort(key=lambda a: a.slot_index)
    return tuple(assignments)


def _assignment_for_candidate(slot: Slot, candidate: Candidate, op: OperationRequest) -> ProofAssignment:
    route = candidate.route
    if route.edges:
        nodes = route.nodes
        edge_ids = route.edge_ids
    else:
        nodes = (candidate.proof.node, op.entry_node)
        edge_ids = ()
    return ProofAssignment(
        slot_index=slot.index,
        requirement=slot.label,
        capability=slot.requirement.capability,
        proof_id=candidate.proof.proof_id,
        proof_kind=candidate.proof.kind,
        identity=candidate.proof.identity,
        route_edges=edge_ids,
        route_nodes=nodes,
        authorization_costs=route.total_authorization_costs(),
    )


def allocate_jointly(
    schedule: tuple[ScheduledOperation, ...],
    ledger: Ledger,
    graph: RelayGraph | None = None,
    max_routes: int | None = None,
) -> dict[str, tuple[ProofAssignment, ...]] | None:
    """Allocate all operations' slots as one backtracking problem.

    Sequential per-operation allocation can commit an assignment that later
    blocks another operation.  This function keeps all slots in one search so
    choices for earlier operations are revisited when necessary.
    """
    global_slots: list[GlobalSlot] = []
    global_index = 0
    for scheduled in schedule:
        for slot in build_slots(scheduled.operation):
            global_slots.append(
                GlobalSlot(
                    global_index=global_index,
                    at=scheduled.confirmed_at,
                    operation=scheduled.operation,
                    slot=slot,
                )
            )
            global_index += 1

    entry_snapshot = ledger.snapshot()
    tagged_assignments: list[tuple[str, ProofAssignment]] = []

    def search(remaining: tuple[GlobalSlot, ...]) -> bool:
        if not remaining:
            return True

        candidate_sets: list[tuple[GlobalSlot, list[Candidate]]] = []
        for global_slot in remaining:
            candidates = candidate_list(
                global_slot.slot,
                global_slot.operation,
                global_slot.at,
                graph,
                ledger,
                max_routes,
            )
            if not candidates:
                return False
            candidate_sets.append((global_slot, candidates))

        chosen_pos = min(
            range(len(candidate_sets)),
            key=lambda i: (
                len(candidate_sets[i][1]),
                candidate_sets[i][0].global_index,
            ),
        )
        chosen_global, candidates = candidate_sets[chosen_pos]
        next_remaining = tuple(
            item for item in remaining if item.global_index != chosen_global.global_index
        )

        for candidate in candidates:
            snapshot = ledger.snapshot()
            if not ledger.reserve(candidate):
                ledger.restore(snapshot)
                continue
            assignment = _assignment_for_candidate(
                chosen_global.slot, candidate, chosen_global.operation
            )
            tagged_assignments.append((chosen_global.operation.request_id, assignment))
            if search(next_remaining):
                return True
            tagged_assignments.pop()
            ledger.restore(snapshot)
        return False

    try:
        ok = search(tuple(global_slots))
    except Exception:
        ledger.restore(entry_snapshot)
        raise

    if not ok:
        ledger.restore(entry_snapshot)
        return None

    grouped: dict[str, list[ProofAssignment]] = {s.operation.request_id: [] for s in schedule}
    for request_id, assignment in tagged_assignments:
        grouped[request_id].append(assignment)
    return {
        request_id: tuple(sorted(items, key=lambda a: a.slot_index))
        for request_id, items in grouped.items()
    }


def build_schedule(operations: Iterable[OperationRequest], now: float) -> tuple[ScheduledOperation, ...] | None:
    ordered = sorted(
        operations,
        key=lambda op: (
            op.deadline if op.deadline is not None else inf,
            -op.priority,
            op.request_id,
        ),
    )
    current = float(now)
    scheduled: list[ScheduledOperation] = []
    for order, op in enumerate(ordered, start=1):
        started = current
        confirmed = current + op.duration
        if op.deadline is not None and confirmed > op.deadline:
            return None
        scheduled.append(ScheduledOperation(op, order, started, confirmed))
        current = confirmed
    return tuple(scheduled)


def schedule_in_order(operations: Sequence[OperationRequest], now: float) -> tuple[ScheduledOperation, ...] | None:
    """Validate a concrete terminal order without EDF reordering."""
    current = float(now)
    scheduled: list[ScheduledOperation] = []
    for order, op in enumerate(operations, start=1):
        started = current
        confirmed = current + op.duration
        if op.deadline is not None and confirmed > op.deadline:
            return None
        scheduled.append(ScheduledOperation(op, order, started, confirmed))
        current = confirmed
    return tuple(scheduled)


def make_approved(scheduled: ScheduledOperation, assignments: tuple[ProofAssignment, ...]) -> ApprovedOperation:
    op = scheduled.operation
    return ApprovedOperation(
        request_id=op.request_id,
        identity=op.identity,
        order=scheduled.order,
        started_at=scheduled.started_at,
        confirmed_at=scheduled.confirmed_at,
        deadline=op.deadline,
        duration=op.duration,
        assignments=assignments,
    )


def _consumers_from_approved(approved: Iterable[ApprovedOperation]) -> dict[str, list[str]]:
    consumers: dict[str, list[str]] = {}
    for op in approved:
        for proof_id in op.proof_ids:
            consumers.setdefault(proof_id, [])
            if op.request_id not in consumers[proof_id]:
                consumers[proof_id].append(op.request_id)
    return consumers


def _maximum_assignable_for_requirement(
    req: Requirement,
    op: OperationRequest,
    at: float,
    base_ledger: Ledger,
    graph: RelayGraph | None,
    max_routes: int | None,
) -> int:
    upper = req.count
    for amount in range(upper, -1, -1):
        if amount == 0:
            return 0
        probe_req = replace(req, count=amount)
        probe_op = OperationRequest(
            request_id=f"{op.request_id}::__diagnostic_{req.capability}_{amount}",
            requirements=(probe_req,),
            deadline=None,
            identity=op.identity,
            entry_node=op.entry_node,
            duration=0.0,
        )
        probe_ledger = base_ledger.clone()
        if allocate_operation(probe_op, probe_ledger, at, graph, max_routes) is not None:
            return amount
    return 0


def _static_requirement_diagnosis(
    req: Requirement,
    op: OperationRequest,
    at: float,
    proof_map: dict[str, Proof],
    graph: RelayGraph | None,
    max_routes: int | None,
) -> tuple[tuple[str, ...], str, dict[str, int]]:
    candidate_ids: list[str] = []
    failure_counts: dict[str, int] = {}
    for proof in proof_map.values():
        routes, failure = evaluate_candidate_proof(req, proof, op, at, graph, max_routes)
        if routes:
            candidate_ids.append(proof.proof_id)
        elif failure:
            failure_counts[failure] = failure_counts.get(failure, 0) + 1

    if candidate_ids:
        return tuple(sorted(candidate_ids)), "candidate_available", failure_counts

    priority = (
        "proof_expired",
        "proof_not_yet_valid",
        "no_relay_route",
        "insufficient_authorization",
        "request_identity_unknown",
        "identity_mismatch",
        "identity_not_allowed",
        "attribute_mismatch",
        "proof_kind_mismatch",
        "missing_capability",
    )
    code = next((item for item in priority if failure_counts.get(item)), "capability_absent")
    return (), code, failure_counts


def _message_for_reason(code: str, req: Requirement) -> str:
    messages = {
        "capability_absent": f"没有任何已验证证明提供能力 {req.capability!r}",
        "proof_kind_mismatch": f"提供 {req.capability!r} 的证明类型不符合要求",
        "identity_mismatch": f"证明身份与操作 {req.capability!r} 要求的身份策略不匹配",
        "identity_not_allowed": f"没有允许身份的证明可满足 {req.capability!r}",
        "request_identity_unknown": f"操作缺少身份，无法满足 {req.capability!r} 的身份绑定",
        "attribute_mismatch": f"证明属性无法满足 {req.capability!r} 的指定组合",
        "no_relay_route": f"接力图中没有允许该证明到达操作节点的路径：{req.capability!r}",
        "insufficient_authorization": f"接力边所需在场授权不足：{req.capability!r}",
        "proof_expired": f"证明在确认时间已过期：{req.capability!r}",
        "proof_not_yet_valid": f"证明在确认时间尚未生效：{req.capability!r}",
        "exhausted_by_approval": f"能力 {req.capability!r} 的剩余一次性证明已被批准集合占用",
        "capability_shortage": f"能力 {req.capability!r} 的证明数量不足",
        "shortage_and_exhaustion": f"能力 {req.capability!r} 原本不足，且部分证明已被批准集合占用",
        "joint_proof_contention": f"各要求单独有候选证明，但相同一次性证明无法共同满足整个组合：{req.capability!r}",
    }
    return messages.get(code, f"能力 {req.capability!r} 不可用（{code}）")


def diagnose_operation(
    op: OperationRequest,
    base_ledger: Ledger,
    proof_map: dict[str, Proof],
    now: float,
    graph: RelayGraph | None,
    consumers: Mapping[str, list[str]],
    *,
    max_routes: int | None = None,
    schedule_possible_alone: bool = True,
) -> RejectedOperation:
    alone_schedule = build_schedule([op], now)
    if alone_schedule is not None:
        at = alone_schedule[0].confirmed_at
        schedule_ok = True
    else:
        at = float(now) + op.duration
        schedule_ok = False

    probe = base_ledger.clone()
    joint_assignments = allocate_operation(op, probe, at, graph, max_routes)

    missing: list[MissingCapability] = []
    unmet: list[str] = []
    conflict: list[str] = []
    hard_missing = False

    if joint_assignments is None:
        for req in op.requirements:
            residual_amount = _maximum_assignable_for_requirement(
                req, op, at, base_ledger, graph, max_routes
            )
            global_ledger = Ledger(proof_map, graph)
            global_amount = _maximum_assignable_for_requirement(
                req, op, at, global_ledger, graph, max_routes
            )
            static_ids, static_code, failure_counts = _static_requirement_diagnosis(
                req, op, at, proof_map, graph, max_routes
            )

            if residual_amount < req.count:
                hard_missing = True
                unmet.append(req.label)
                if global_amount == 0:
                    code = static_code if static_code != "candidate_available" else "capability_absent"
                elif global_amount < req.count and residual_amount < global_amount:
                    code = "shortage_and_exhaustion"
                elif global_amount < req.count:
                    code = "capability_shortage"
                else:
                    code = "exhausted_by_approval"

                consumed_by = {
                    pid: tuple(consumers.get(pid, ()))
                    for pid in static_ids
                    if consumers.get(pid)
                }
                missing.append(
                    MissingCapability(
                        capability=req.capability,
                        requirement=req.label,
                        required=req.count,
                        available_after=residual_amount,
                        available_globally=global_amount,
                        reason_code=code,
                        message=_message_for_reason(code, req),
                        candidate_proof_ids=static_ids,
                        consumed_by=consumed_by,  # type: ignore[arg-type]
                        detail={"failure_counts": failure_counts, "at": at},
                    )
                )
            else:
                conflict.append(req.label)

        if not missing and conflict:
            # Every requirement is independently satisfiable, but the atomic
            # joint allocation failed.  Report each capability as contested.
            for req in op.requirements:
                static_ids, _code, _counts = _static_requirement_diagnosis(
                    req, op, at, proof_map, graph, max_routes
                )
                consumed_by = {
                    pid: tuple(consumers.get(pid, ()))
                    for pid in static_ids
                    if consumers.get(pid)
                }
                missing.append(
                    MissingCapability(
                        capability=req.capability,
                        requirement=req.label,
                        required=req.count,
                        available_after=req.count,
                        available_globally=req.count,
                        reason_code="joint_proof_contention",
                        message=_message_for_reason("joint_proof_contention", req),
                        candidate_proof_ids=static_ids,
                        consumed_by=consumed_by,  # type: ignore[arg-type]
                        detail={"at": at},
                    )
                )

    if not schedule_ok and joint_assignments is None:
        reason_code = "deadline_and_capabilities_missing"
        message = "操作超过最迟确认时间，且剩余证明组合不完整"
    elif not schedule_ok:
        reason_code = "deadline_exceeded"
        message = f"按当前终端时间处理将在 {at:g} 完成，超过最迟确认时间 {op.deadline:g}"
    elif joint_assignments is None:
        reason_code = "requirements_unavailable" if hard_missing else "conflicting_proof_combination"
        message = "无法原子化满足完整证明组合" if hard_missing else "证明可单独使用，但无法共同完成分配"
    else:
        reason_code = "terminal_schedule_conflict"
        message = "剩余证明可满足该申请，但加入已选集合后终端时限或最大集合约束使其不能一起批准"

    return RejectedOperation(
        request_id=op.request_id,
        identity=op.identity,
        deadline=op.deadline,
        reason_code=reason_code,
        message=message,
        missing_capabilities=tuple(missing),
        unmet_requirements=tuple(unmet),
        conflict_requirements=tuple(conflict if not missing else conflict),
        schedule_possible_alone=schedule_ok,
        diagnostics={"evaluated_at": at, "joint_residual_allocation_possible": joint_assignments is not None},
    )


def adjudicate(
    operations: Iterable[OperationRequest | Mapping[str, Any]] = (),
    proofs: Iterable[Proof | Mapping[str, Any]] | Mapping[str, Any] = (),
    *,
    presence_proofs: Iterable[Proof | Mapping[str, Any]] = (),
    device_proofs: Iterable[Proof | Mapping[str, Any]] = (),
    graph: RelayGraph | Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
    now: float = 0.0,
    terminal_id: str | None = None,
    max_routes: int | None = None,
) -> AdjudicationResult:
    """Return the largest deterministically chosen jointly-approvable set."""
    proof_map, operation_list, relay = prepare_inputs(
        operations,
        proofs,
        presence_proofs=presence_proofs,
        device_proofs=device_proofs,
        graph=graph,
    )
    now = float(now)
    by_id = {op.request_id: op for op in operation_list}
    id_order = sorted(by_id)
    time_sensitive = any(
        proof.valid_from is not None or proof.expires_at is not None
        for proof in proof_map.values()
    )

    best_approved: tuple[ApprovedOperation, ...] = ()
    best_ledger = Ledger(proof_map, relay)
    examined = 0
    found = False

    for size in range(len(id_order), -1, -1):
        for combo in combinations(id_order, size):
            examined += 1
            combo_ops = [by_id[request_id] for request_id in combo]
            schedule = build_schedule(combo_ops, now)
            if schedule is None:
                continue

            ledger = Ledger(proof_map, relay)
            grouped = allocate_jointly(schedule, ledger, relay, max_routes)
            successful_schedule = schedule
            successful_ledger = ledger

            if grouped is None and time_sensitive and size > 1:
                # Proof validity can make a deadline-feasible non-EDF order the
                # only allocation-feasible order.  The input set is expected to
                # be a small control-room batch, so check all concrete orders.
                edf_ids = tuple(scheduled.operation.request_id for scheduled in schedule)
                for permutation in permutations(combo_ops):
                    if tuple(op.request_id for op in permutation) == edf_ids:
                        continue
                    ordered_schedule = schedule_in_order(permutation, now)
                    if ordered_schedule is None:
                        continue
                    permutation_ledger = Ledger(proof_map, relay)
                    permutation_grouped = allocate_jointly(
                        ordered_schedule, permutation_ledger, relay, max_routes
                    )
                    if permutation_grouped is not None:
                        grouped = permutation_grouped
                        successful_schedule = ordered_schedule
                        successful_ledger = permutation_ledger
                        break

            if grouped is not None:
                approved = tuple(
                    make_approved(
                        scheduled,
                        grouped[scheduled.operation.request_id],
                    )
                    for scheduled in successful_schedule
                )
                best_approved = tuple(sorted(approved, key=lambda a: a.order))
                best_ledger = successful_ledger
                found = True
                break
        if found:
            break

    approved_ids = {op.request_id for op in best_approved}
    consumers = _consumers_from_approved(best_approved)
    rejected: list[RejectedOperation] = []
    for request_id in id_order:
        if request_id in approved_ids:
            continue
        op = by_id[request_id]
        rejected.append(
            diagnose_operation(
                op,
                best_ledger,
                proof_map,
                now,
                relay,
                consumers,
                max_routes=max_routes,
            )
        )

    return AdjudicationResult(
        now=now,
        terminal_id=terminal_id,
        maximum_size=len(best_approved),
        approved=best_approved,
        rejected=tuple(rejected),
        search_examined=examined,
    )


def adjudicate_payload(payload: Mapping[str, Any], **overrides: Any) -> AdjudicationResult:
    """Adjudicate a JSON-like dictionary payload."""
    if not isinstance(payload, Mapping):
        raise AdjudicationInputError("payload must be a mapping")
    options = dict(payload)
    options.update(overrides)
    operations = options.get("operations", options.get("requests", options.get("pending", ())))
    return adjudicate(
        operations,
        proofs=options.get("proofs", ()),
        presence_proofs=options.get("presence_proofs", options.get("presence", ())),
        device_proofs=options.get("device_proofs", options.get("device", ())),
        graph=options.get("graph"),
        now=options.get("now", 0.0),
        terminal_id=options.get("terminal_id"),
        max_routes=options.get("max_routes"),
    )


class AdjudicationSession:
    """Stateful one-terminal session for interleaved success/failure cases."""

    def __init__(
        self,
        proofs: Iterable[Proof | Mapping[str, Any]] | Mapping[str, Any] = (),
        *,
        presence_proofs: Iterable[Proof | Mapping[str, Any]] = (),
        device_proofs: Iterable[Proof | Mapping[str, Any]] = (),
        graph: RelayGraph | Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
        now: float = 0.0,
        terminal_id: str | None = None,
        max_routes: int | None = None,
    ) -> None:
        proof_map, _ops, relay = prepare_inputs(
            (),
            proofs,
            presence_proofs=presence_proofs,
            device_proofs=device_proofs,
            graph=graph,
        )
        self.proof_map = proof_map
        self.graph = relay
        self.ledger = Ledger(proof_map, relay)
        self.now = float(now)
        self.terminal_id = terminal_id
        self.max_routes = max_routes
        self.approved: list[ApprovedOperation] = []
        self.history: list[ApprovedOperation | RejectedOperation] = []
        self.consumers: dict[str, list[str]] = {}

    def review(
        self,
        operation: OperationRequest | Mapping[str, Any],
        *,
        advance_time: bool = True,
    ) -> ApprovedOperation | RejectedOperation:
        op = operation_from_dict(operation)
        schedule = build_schedule([op], self.now)
        snapshot = self.ledger.snapshot()

        if schedule is None:
            rejected = diagnose_operation(
                op,
                self.ledger,
                self.proof_map,
                self.now,
                self.graph,
                self.consumers,
                max_routes=self.max_routes,
                schedule_possible_alone=False,
            )
            self.history.append(rejected)
            return rejected

        scheduled = schedule[0]
        assignments = allocate_operation(
            op, self.ledger, scheduled.confirmed_at, self.graph, self.max_routes
        )
        if assignments is None:
            self.ledger.restore(snapshot)
            rejected = diagnose_operation(
                op,
                self.ledger,
                self.proof_map,
                self.now,
                self.graph,
                self.consumers,
                max_routes=self.max_routes,
                schedule_possible_alone=True,
            )
            self.history.append(rejected)
            return rejected

        approved = make_approved(
            # Preserve session processing order in addition to EDF order (same for one op).
            replace(scheduled, order=len(self.approved) + 1),
            assignments,
        )
        self.approved.append(approved)
        if advance_time:
            self.now = scheduled.confirmed_at
        else:
            # Caller asked not to model elapsed terminal handling time.
            pass
        for proof_id in approved.proof_ids:
            self.consumers.setdefault(proof_id, [])
            if op.request_id not in self.consumers[proof_id]:
                self.consumers[proof_id].append(op.request_id)
        self.history.append(approved)
        return approved

    process = review


# Public semantic alias.
solve = adjudicate
