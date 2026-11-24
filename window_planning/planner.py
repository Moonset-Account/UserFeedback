"""Physical window planner.

The planner searches execution orders that respect window dependencies and
minimises the total *real* sorting fee. With at most eight windows the search
is exact: a subset DP over executed window masks, where each DP state also
remembers the concrete current row permutation and physical key.

Remembering the concrete permutation matters because the number of Timsort
comparisons of an in-group stable sort depends on the order inherited from
previous operations -- an abstract "sorted by columns X" state would lose
that information and could mis-price plans.

Three kinds of transitions exist for every executable window:

* **REUSE**: the current physical order already serves the window -- zero fee;
* **PARTIAL**: partition columns already form a contiguous prefix, so each
  prefix group is stable-sorted separately;
* **FULL**: a full stable sort under one of the window's candidate keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .cost import FeeSchedule
from .logical import materialise_logical
from .physical import (
    INITIAL_KEY,
    PhysKey,
    candidate_keys,
    can_reuse,
    iter_physkeys,
    measured_partial_sort,
    measured_stable_sort,
    partial_sort_key,
    partition_prefix_match,
)
from .spec import ROWNO_COLUMN, TiePolicy, WindowSpec
from .validation import SpecError, validate_specs


class StepKind(Enum):
    REUSE = "REUSE"
    PARTIAL = "PARTIAL"
    FULL = "FULL"


@dataclass(frozen=True)
class PlanStep:
    """One executed window inside a plan."""

    window: str
    kind: StepKind
    physical_key: PhysKey
    #: Why REUSE was legal, or which prefix a PARTIAL sort reused.
    reuse_basis: str
    comparisons: int
    group_sizes: tuple[int, ...]
    fee: int

    def describe(self) -> str:
        key_text = self.physical_key.describe()
        if self.kind is StepKind.REUSE:
            return (
                f"{self.window}: REUSE existing order [{key_text}] "
                f"-- {self.reuse_basis}; fee=0"
            )
        groups = ",".join(str(size) for size in self.group_sizes) or "-"
        return (
            f"{self.window}: {self.kind.value} sort on [{key_text}] "
            f"-- {self.reuse_basis}; comparisons={self.comparisons}, "
            f"group_sizes=({groups}), fee={self.fee}"
        )


@dataclass
class WindowPlan:
    """The minimum-fee execution plan and its search evidence."""

    steps: list[PlanStep]
    total_fee: int
    execution_order: tuple[str, ...]
    final_physical_key: PhysKey
    states_visited: int
    transitions_tried: int
    fee_schedule: FeeSchedule
    dependencies: dict[str, list[str]] = field(default_factory=dict)

    def describe(self) -> str:
        lines = [
            "execution order: " + " -> ".join(self.execution_order),
            f"total sorting fee: {self.total_fee}",
            "steps:",
        ]
        lines.extend(f"  {i + 1}. {step.describe()}" for i, step in enumerate(self.steps))
        lines.append(
            f"(DP states visited: {self.states_visited}, "
            f"transitions tried: {self.transitions_tried})"
        )
        return "\n".join(lines)


#: Safety valve; with eight windows real state counts stay far below this.
_MAX_DP_STATES = 200_000


@dataclass(frozen=True)
class _Node:
    mask: int
    key: PhysKey
    permutation: tuple[int, ...]


def plan_windows(
    rows: list[dict],
    specs: list[WindowSpec],
    fee_schedule: FeeSchedule,
) -> WindowPlan:
    """Find the minimum-fee plan for ``specs`` over already-loaded ``rows``.

    ``rows`` must come from :func:`~window_planning.validation.load_rows` and
    therefore contain the reserved row-number column. The planner works on a
    shallow copy of the rows on which every generated column is precomputed in
    topological order: logical values are path-independent, and having them
    available lets the DP measure sorts whose keys reference window outputs.
    """
    base_columns = frozenset(rows[0]) - {ROWNO_COLUMN} if rows else frozenset()
    dependencies = validate_specs(specs, base_columns)
    working_rows = [dict(row) for row in rows]
    materialise_logical(working_rows, specs)

    if not working_rows:
        # An empty table needs no physical work at all: every window reuses
        # the (empty) current order at zero fee, in dependency-valid order.
        from .validation import topo_order

        empty_steps = [
            PlanStep(
                window=spec.name,
                kind=StepKind.REUSE,
                physical_key=INITIAL_KEY,
                reuse_basis="empty table: the empty order serves every window",
                comparisons=0,
                group_sizes=(),
                fee=0,
            )
            for spec in topo_order(specs, dependencies)
        ]
        return WindowPlan(
            steps=empty_steps,
            total_fee=0,
            execution_order=tuple(spec.name for spec in topo_order(specs, dependencies)),
            final_physical_key=INITIAL_KEY,
            states_visited=1,
            transitions_tried=0,
            fee_schedule=fee_schedule,
            dependencies=dependencies,
        )

    dep_mask = {
        spec.name: _dependency_mask(spec, specs, dependencies) for spec in specs
    }
    index_of = {spec.name: i for i, spec in enumerate(specs)}
    full_mask = (1 << len(specs)) - 1

    initial_perm = tuple(range(len(working_rows)))
    start = _Node(mask=0, key=INITIAL_KEY, permutation=initial_perm)

    # Best fee and back-pointer per concrete node.
    best: dict[_Node, int] = {start: 0}
    parent: dict[_Node, tuple[_Node | None, PlanStep | None]] = {
        start: (None, None)
    }
    states_visited = 0
    transitions_tried = 0

    # Nodes are expanded in non-decreasing mask size, so every predecessor is
    # settled before a successor is relaxed.
    frontier = [start]
    while frontier:
        for node in frontier:
            states_visited += 1
            if len(best) > _MAX_DP_STATES:  # pragma: no cover - defensive cap
                raise SpecError("planner state space exceeded safety limit")
            executable = [
                spec
                for spec in specs
                if not (node.mask >> index_of[spec.name]) & 1
                and node.mask & dep_mask[spec.name] == dep_mask[spec.name]
            ]
            for spec in executable:
                bit = 1 << index_of[spec.name]
                new_mask = node.mask | bit

                # 1) Reuse the current physical order for free.
                if can_reuse(node.key, spec):
                    transitions_tried += 1
                    step = PlanStep(
                        window=spec.name,
                        kind=StepKind.REUSE,
                        physical_key=node.key,
                        reuse_basis=_reuse_reason(node.key, spec),
                        comparisons=0,
                        group_sizes=(),
                        fee=0,
                    )
                    target = _Node(new_mask, node.key, node.permutation)
                    _relax(best, parent, node, target, step)

                # 2) Partial stable sort inside contiguous partition groups.
                if partition_prefix_match(node.key, spec):
                    transitions_tried += 1
                    sort_key = partial_sort_key(node.key, spec)
                    measurement = measured_partial_sort(
                        working_rows,
                        node.permutation,
                        tuple(spec.partition_by),
                        sort_key,
                    )
                    fee = fee_schedule.price(measurement)
                    step = PlanStep(
                        window=spec.name,
                        kind=StepKind.PARTIAL,
                        physical_key=sort_key,
                        reuse_basis=(
                            "partition prefix "
                            f"{tuple(spec.partition_by)} already contiguous; "
                            "groups keep their positions"
                        ),
                        comparisons=measurement.comparisons,
                        group_sizes=measurement.group_sizes,
                        fee=fee,
                    )
                    target = _Node(new_mask, sort_key, measurement.permutation)
                    _relax(best, parent, node, target, step)

                # 3) Full stable sort under each candidate physical key.
                for candidate in iter_physkeys(candidate_keys(spec)):
                    transitions_tried += 1
                    measurement = measured_stable_sort(
                        working_rows, node.permutation, candidate
                    )
                    fee = fee_schedule.price(measurement)
                    step = PlanStep(
                        window=spec.name,
                        kind=StepKind.FULL,
                        physical_key=candidate,
                        reuse_basis="no reusable prefix; full stable sort",
                        comparisons=measurement.comparisons,
                        group_sizes=measurement.group_sizes,
                        fee=fee,
                    )
                    target = _Node(new_mask, candidate, measurement.permutation)
                    _relax(best, parent, node, target, step)

        # Rebuild the next frontier strictly from the best-table so that each
        # concrete node is expanded exactly once at its mask level.
        next_size = node.mask.bit_count() + 1
        frontier = sorted(
            (candidate for candidate in best if candidate.mask.bit_count() == next_size),
            key=lambda candidate: (candidate.permutation, candidate.key.describe()),
        )
        if not frontier:
            break

    finals = [node for node in best if node.mask == full_mask]
    if not finals:  # pragma: no cover - validation already rejects cycles
        raise SpecError("no feasible execution order found")
    winner = min(finals, key=lambda node: (best[node], node.key.describe()))

    chain: list[PlanStep] = []
    cursor: _Node | None = winner
    while cursor is not None:
        previous, step = parent[cursor]
        if step is not None:
            chain.append(step)
        cursor = previous
    chain.reverse()

    return WindowPlan(
        steps=chain,
        total_fee=best[winner],
        execution_order=tuple(step.window for step in chain),
        final_physical_key=winner.key,
        states_visited=states_visited,
        transitions_tried=transitions_tried,
        fee_schedule=fee_schedule,
        dependencies=dependencies,
    )


def _relax(
    best: dict[_Node, int],
    parent: dict[_Node, tuple[_Node | None, PlanStep | None]],
    node: _Node,
    target: _Node,
    step: PlanStep,
) -> None:
    candidate_fee = best[node] + step.fee
    if target not in best or candidate_fee < best[target]:
        best[target] = candidate_fee
        parent[target] = (node, step)


def _dependency_mask(
    spec: WindowSpec,
    specs: list[WindowSpec],
    dependencies: dict[str, list[str]],
) -> int:
    mask = 0
    for dep in dependencies[spec.name]:
        for i, other in enumerate(specs):
            if other.name == dep:
                mask |= 1 << i
    return mask


def _reuse_reason(key: PhysKey, spec: WindowSpec) -> str:
    parts = [
        "partition prefix "
        f"{tuple(spec.partition_by) or '()'} contiguous in current order"
    ]
    if spec.order_by:
        parts.append(
            "order suffix "
            + ", ".join(order_key.describe() for order_key in spec.order_by)
            + " already satisfied"
        )
    else:
        parts.append("window declares no ordering keys")
    if spec.tie_policy is TiePolicy.ROW_NUMBER:
        parts.append("row-number tail preserves a deterministic total order")
    else:
        parts.append("logical ties stay inside one equal-value group")
    return "; ".join(parts)
