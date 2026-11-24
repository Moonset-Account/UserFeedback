"""Exact belief-state optimizer for adaptive join strategies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import Block, PlannerError, Problem, ordering_covers


class NoFeasiblePlanError(PlannerError):
    """Raised when no memory/edge/algorithm-feasible strategy exists."""


@dataclass(frozen=True)
class Action:
    kind: str  # "join" or "materialize"
    cost_vector: tuple[int, ...]
    memory_vector: tuple[int, ...]
    mask: int | None = None
    left_mask: int | None = None
    right_mask: int | None = None
    output_mask: int | None = None
    algorithm: str | None = None
    edge_id: str | None = None
    sort_left: bool = False
    sort_right: bool = False
    memory_limit: int | None = None
    join_cost_vector: tuple[int, ...] | None = None
    join_memory_vector: tuple[int, ...] | None = None
    sort_left_cost_vector: tuple[int, ...] | None = None
    sort_right_cost_vector: tuple[int, ...] | None = None
    sort_left_memory_vector: tuple[int, ...] | None = None
    sort_right_memory_vector: tuple[int, ...] | None = None


@dataclass
class BranchSolution:
    observation: int
    scenarios: tuple[int, ...]
    child: "NodeSolution"


@dataclass
class NodeSolution:
    forest: tuple[Block, ...]
    belief: tuple[int, ...]
    sunk: tuple[int, ...]
    action: Action | None
    future_sum: int
    child: "NodeSolution | None" = None
    branches: tuple[BranchSolution, ...] = ()


@dataclass
class RootBranch:
    observations: tuple[tuple[int, int], ...]
    scenarios: tuple[int, ...]
    node: NodeSolution


@dataclass
class RootSolution:
    branches: tuple[RootBranch, ...]


def _zero_vector(n: int) -> list[int]:
    return [0 for _ in range(n)]


def action_signature(action: Action | None) -> tuple[Any, ...]:
    if action is None:
        return ("terminal",)
    return (
        action.kind,
        action.mask,
        action.left_mask,
        action.right_mask,
        action.output_mask,
        action.algorithm,
        action.edge_id,
        action.sort_left,
        action.sort_right,
    )


def action_label(problem: Problem, action: Action | None) -> str:
    if action is None:
        return "terminal"
    if action.kind == "materialize":
        return f"materialize_and_observe({problem.subset_key(action.mask or 0)})"
    assert action.output_mask is not None
    bits = [problem.subset_key(action.left_mask or 0), problem.subset_key(action.right_mask or 0)]
    if action.left_mask and action.right_mask:
        left_block_key = problem.subset_key(action.left_mask)
        right_block_key = problem.subset_key(action.right_mask)
        bits = [left_block_key, right_block_key]
    sort_note = ""
    if action.algorithm == "merge":
        notes = []
        notes.append("sort-left" if action.sort_left else "reuse-left-order")
        notes.append("sort-right" if action.sort_right else "reuse-right-order")
        sort_note = " [" + ", ".join(notes) + "]"
    return (
        f"{action.algorithm}_join({bits[0]}, {bits[1]}) "
        f"via {action.edge_id} -> {problem.subset_key(action.output_mask)}{sort_note}"
    )


class Optimizer:
    def __init__(self, problem: Problem, *, allow_materialization: bool = True) -> None:
        self.p = problem
        self.k = len(problem.scenarios)
        self.allow_materialization = allow_materialization
        self._memo: dict[tuple[Any, ...], NodeSolution | None] = {}

    # ------------------------------------------------------------------
    # Root / initial observations
    # ------------------------------------------------------------------

    def initial_signature(self, scenario_index: int) -> tuple[tuple[int, int], ...]:
        p = self.p
        sig: list[tuple[int, int]] = []
        for block in p.initial_blocks:
            if block.materialized:
                sig.append((block.mask, p.row_count(scenario_index, block.mask)))
        return tuple(sorted(sig))

    def solve_root(self, cap: int | None) -> RootSolution | None:
        groups: dict[tuple[tuple[int, int], ...], list[int]] = {}
        for si in range(self.k):
            sig = self.initial_signature(si)
            groups.setdefault(sig, []).append(si)

        branches: list[RootBranch] = []
        sunk = tuple(self.p.initial_sunk)
        for sig, indices in sorted(groups.items(), key=lambda kv: kv[0]):
            belief = tuple(sorted(indices))
            node = self._state(tuple(self.p.initial_blocks), belief, sunk, cap)
            if node is None:
                return None
            branches.append(RootBranch(observations=sig, scenarios=belief, node=node))
        return RootSolution(tuple(branches))

    # ------------------------------------------------------------------
    # Belief-state DP
    # ------------------------------------------------------------------

    def _state(
        self,
        forest: tuple[Block, ...],
        belief: tuple[int, ...],
        sunk: tuple[int, ...],
        cap: int | None,
    ) -> NodeSolution | None:
        key = (forest, belief, sunk, cap, self.allow_materialization)
        if key in self._memo:
            return self._memo[key]

        if cap is not None and any(sunk[si] > cap for si in belief):
            self._memo[key] = None
            return None

        if len(forest) == 1:
            if forest[0].mask != self.p.all_mask:
                self._memo[key] = None
                return None
            sol = NodeSolution(
                forest=forest,
                belief=belief,
                sunk=sunk,
                action=None,
                future_sum=0,
            )
            self._memo[key] = sol
            return sol

        candidates: list[tuple[int, tuple[Any, ...], NodeSolution]] = []

        for action in self._materialize_actions(forest, belief):
            sunk2 = self._add_costs(sunk, belief, action, cap)
            if sunk2 is None:
                continue
            assert action.mask is not None
            new_forest = tuple(
                sorted(
                    (
                        Block(
                            mask=b.mask,
                            ordering=b.ordering,
                            materialized=True if b.mask == action.mask else b.materialized,
                            memory_limit=b.memory_limit,
                            reuse_order=b.reuse_order,
                        )
                        if b.mask == action.mask
                        else b
                        for b in forest
                    ),
                    key=lambda b: b.mask,
                )
            )
            groups: dict[int, list[int]] = {}
            for si in belief:
                obs = self.p.row_count(si, action.mask)
                groups.setdefault(obs, []).append(si)

            branch_solutions: list[BranchSolution] = []
            child_sum = 0
            possible = True
            for obs, indices in sorted(groups.items()):
                child_belief = tuple(sorted(indices))
                child = self._state(new_forest, child_belief, sunk2, cap)
                if child is None:
                    possible = False
                    break
                child_sum += child.future_sum
                branch_solutions.append(
                    BranchSolution(obs, child_belief, child)
                )
            if not possible:
                continue

            immediate_sum = sum(action.cost_vector[si] for si in belief)
            total = immediate_sum + child_sum
            temp = NodeSolution(
                forest=forest,
                belief=belief,
                sunk=sunk,
                action=action,
                future_sum=total,
                branches=tuple(branch_solutions),
            )
            candidates.append((total, self._tie_key(action), temp))

        for action, new_forest in self._join_actions(forest, belief):
            sunk2 = self._add_costs(sunk, belief, action, cap)
            if sunk2 is None:
                continue
            child = self._state(new_forest, belief, sunk2, cap)
            if child is None:
                continue
            immediate_sum = sum(action.cost_vector[si] for si in belief)
            total = immediate_sum + child.future_sum
            temp = NodeSolution(
                forest=forest,
                belief=belief,
                sunk=sunk,
                action=action,
                future_sum=total,
                child=child,
            )
            candidates.append((total, self._tie_key(action), temp))

        if not candidates:
            self._memo[key] = None
            return None

        candidates.sort(key=lambda x: (x[0], x[1]))
        best = candidates[0][2]
        self._memo[key] = best
        return best

    @staticmethod
    def _add_costs(
        sunk: tuple[int, ...],
        belief: tuple[int, ...],
        action: Action,
        cap: int | None,
    ) -> tuple[int, ...] | None:
        sunk2 = list(sunk)
        for si in belief:
            sunk2[si] += action.cost_vector[si]
            if cap is not None and sunk2[si] > cap:
                return None
        return tuple(sunk2)

    @staticmethod
    def _tie_key(action: Action) -> tuple[Any, ...]:
        return (
            0 if action.kind == "materialize" else 1,
            action.mask or 0,
            action.left_mask or 0,
            action.right_mask or 0,
            action.edge_id or "",
            action.algorithm or "",
            action.sort_left,
            action.sort_right,
        )

    # ------------------------------------------------------------------
    # Action enumeration
    # ------------------------------------------------------------------

    def _materialize_actions(
        self, forest: tuple[Block, ...], belief: tuple[int, ...]
    ) -> list[Action]:
        if not self.allow_materialization:
            return []
        p = self.p
        result: list[Action] = []
        for block in forest:
            if block.materialized:
                continue
            costs = _zero_vector(self.k)
            memories = _zero_vector(self.k)
            feasible = True
            for si in belief:
                rows = p.row_count(si, block.mask)
                costs[si] = p.materialization_cost.cost(rows)
                memories[si] = p.materialization_cost.memory(rows)
                if block.memory_limit is not None and memories[si] > block.memory_limit:
                    feasible = False
                    break
            if feasible:
                result.append(
                    Action(
                        kind="materialize",
                        mask=block.mask,
                        cost_vector=tuple(costs),
                        memory_vector=tuple(memories),
                        memory_limit=block.memory_limit,
                    )
                )
        return result

    def _orient_edge(
        self, a: Block, b: Block
    ) -> list[tuple[Block, Block, Any]]:
        p = self.p
        oriented: list[tuple[Block, Block, Any]] = []
        for edge in p.edges:
            if a.mask & edge.left_bit and b.mask & edge.right_bit:
                oriented.append((a, b, edge))
            elif a.mask & edge.right_bit and b.mask & edge.left_bit:
                # Block containing edge.left is b in this orientation.
                oriented.append((b, a, edge))
        return oriented

    def _join_actions(
        self, forest: tuple[Block, ...], belief: tuple[int, ...]
    ) -> list[tuple[Action, tuple[Block, ...]]]:
        p = self.p
        out: list[tuple[Action, tuple[Block, ...]]] = []

        for i in range(len(forest)):
            for j in range(i + 1, len(forest)):
                a, b = forest[i], forest[j]
                if a.mask & b.mask:
                    continue
                for left_block, right_block, edge in self._orient_edge(a, b):
                    out.extend(
                        self._hash_candidates(
                            forest, belief, left_block, right_block, edge
                        )
                    )
                    out.extend(
                        self._merge_candidates(
                            forest, belief, left_block, right_block, edge
                        )
                    )
        return out

    def _join_limit(self, edge: Any, output_mask: int) -> int | None:
        if edge.memory_limit is not None:
            return edge.memory_limit
        if output_mask in self.p.memory_limits:
            return self.p.memory_limits[output_mask]
        return self.p.default_memory_limit

    def _hash_candidates(
        self,
        forest: tuple[Block, ...],
        belief: tuple[int, ...],
        left_block: Block,
        right_block: Block,
        edge: Any,
    ) -> list[tuple[Action, tuple[Block, ...]]]:
        p = self.p
        output_mask = left_block.mask | right_block.mask
        limit = self._join_limit(edge, output_mask)
        costs = _zero_vector(self.k)
        memory = _zero_vector(self.k)
        feasible = True
        for si in belief:
            lr = p.row_count(si, left_block.mask)
            rr = p.row_count(si, right_block.mask)
            orow = p.row_count(si, output_mask)
            point = p.hash_cost.lookup(lr, rr, orow)
            costs[si] = point.cost
            memory[si] = point.memory
            if limit is not None and point.memory > limit:
                feasible = False
                break
        if not feasible:
            return []

        action = Action(
            kind="join",
            algorithm="hash",
            edge_id=edge.eid,
            left_mask=left_block.mask,
            right_mask=right_block.mask,
            output_mask=output_mask,
            cost_vector=tuple(costs),
            memory_vector=tuple(memory),
            memory_limit=limit,
            join_cost_vector=tuple(costs),
            join_memory_vector=tuple(memory),
        )
        out_block = Block(
            mask=output_mask,
            ordering=(),
            materialized=False,
            memory_limit=p.limit_for(output_mask),
        )
        new_forest = tuple(
            sorted(
                [
                    b
                    for b in forest
                    if b.mask not in (left_block.mask, right_block.mask)
                ]
                + [out_block],
                key=lambda b: b.mask,
            )
        )
        return [(action, new_forest)]

    def _merge_candidates(
        self,
        forest: tuple[Block, ...],
        belief: tuple[int, ...],
        left_block: Block,
        right_block: Block,
        edge: Any,
    ) -> list[tuple[Action, tuple[Block, ...]]]:
        p = self.p
        left_order_reusable = p.reuse_existing_order and left_block.reuse_order
        right_order_reusable = p.reuse_existing_order and right_block.reuse_order
        sort_left = not (
            left_order_reusable and ordering_covers(left_block.ordering, edge.left_keys)
        )
        sort_right = not (
            right_order_reusable and ordering_covers(right_block.ordering, edge.right_keys)
        )
        if (sort_left or sort_right) and p.sort_cost is None:
            return []

        output_mask = left_block.mask | right_block.mask
        limit = self._join_limit(edge, output_mask)
        total_costs = _zero_vector(self.k)
        join_costs = _zero_vector(self.k)
        join_memory = _zero_vector(self.k)
        sl_cost = _zero_vector(self.k) if sort_left else None
        sr_cost = _zero_vector(self.k) if sort_right else None
        sl_mem = _zero_vector(self.k) if sort_left else None
        sr_mem = _zero_vector(self.k) if sort_right else None
        max_memory = _zero_vector(self.k)

        feasible = True
        for si in belief:
            lr = p.row_count(si, left_block.mask)
            rr = p.row_count(si, right_block.mask)
            orow = p.row_count(si, output_mask)
            point = p.merge_cost.lookup(lr, rr, orow)
            join_costs[si] = point.cost
            join_memory[si] = point.memory
            total_costs[si] = point.cost
            max_memory[si] = point.memory
            if limit is not None and point.memory > limit:
                feasible = False
                break

            if sort_left:
                assert p.sort_cost is not None and sl_cost is not None and sl_mem is not None
                smem = p.sort_cost.memory(lr)
                scost = p.sort_cost.cost(lr)
                sl_cost[si] = scost
                sl_mem[si] = smem
                total_costs[si] += scost
                max_memory[si] = max(max_memory[si], smem)
                if left_block.memory_limit is not None and smem > left_block.memory_limit:
                    feasible = False
                    break
            if sort_right:
                assert p.sort_cost is not None and sr_cost is not None and sr_mem is not None
                smem = p.sort_cost.memory(rr)
                scost = p.sort_cost.cost(rr)
                sr_cost[si] = scost
                sr_mem[si] = smem
                total_costs[si] += scost
                max_memory[si] = max(max_memory[si], smem)
                if right_block.memory_limit is not None and smem > right_block.memory_limit:
                    feasible = False
                    break

        if not feasible:
            return []

        output_order = edge.output_order or edge.left_keys
        action = Action(
            kind="join",
            algorithm="merge",
            edge_id=edge.eid,
            left_mask=left_block.mask,
            right_mask=right_block.mask,
            output_mask=output_mask,
            sort_left=sort_left,
            sort_right=sort_right,
            cost_vector=tuple(total_costs),
            memory_vector=tuple(max_memory),
            memory_limit=limit,
            join_cost_vector=tuple(join_costs),
            join_memory_vector=tuple(join_memory),
            sort_left_cost_vector=None if sl_cost is None else tuple(sl_cost),
            sort_right_cost_vector=None if sr_cost is None else tuple(sr_cost),
            sort_left_memory_vector=None if sl_mem is None else tuple(sl_mem),
            sort_right_memory_vector=None if sr_mem is None else tuple(sr_mem),
        )
        out_block = Block(
            mask=output_mask,
            ordering=tuple(output_order),
            materialized=False,
            memory_limit=p.limit_for(output_mask),
        )
        new_forest = tuple(
            sorted(
                [
                    b
                    for b in forest
                    if b.mask not in (left_block.mask, right_block.mask)
                ]
                + [out_block],
                key=lambda b: b.mask,
            )
        )
        return [(action, new_forest)]


# ---------------------------------------------------------------------------
# Cost simulation and serialization
# ---------------------------------------------------------------------------


def node_totals(node: NodeSolution, k: int) -> tuple[int, ...]:
    totals = [0] * k

    def walk(n: NodeSolution) -> None:
        if n.action is None:
            return
        for si in n.belief:
            totals[si] += n.action.cost_vector[si]
        if n.child is not None:
            walk(n.child)
        for branch in n.branches:
            walk(branch.child)

    walk(node)
    return tuple(totals)


def root_totals(problem: Problem, root: RootSolution) -> tuple[int, ...]:
    totals = list(problem.initial_sunk)
    for rb in root.branches:
        future = node_totals(rb.node, len(problem.scenarios))
        for si in rb.scenarios:
            totals[si] += future[si]
    return tuple(totals)


def plan_strategy(
    problem: Problem,
    *,
    allow_materialization: bool = True,
) -> dict[str, Any]:
    """Return the lexicographically optimal ``(worst_cost, cost_sum)`` tree."""

    opt = Optimizer(problem, allow_materialization=allow_materialization)
    unbounded = opt.solve_root(None)
    if unbounded is None:
        raise NoFeasiblePlanError(
            "no feasible join strategy under the supplied edges and memory limits"
        )

    unbounded_totals = root_totals(problem, unbounded)
    upper = max(unbounded_totals)

    lo = -1
    hi = upper
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        candidate = opt.solve_root(mid)
        if candidate is not None:
            hi = mid
        else:
            lo = mid

    best = opt.solve_root(hi)
    if best is None:  # pragma: no cover - defensive, hi came from feasible plan
        raise NoFeasiblePlanError("optimizer lost the feasible strategy")

    totals = root_totals(problem, best)
    return _serialize_strategy(
        problem,
        best,
        worst=max(totals),
        total_sum=sum(totals),
        totals=totals,
        allow_materialization=allow_materialization,
    )


def _block_dict(problem: Problem, block: Block) -> dict[str, Any]:
    return {
        "subset": problem.subset_key(block.mask),
        "mask": block.mask,
        "materialized": block.materialized,
        "ordering": list(block.ordering),
        "memory_limit": block.memory_limit,
        "reuse_order": block.reuse_order,
    }


def _cost_map(problem: Problem, vector: tuple[int, ...] | None, belief: tuple[int, ...]) -> dict[str, int] | None:
    if vector is None:
        return None
    return {problem.scenarios[si].sid: vector[si] for si in belief}


def _serialize_action(
    problem: Problem, action: Action, belief: tuple[int, ...]
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": action.kind,
        "label": action_label(problem, action),
        "total_cost": _cost_map(problem, action.cost_vector, belief),
        "required_memory": _cost_map(problem, action.memory_vector, belief),
        "memory_limit": action.memory_limit,
    }
    if action.kind == "materialize":
        data["subset"] = problem.subset_key(action.mask or 0)
        data["mask"] = action.mask
        data["observation"] = "row_count"
    else:
        data.update(
            {
                "algorithm": action.algorithm,
                "edge_id": action.edge_id,
                "left_subset": problem.subset_key(action.left_mask or 0),
                "right_subset": problem.subset_key(action.right_mask or 0),
                "output_subset": problem.subset_key(action.output_mask or 0),
                "left_mask": action.left_mask,
                "right_mask": action.right_mask,
                "output_mask": action.output_mask,
                "sort_left_required": action.sort_left,
                "sort_right_required": action.sort_right,
                "reused_existing_order": {
                    "left": action.algorithm != "merge" or not action.sort_left,
                    "right": action.algorithm != "merge" or not action.sort_right,
                },
                "join_cost": _cost_map(problem, action.join_cost_vector, belief),
                "join_memory": _cost_map(problem, action.join_memory_vector, belief),
                "sort_left_cost": _cost_map(
                    problem, action.sort_left_cost_vector, belief
                )
                or {problem.scenarios[si].sid: 0 for si in belief},
                "sort_right_cost": _cost_map(
                    problem, action.sort_right_cost_vector, belief
                )
                or {problem.scenarios[si].sid: 0 for si in belief},
            }
        )
    return data


def _serialize_strategy(
    problem: Problem,
    root: RootSolution,
    *,
    worst: int,
    total_sum: int,
    totals: tuple[int, ...],
    allow_materialization: bool,
) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    id_map: dict[int, str] = {}
    counter = 0

    def node_id(node: NodeSolution) -> str:
        nonlocal counter
        oid = id(node)
        if oid not in id_map:
            counter += 1
            nid = f"n{counter}"
            id_map[oid] = nid
            _emit_node(nid, node)
        return id_map[oid]

    def _emit_node(nid: str, node: NodeSolution) -> None:
        base: dict[str, Any] = {
            "id": nid,
            "scenarios": [problem.scenarios[si].sid for si in node.belief],
            "state": {
                "frontier": [_block_dict(problem, b) for b in node.forest],
            },
            "future_cost_sum_from_here": node.future_sum,
        }
        if node.action is None:
            base["kind"] = "terminal"
            base["action"] = None
            nodes[nid] = base
            return

        base["kind"] = "decision"
        base["action"] = _serialize_action(problem, node.action, node.belief)
        # Insert before recursively emitting children so JSON node order follows
        # the strategy tree from parent to child.
        nodes[nid] = base
        if node.child is not None:
            base["next_node"] = node_id(node.child)
            base["branches"] = []
        else:
            base["next_node"] = None
            base["branches"] = [
                {
                    "observed_rows": br.observation,
                    "scenarios": [problem.scenarios[si].sid for si in br.scenarios],
                    "next_node": node_id(br.child),
                }
                for br in node.branches
            ]

    root_id = "root"
    root_branches: list[dict[str, Any]] = []
    scenario_root_branch: dict[int, RootBranch] = {}
    for rb in root.branches:
        nid = node_id(rb.node)
        observations = [
            {
                "subset": problem.subset_key(mask),
                "mask": mask,
                "observed_rows": rows,
            }
            for mask, rows in rb.observations
        ]
        sig_key = ";".join(f"{mask}:{rows}" for mask, rows in rb.observations) or "no-initial-observation"
        root_branches.append(
            {
                "signature": sig_key,
                "observations": observations,
                "scenarios": [problem.scenarios[si].sid for si in rb.scenarios],
                "next_node": nid,
            }
        )
        for si in rb.scenarios:
            scenario_root_branch[si] = rb

    nodes[root_id] = {
        "id": root_id,
        "kind": "root",
        "branches": root_branches,
    }

    paths, switches = _build_paths_and_switches(
        problem, root, id_map, scenario_root_branch
    )

    return {
        "schema_version": "adaptive-plan-1.0",
        "status": "optimal",
        "materialization_allowed": allow_materialization,
        "objective": {
            "worst_total_cost": worst,
            "sum_of_scenario_total_costs": total_sum,
            "scenario_total_costs": {
                problem.scenarios[si].sid: totals[si]
                for si in range(len(problem.scenarios))
            },
        },
        "root_node": root_id,
        "nodes": nodes,
        "paths": paths,
        "switches": switches,
    }


def _find_branch_for_scenario(
    root: RootSolution, si: int
) -> RootBranch:
    for rb in root.branches:
        if si in rb.scenarios:
            return rb
    raise PlannerError(f"scenario index {si} absent from root branches")


def _build_paths_and_switches(
    problem: Problem,
    root: RootSolution,
    id_map: dict[int, str],
    scenario_root_branch: dict[int, RootBranch],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    paths: dict[str, list[dict[str, Any]]] = {}
    switches: dict[str, list[dict[str, Any]]] = {}

    # Initial-observation switch summary.
    initial_next_actions: list[tuple[tuple[int, int], tuple[Any, ...], str]] = []
    for rb in root.branches:
        sig = rb.observations
        sig_label = ";".join(f"{problem.subset_key(m)}={rows}" for m, rows in sig) or "none"
        initial_next_actions.append((sig, action_signature(rb.node.action), sig_label))
    initial_causes_switch = len({sig_act[1] for sig_act in initial_next_actions}) > 1

    for si, scenario in enumerate(problem.scenarios):
        sid = scenario.sid
        rb = scenario_root_branch.get(si) or _find_branch_for_scenario(root, si)
        cumulative = problem.initial_sunk[si]
        observations_so_far = [
            {
                "stage": "initial",
                "subset": problem.subset_key(mask),
                "observed_rows": rows,
            }
            for mask, rows in rb.observations
        ]
        path: list[dict[str, Any]] = []
        scenario_switches: list[dict[str, Any]] = []

        if rb.observations:
            scenario_switches.append(
                {
                    "location": "initial_materialized_block",
                    "after_step": 0,
                    "observations": observations_so_far,
                    "causes_switch": initial_causes_switch,
                    "selected_next_node": id_map[id(rb.node)],
                }
            )

        node = rb.node
        step_no = 0
        while True:
            nid = id_map[id(node)]
            if node.action is None:
                path.append(
                    {
                        "step": step_no + 1,
                        "kind": "terminal",
                        "node_id": nid,
                        "subset": problem.subset_key(problem.all_mask),
                        "cumulative_cost": cumulative,
                    }
                )
                break

            action = node.action
            step_no += 1
            cost = action.cost_vector[si]
            cumulative += cost
            actual_output_rows = None
            if action.output_mask is not None:
                actual_output_rows = problem.row_count(si, action.output_mask)

            observed_rows: int | None = None
            next_node_id: str | None = None
            branch_info: dict[str, Any] | None = None
            if action.kind == "materialize":
                observed_rows = problem.row_count(si, action.mask or 0)
                observations_so_far.append(
                    {
                        "stage": "materialize",
                        "subset": problem.subset_key(action.mask or 0),
                        "observed_rows": observed_rows,
                    }
                )
                selected_branch = next(
                    br for br in node.branches if si in br.scenarios
                )
                next_node_id = id_map[id(selected_branch.child)]
                sibling_actions = [
                    (
                        br.observation,
                        action_signature(br.child.action),
                        id_map[id(br.child)],
                        action_label(problem, br.child.action),
                    )
                    for br in node.branches
                ]
                causes_switch = len({item[1] for item in sibling_actions}) > 1
                branch_info = {
                    "after_step": step_no,
                    "location": "after_materialization",
                    "node_id": nid,
                    "subset": problem.subset_key(action.mask or 0),
                    "observed_rows": observed_rows,
                    "causes_switch": causes_switch,
                    "selected_next_node": next_node_id,
                    "branches": [
                        {
                            "observed_rows": obs,
                            "next_node": child_id,
                            "next_action": label,
                        }
                        for obs, _sig, child_id, label in sibling_actions
                    ],
                }
                scenario_switches.append(branch_info)
            else:
                assert node.child is not None
                next_node_id = id_map[id(node.child)]

            path.append(
                {
                    "step": step_no,
                    "kind": action.kind,
                    "label": action_label(problem, action),
                    "node_id": nid,
                    "action": _serialize_action(problem, action, node.belief),
                    "cost": cost,
                    "cumulative_cost": cumulative,
                    "actual_output_rows_offline": actual_output_rows,
                    "observed_rows": observed_rows,
                    "next_node_id": next_node_id,
                    "observations_so_far": [dict(o) for o in observations_so_far],
                }
            )

            if action.kind == "materialize":
                selected_branch = next(br for br in node.branches if si in br.scenarios)
                node = selected_branch.child
            else:
                assert node.child is not None
                node = node.child

        paths[sid] = path
        switches[sid] = scenario_switches

    return paths, switches
