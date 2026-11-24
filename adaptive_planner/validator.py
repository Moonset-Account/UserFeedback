"""Offline validation and causal no-scenario-leak audits.

The validator deliberately re-simulates a serialized strategy using the
problem's integer tables.  It does not trust reported costs, paths, or the
solver's belief labels.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import PlannerError, Problem, ordering_covers
from .planner import plan_strategy


@dataclass
class ValidationReport:
    ok: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked_scenarios: int = 0
    checked_decisions: int = 0
    leak_violations: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "checked_scenarios": self.checked_scenarios,
            "checked_decisions": self.checked_decisions,
            "leak_violations": self.leak_violations,
        }


def _canonical_action(action: Mapping[str, Any] | None) -> tuple[Any, ...]:
    if action is None:
        return ("terminal",)
    return (
        action.get("kind"),
        action.get("algorithm"),
        action.get("edge_id"),
        action.get("mask"),
        action.get("left_mask"),
        action.get("right_mask"),
        action.get("output_mask"),
        bool(action.get("sort_left_required", False)),
        bool(action.get("sort_right_required", False)),
    )


def audit_decision_traces(
    traces: Mapping[str, Sequence[Mapping[str, Any]]],
) -> ValidationReport:
    """Check that equal observation/action prefixes lead to equal actions.

    Each trace is an ordered list of objects.  An action should be supplied as
    ``action``; an observation produced by that action may be supplied as
    ``observed_rows`` (with optional ``subset``).  The check is causal: only
    observations from earlier steps are available before the current decision.
    """

    report = ValidationReport()
    if not isinstance(traces, Mapping) or not traces:
        report.error("traces must be a non-empty mapping of scenario id to steps")
        return report

    scenario_steps: dict[str, list[Mapping[str, Any]]] = {}
    for sid, steps in traces.items():
        if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
            report.error(f"trace {sid!r} must be a list")
            continue
        scenario_steps[str(sid)] = list(steps)

    if not report.ok:
        return report

    max_len = max((len(steps) for steps in scenario_steps.values()), default=0)
    groups: dict[tuple[Any, ...], set[str]] = {}
    examples: dict[tuple[Any, ...], dict[str, str]] = {}

    for step_index in range(max_len):
        prefix_groups: dict[tuple[Any, ...], dict[str, str]] = {}
        for sid, steps in scenario_steps.items():
            if step_index >= len(steps):
                continue
            step = steps[step_index]
            if not isinstance(step, Mapping):
                report.error(f"{sid} step {step_index} must be an object")
                continue
            action = str(step.get("action", step.get("label", "")))
            if not action:
                report.error(f"{sid} step {step_index} lacks an action")
                continue

            prior_obs: list[tuple[Any, Any]] = []
            prior_actions: list[str] = []
            for prev in steps[:step_index]:
                prior_actions.append(str(prev.get("action", prev.get("label", ""))))
                if prev.get("observed_rows") is not None:
                    prior_obs.append(
                        (prev.get("subset", prev.get("mask")), prev.get("observed_rows"))
                    )
            key = (tuple(prior_obs), tuple(prior_actions))
            prefix_groups.setdefault(key, {})[sid] = action

        for key, sid_actions in prefix_groups.items():
            distinct = set(sid_actions.values())
            if len(distinct) > 1:
                msg = (
                    "different actions before a distinguishing observation: "
                    + ", ".join(f"{sid}->{act}" for sid, act in sorted(sid_actions.items()))
                )
                report.leak_violations.append(msg)
                report.error(msg)

    report.checked_scenarios = len(scenario_steps)
    report.checked_decisions = sum(len(steps) for steps in scenario_steps.values())
    report.ok = not report.errors
    return report


def validate_no_scenario_leak(
    traces: Mapping[str, Sequence[Mapping[str, Any]]],
) -> ValidationReport:
    """Descriptive alias for :func:`audit_decision_traces`."""

    return audit_decision_traces(traces)


def _trace_from_strategy(strategy: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    traces: dict[str, list[dict[str, Any]]] = {}
    paths = strategy.get("paths", {})
    if not isinstance(paths, Mapping):
        return traces
    for sid, steps in paths.items():
        trace: list[dict[str, Any]] = []
        if not isinstance(steps, Sequence):
            continue
        action_steps = [s for s in steps if isinstance(s, Mapping)]
        initial_obs: list[Mapping[str, Any]] = []
        if action_steps:
            first = action_steps[0]
            observations_so_far = first.get("observations_so_far", [])
            if isinstance(observations_so_far, list):
                initial_obs = [
                    o
                    for o in observations_so_far
                    if isinstance(o, Mapping) and o.get("stage") == "initial"
                ]
        for obs in initial_obs:
            trace.append(
                {
                    "action": "__existing_materialized_block_observation__",
                    "subset": obs.get("subset", obs.get("mask")),
                    "observed_rows": obs.get("observed_rows"),
                }
            )
        for step in steps:
            if not isinstance(step, Mapping) or step.get("kind") == "terminal":
                continue
            action_obj = step.get("action") if isinstance(step.get("action"), Mapping) else {}
            trace.append(
                {
                    "action": step.get("label") or _canonical_action(action_obj),
                    "subset": action_obj.get("subset")
                    if isinstance(action_obj, Mapping)
                    else None,
                    "observed_rows": step.get("observed_rows"),
                }
            )
        traces[str(sid)] = trace
    return traces


def verify_strategy(
    problem: Problem,
    strategy: Mapping[str, Any],
    *,
    check_optimal: bool = False,
) -> ValidationReport:
    report = ValidationReport()
    if not isinstance(strategy, Mapping):
        report.error("strategy must be a JSON object")
        return report

    nodes = strategy.get("nodes")
    if not isinstance(nodes, Mapping):
        report.error("strategy.nodes must be an object")
        return report
    root_id = strategy.get("root_node", "root")
    if root_id not in nodes:
        report.error(f"root node {root_id!r} is absent")
        return report
    root = nodes[root_id]
    if not isinstance(root, Mapping) or root.get("kind") != "root":
        report.error("root node must have kind='root'")
        return report

    edge_by_id = {e.eid: e for e in problem.edges}
    objective = strategy.get("objective", {})
    reported_costs = objective.get("scenario_total_costs", {}) if isinstance(objective, Mapping) else {}

    decision_groups: dict[tuple[Any, ...], set[tuple[Any, ...]]] = {}
    sim_totals: dict[str, int] = {}

    for si, scenario in enumerate(problem.scenarios):
        sid = scenario.sid
        report.checked_scenarios += 1
        forest = tuple(problem.initial_blocks)
        cumulative = problem.initial_sunk[si]
        obs_history: list[tuple[Any, Any]] = []
        action_history: list[tuple[Any, ...]] = []

        initial_sig = tuple(
            sorted(
                (b.mask, problem.row_count(si, b.mask))
                for b in problem.initial_blocks
                if b.materialized
            )
        )
        root_branch = _find_root_branch(report, root, initial_sig, problem)
        if root_branch is None:
            report.error(f"{sid}: no root branch matches initial observations {initial_sig}")
            continue

        for obs in root_branch.get("observations", []):
            if isinstance(obs, Mapping):
                obs_history.append((obs.get("mask", obs.get("subset")), obs.get("observed_rows")))

        current_id = root_branch.get("next_node")
        visited: set[str] = set()
        sim_steps: list[dict[str, Any]] = []
        sim_switches: list[dict[str, Any]] = []
        action_count = 0

        if initial_sig:
            initial_action_signatures = set()
            for rb in root.get("branches", []):
                if not isinstance(rb, Mapping):
                    continue
                next_node = nodes.get(rb.get("next_node"))
                next_action = (
                    next_node.get("action") if isinstance(next_node, Mapping) else None
                )
                initial_action_signatures.add(_canonical_action(next_action))
            sim_switches.append(
                {
                    "location": "initial_materialized_block",
                    "after_step": 0,
                    "causes_switch": len(initial_action_signatures) > 1,
                    "selected_next_node": current_id,
                }
            )

        while True:
            if not isinstance(current_id, str) or current_id not in nodes:
                report.error(f"{sid}: missing next node {current_id!r}")
                break
            if current_id in visited:
                report.error(f"{sid}: policy graph contains a cycle at {current_id!r}")
                break
            visited.add(current_id)
            node = nodes[current_id]
            if not isinstance(node, Mapping):
                report.error(f"{sid}: node {current_id!r} is not an object")
                break

            _check_frontier_state(report, problem, sid, current_id, node, forest)

            if node.get("kind") == "terminal":
                if len(forest) != 1 or forest[0].mask != problem.all_mask:
                    report.error(
                        f"{sid}: terminal node {current_id!r} has frontier "
                        f"{[problem.subset_key(b.mask) for b in forest]}"
                    )
                break

            if node.get("kind") != "decision":
                report.error(f"{sid}: node {current_id!r} has invalid kind {node.get('kind')!r}")
                break
            action = node.get("action")
            if not isinstance(action, Mapping):
                report.error(f"{sid}: node {current_id!r} lacks one action object")
                break

            report.checked_decisions += 1
            action_sig = _canonical_action(action)
            info_key = (tuple(obs_history), tuple(action_history))
            decision_groups.setdefault(info_key, set()).add(action_sig)
            if len(decision_groups[info_key]) > 1:
                report.leak_violations.append(
                    f"information prefix {info_key} has multiple actions: "
                    f"{decision_groups[info_key]}"
                )
                report.error(
                    "scenario leak/inconsistent policy: same observation/action prefix "
                    f"has different actions at node {current_id}"
                )

            try:
                cost, forest, observed, next_id = _apply_and_check_action(
                    report=report,
                    problem=problem,
                    si=si,
                    sid=sid,
                    node=node,
                    action=action,
                    forest=forest,
                    nodes=nodes,
                    edge_by_id=edge_by_id,
                    current_id=current_id,
                )
            except PlannerError as exc:
                report.error(f"{sid}: {exc}")
                break

            reported_action_cost = action.get("total_cost", {})
            if isinstance(reported_action_cost, Mapping) and sid in reported_action_cost:
                if reported_action_cost[sid] != cost:
                    report.error(
                        f"{sid} at {current_id}: reported action cost "
                        f"{reported_action_cost[sid]} != recomputed {cost}"
                    )

            cumulative += cost
            action_history.append(action_sig)
            action_count += 1
            sim_steps.append(
                {
                    "node_id": current_id,
                    "cost": cost,
                    "cumulative_cost": cumulative,
                    "observed_rows": observed,
                }
            )

            if action.get("kind") == "materialize":
                if observed is None:
                    report.error(f"{sid} at {current_id}: materialize produced no observation")
                    break
                obs_history.append((action.get("mask"), observed))
                branch = _find_observation_branch(node, observed)
                if branch is None:
                    report.error(
                        f"{sid} at {current_id}: no branch for observed_rows={observed}"
                    )
                    break
                next_id = branch.get("next_node")
                branch_action_signatures = set()
                for serialized_branch in node.get("branches", []):
                    if not isinstance(serialized_branch, Mapping):
                        continue
                    child_node = nodes.get(serialized_branch.get("next_node"))
                    child_action = (
                        child_node.get("action")
                        if isinstance(child_node, Mapping)
                        else None
                    )
                    branch_action_signatures.add(_canonical_action(child_action))
                sim_switches.append(
                    {
                        "location": "after_materialization",
                        "after_step": action_count,
                        "node_id": current_id,
                        "subset": action.get("subset"),
                        "mask": action.get("mask"),
                        "observed_rows": observed,
                        "causes_switch": len(branch_action_signatures) > 1,
                        "selected_next_node": next_id,
                    }
                )
            current_id = next_id
            if action_count > 2 * problem.n_tables + 2:
                report.error(f"{sid}: path exceeds finite action bound; likely malformed cycle")
                break

        sim_totals[sid] = cumulative
        _check_serialized_path(report, strategy, sid, sim_steps, cumulative)
        _check_serialized_switches(report, strategy, sid, sim_switches)

    # Objective checks -------------------------------------------------------
    if reported_costs:
        for sid, total in sim_totals.items():
            if sid in reported_costs and reported_costs[sid] != total:
                report.error(
                    f"objective cost for {sid}: reported {reported_costs[sid]} != recomputed {total}"
                )
        if isinstance(objective, Mapping):
            worst = objective.get("worst_total_cost")
            total_sum = objective.get("sum_of_scenario_total_costs")
            if worst is not None and sim_totals and worst != max(sim_totals.values()):
                report.error(
                    f"reported worst {worst} != recomputed {max(sim_totals.values())}"
                )
            if total_sum is not None and total_sum != sum(sim_totals.values()):
                report.error(
                    f"reported sum {total_sum} != recomputed {sum(sim_totals.values())}"
                )

    # Check serialized paths as an independent trace audit.
    trace_report = audit_decision_traces(_trace_from_strategy(strategy))
    report.leak_violations.extend(trace_report.leak_violations)
    for err in trace_report.errors:
        if err not in report.errors:
            report.error(err)

    if check_optimal:
        try:
            expected = plan_strategy(problem)
        except PlannerError as exc:
            report.error(f"optimality check failed while replanning: {exc}")
        else:
            exp_obj = expected.get("objective", {})
            got_obj = strategy.get("objective", {})
            for key in ("worst_total_cost", "sum_of_scenario_total_costs"):
                if got_obj.get(key) != exp_obj.get(key):
                    report.error(
                        f"strategy is not optimal for {key}: "
                        f"got {got_obj.get(key)}, expected {exp_obj.get(key)}"
                    )

    report.ok = not report.errors
    return report


def _find_root_branch(
    report: ValidationReport,
    root: Mapping[str, Any],
    initial_sig: tuple[tuple[int, int], ...],
    problem: Problem,
) -> Mapping[str, Any] | None:
    branches = root.get("branches", [])
    if not isinstance(branches, list):
        report.error("root.branches must be a list")
        return None
    wanted = list(initial_sig)
    for branch in branches:
        if not isinstance(branch, Mapping):
            continue
        obs = branch.get("observations", [])
        got: list[tuple[int, int]] = []
        if isinstance(obs, list):
            for item in obs:
                if isinstance(item, Mapping):
                    mask = item.get("mask")
                    rows = item.get("observed_rows")
                    if is_int(mask) and is_int(rows):
                        got.append((int(mask), int(rows)))  # type: ignore[arg-type]
        if sorted(got) == wanted:
            return branch
    # If there are no initial observations, accept the sole branch even if its
    # observation serialization used subset names rather than masks.
    if not initial_sig and len(branches) == 1 and isinstance(branches[0], Mapping):
        return branches[0]
    return None


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_frontier_state(
    report: ValidationReport,
    problem: Problem,
    sid: str,
    node_id: str,
    node: Mapping[str, Any],
    forest: tuple[Any, ...],
) -> None:
    state = node.get("state")
    if not isinstance(state, Mapping):
        return
    raw_frontier = state.get("frontier")
    if not isinstance(raw_frontier, list):
        report.error(f"{sid} {node_id}: state.frontier must be a list")
        return
    expected_masks = sorted(b.mask for b in forest)
    got_masks: list[int] = []
    for item in raw_frontier:
        if isinstance(item, Mapping) and is_int(item.get("mask")):
            got_masks.append(int(item["mask"]))
    if sorted(got_masks) != expected_masks:
        report.error(
            f"{sid} {node_id}: serialized frontier {sorted(got_masks)} != "
            f"simulated frontier {expected_masks}"
        )


def _find_observation_branch(
    node: Mapping[str, Any], observed: int
) -> Mapping[str, Any] | None:
    branches = node.get("branches", [])
    if not isinstance(branches, list):
        return None
    for branch in branches:
        if isinstance(branch, Mapping) and branch.get("observed_rows") == observed:
            return branch
    return None


def _block_by_mask(forest: tuple[Any, ...], mask: int) -> Any | None:
    for block in forest:
        if block.mask == mask:
            return block
    return None


def _apply_and_check_action(
    *,
    report: ValidationReport,
    problem: Problem,
    si: int,
    sid: str,
    node: Mapping[str, Any],
    action: Mapping[str, Any],
    forest: tuple[Any, ...],
    nodes: Mapping[str, Any],
    edge_by_id: Mapping[str, Any],
    current_id: str,
) -> tuple[int, tuple[Any, ...], int | None, str | None]:
    kind = action.get("kind")
    if kind == "materialize":
        mask = action.get("mask")
        if not is_int(mask):
            raise PlannerError("materialize action lacks integer mask")
        mask = int(mask)
        block = _block_by_mask(forest, mask)
        if block is None:
            raise PlannerError(f"materialized subset {problem.subset_key(mask)} is not on current frontier")
        if block.materialized:
            raise PlannerError(f"subset {problem.subset_key(mask)} was already completed/materialized")
        rows = problem.row_count(si, mask)
        cost = problem.materialization_cost.cost(rows)
        memory = problem.materialization_cost.memory(rows)
        limit = block.memory_limit
        if limit is not None and memory > limit:
            raise PlannerError(
                f"materialization memory {memory} exceeds declared limit {limit}"
            )
        new_forest = tuple(
            sorted(
                [
                    type(block)(
                        mask=block.mask,
                        ordering=block.ordering,
                        materialized=True,
                        memory_limit=block.memory_limit,
                        reuse_order=block.reuse_order,
                    )
                    if b.mask == mask
                    else b
                    for b in forest
                ],
                key=lambda b: b.mask,
            )
        )
        return cost, new_forest, rows, None

    if kind != "join":
        raise PlannerError(f"unknown action kind {kind!r}")

    lm = action.get("left_mask")
    rm = action.get("right_mask")
    om = action.get("output_mask")
    if not all(is_int(x) for x in (lm, rm, om)):
        raise PlannerError("join action lacks integer left/right/output masks")
    lm, rm, om = int(lm), int(rm), int(om)
    left = _block_by_mask(forest, lm)
    right = _block_by_mask(forest, rm)
    if left is None or right is None:
        raise PlannerError("join references a block that is not on the current frontier")
    if lm & rm:
        raise PlannerError("join inputs overlap; this would duplicate/recompute tables")
    if (lm | rm) != om:
        raise PlannerError("join output mask must be the disjoint union of its inputs")

    edge_id = action.get("edge_id")
    edge = edge_by_id.get(edge_id)
    if edge is None:
        raise PlannerError(f"join uses unknown edge_id {edge_id!r}")
    crosses = (
        (lm & edge.left_bit and rm & edge.right_bit)
        or (lm & edge.right_bit and rm & edge.left_bit)
    )
    if not crosses:
        raise PlannerError("join inputs are not connected by the declared edge")

    left_rows = problem.row_count(si, lm)
    right_rows = problem.row_count(si, rm)
    output_rows = problem.row_count(si, om)
    algorithm = action.get("algorithm")
    join_limit = edge.memory_limit
    if join_limit is None:
        join_limit = problem.memory_limits.get(om, problem.default_memory_limit)

    if algorithm == "hash":
        point = problem.hash_cost.lookup(left_rows, right_rows, output_rows)
        cost = point.cost
        if join_limit is not None and point.memory > join_limit:
            raise PlannerError(f"hash memory {point.memory} exceeds limit {join_limit}")
        output_order: tuple[str, ...] = ()
    elif algorithm == "merge":
        # Normalize edge keys to the serialized left/right action orientation.
        if lm & edge.left_bit:
            left_required = edge.left_keys
            right_required = edge.right_keys
        else:
            left_required = edge.right_keys
            right_required = edge.left_keys

        sort_left = bool(action.get("sort_left_required", False))
        sort_right = bool(action.get("sort_right_required", False))
        left_reusable = problem.reuse_existing_order and getattr(left, "reuse_order", True)
        right_reusable = problem.reuse_existing_order and getattr(right, "reuse_order", True)
        if not sort_left and not (
            left_reusable and ordering_covers(left.ordering, left_required)
        ):
            raise PlannerError("merge claims reused left order but input declaration does not permit/cover it")
        if not sort_right and not (
            right_reusable and ordering_covers(right.ordering, right_required)
        ):
            raise PlannerError("merge claims reused right order but input declaration does not permit/cover it")

        point = problem.merge_cost.lookup(left_rows, right_rows, output_rows)
        cost = point.cost
        if join_limit is not None and point.memory > join_limit:
            raise PlannerError(f"merge memory {point.memory} exceeds limit {join_limit}")

        if sort_left:
            if problem.sort_cost is None:
                raise PlannerError("merge requires left sort but no sort_cost table is declared")
            scost = problem.sort_cost.cost(left_rows)
            smem = problem.sort_cost.memory(left_rows)
            cost += scost
            if left.memory_limit is not None and smem > left.memory_limit:
                raise PlannerError(f"left sort memory {smem} exceeds limit {left.memory_limit}")
        if sort_right:
            if problem.sort_cost is None:
                raise PlannerError("merge requires right sort but no sort_cost table is declared")
            scost = problem.sort_cost.cost(right_rows)
            smem = problem.sort_cost.memory(right_rows)
            cost += scost
            if right.memory_limit is not None and smem > right.memory_limit:
                raise PlannerError(f"right sort memory {smem} exceeds limit {right.memory_limit}")

        output_order = tuple(edge.output_order or edge.left_keys)
    else:
        raise PlannerError(f"unknown join algorithm {algorithm!r}")

    remaining = tuple(b for b in forest if b.mask not in (lm, rm))
    from .model import Block

    output_block = Block(
        mask=om,
        ordering=output_order,
        materialized=False,
        memory_limit=problem.limit_for(om),
    )
    new_forest = tuple(sorted(remaining + (output_block,), key=lambda b: b.mask))
    next_id = node.get("next_node")
    return cost, new_forest, None, next_id if isinstance(next_id, str) else None


def _check_serialized_path(
    report: ValidationReport,
    strategy: Mapping[str, Any],
    sid: str,
    sim_steps: Sequence[Mapping[str, Any]],
    final_total: int,
) -> None:
    paths = strategy.get("paths")
    if not isinstance(paths, Mapping) or sid not in paths:
        report.error(f"{sid}: strategy.paths lacks an execution path")
        return
    path = paths[sid]
    if not isinstance(path, Sequence):
        report.error(f"{sid}: path must be a list")
        return

    action_path = [s for s in path if isinstance(s, Mapping) and s.get("kind") != "terminal"]
    if len(action_path) != len(sim_steps):
        report.error(
            f"{sid}: serialized path has {len(action_path)} actions, simulation has {len(sim_steps)}"
        )
        return

    for i, (serialized, simulated) in enumerate(zip(action_path, sim_steps)):
        if serialized.get("node_id") != simulated["node_id"]:
            report.error(f"{sid} path step {i}: node_id mismatch")
        if serialized.get("cost") != simulated["cost"]:
            report.error(
                f"{sid} path step {i}: cost {serialized.get('cost')} != {simulated['cost']}"
            )
        if serialized.get("cumulative_cost") != simulated["cumulative_cost"]:
            report.error(f"{sid} path step {i}: cumulative cost mismatch")
        if serialized.get("observed_rows") != simulated["observed_rows"]:
            report.error(f"{sid} path step {i}: observed_rows mismatch")

    terminals = [s for s in path if isinstance(s, Mapping) and s.get("kind") == "terminal"]
    if terminals and terminals[-1].get("cumulative_cost") != final_total:
        report.error(f"{sid}: terminal path cumulative cost does not match recomputed total")


def _check_serialized_switches(
    report: ValidationReport,
    strategy: Mapping[str, Any],
    sid: str,
    expected: Sequence[Mapping[str, Any]],
) -> None:
    switches = strategy.get("switches")
    if not isinstance(switches, Mapping) or sid not in switches:
        report.error(f"{sid}: strategy.switches lacks switch locations")
        return
    actual = switches[sid]
    if not isinstance(actual, Sequence):
        report.error(f"{sid}: switches must be a list")
        return
    if len(actual) != len(expected):
        report.error(
            f"{sid}: expected {len(expected)} switch/probe points, found {len(actual)}"
        )
        return

    fields = (
        "location",
        "after_step",
        "subset",
        "observed_rows",
        "causes_switch",
        "selected_next_node",
    )
    for i, (got, wanted) in enumerate(zip(actual, expected)):
        if not isinstance(got, Mapping):
            report.error(f"{sid} switch {i} must be an object")
            continue
        for field_name in fields:
            if field_name not in wanted:
                continue
            if got.get(field_name) != wanted[field_name]:
                report.error(
                    f"{sid} switch {i} field {field_name}: "
                    f"reported {got.get(field_name)!r}, expected {wanted[field_name]!r}"
                )


def verify_files(
    problem_path: str | Path,
    strategy_path: str | Path,
    *,
    check_optimal: bool = False,
) -> ValidationReport:
    from .model import load_problem

    problem = load_problem(problem_path)
    try:
        strategy = json.loads(Path(strategy_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        report = ValidationReport()
        report.error(f"cannot read strategy file: {exc}")
        return report
    return verify_strategy(problem, strategy, check_optimal=check_optimal)
