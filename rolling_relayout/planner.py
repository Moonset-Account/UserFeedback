"""BFS-based rolling re-layout planner (minimum-action search).

Search state
------------
A state is a tuple with one :class:`frozenset` of node ids per replica,
listing every node on which that replica currently runs. Surge lets a replica
temporarily run on two nodes at once; an empty frozenset means the replica is
stopped (allowed only while the ready count stays at or above ``min_ready``).

Each transition is exactly one scheduler action:

* ``start`` a replica on an eligible node with a free slot, allowed only when
  both spreading-rule deviation gates pass and the temporary increment cap
  (``desired + max_surge`` running instances) is respected;
* ``stop`` a running instance, allowed only while at least ``min_ready``
  instances remain.

Breadth-first search over these transitions yields the shortest (minimum
number of actions) sequence reaching a balanced layout: every replica runs on
exactly one node and no spreading rule observes a skew above ``max_skew``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence

from .domains import evaluate_domains, evaluate_start, find_balance_violations
from .model import (
    Action,
    DomainStat,
    GateEvaluation,
    Node,
    PlannerInput,
    Replica,
    RolloutPlan,
    RolloutStep,
    SpreadRule,
)

State = tuple[frozenset[str], ...]
MAX_SUPPORTED_NODES = 8


class Planner:
    """Stateful wrapper holding a validated :class:`PlannerInput`."""

    def __init__(self, planner_input: PlannerInput) -> None:
        self.input = planner_input
        if len(planner_input.nodes) > MAX_SUPPORTED_NODES:
            raise ValueError(
                f"exact minimum-action search supports at most {MAX_SUPPORTED_NODES} nodes, "
                f"got {len(planner_input.nodes)}"
            )
        self._node_by_id: dict[str, Node] = {n.node_id: n for n in planner_input.nodes}
        self._initial_occupancy = self._initial_node_occupancy(planner_input)
        self._initial_state = self._build_initial_state(planner_input)
        self._running_cap = len(planner_input.replicas) + planner_input.max_surge

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _build_initial_state(planner_input: PlannerInput) -> State:
        state: list[frozenset[str]] = []
        for replica in planner_input.replicas:
            state.append(frozenset([replica.current_node]) if replica.current_node else frozenset())
        return tuple(state)

    @staticmethod
    def _initial_node_occupancy(planner_input: PlannerInput) -> dict[str, int]:
        occupancy: dict[str, int] = {n.node_id: 0 for n in planner_input.nodes}
        for replica in planner_input.replicas:
            if replica.current_node is not None:
                occupancy[replica.current_node] += 1
        return occupancy

    def _free_slots(self, node_id: str, state: State) -> int:
        node = self._node_by_id[node_id]
        running_on_node = sum(1 for running in state if node_id in running)
        # Free slots shrink relative to the initial layout (which already
        # accounts for the initially running replicas).
        extra = running_on_node - self._initial_occupancy[node_id]
        return node.free_slots - extra

    @staticmethod
    def _running_count(state: State) -> int:
        return sum(len(running) for running in state)

    def _is_balanced(self, state: State) -> bool:
        if any(len(running) != 1 for running in state):
            return False
        for i, replica in enumerate(self.input.replicas):
            node_id = next(iter(state[i]))
            node = self._node_by_id[node_id]
            # The target layout must place every replica on a node satisfying
            # its own condition; grandfathered non-conforming placements are
            # allowed only as intermediate/starting states.
            if not replica.selector.matches(node):
                return False
        placement = {replica.replica_id: state[i] for i, replica in enumerate(self.input.replicas)}
        return not find_balance_violations(
            self.input.nodes, placement, self.input.replicas, self.input.rules
        )

    # ------------------------------------------------------------ transitions
    def _start_actions(self, state: State) -> list[tuple[Action, State, Mapping[str, GateEvaluation]]]:
        out: list[tuple[Action, State, Mapping[str, GateEvaluation]]] = []
        if self._running_count(state) >= self._running_cap:
            return out
        placement = {replica.replica_id: state[i] for i, replica in enumerate(self.input.replicas)}
        for i, replica in enumerate(self.input.replicas):
            for node in self.input.nodes:
                if node.node_id in state[i]:
                    continue
                if self._free_slots(node.node_id, state) <= 0:
                    continue
                if not replica.selector.matches(node):
                    continue
                gates: dict[str, GateEvaluation] = {}
                allowed = True
                for rule in self.input.rules:
                    gate = evaluate_start(
                        self.input.nodes, placement, replica, node, rule
                    )
                    gates[rule.name] = gate
                    if not gate.allowed:
                        allowed = False
                if not allowed:
                    continue
                new_running = set(state[i])
                new_running.add(node.node_id)
                new_state = list(state)
                new_state[i] = frozenset(new_running)
                out.append((Action("start", replica.replica_id, node.node_id), tuple(new_state), gates))
        return out

    def _stop_actions(self, state: State) -> list[tuple[Action, State, Mapping[str, GateEvaluation]]]:
        out: list[tuple[Action, State, Mapping[str, GateEvaluation]]] = []
        if self._running_count(state) <= self.input.min_ready:
            return out
        for i, replica in enumerate(self.input.replicas):
            for node_id in state[i]:
                new_running = set(state[i])
                new_running.remove(node_id)
                new_state = list(state)
                new_state[i] = frozenset(new_running)
                out.append((Action("stop", replica.replica_id, node_id), tuple(new_state), {}))
        return out

    # ------------------------------------------------------------------- search
    def plan(self) -> RolloutPlan:
        initial = self._initial_state
        if self._is_balanced(initial):
            return RolloutPlan(
                actions=(),
                steps=(),
                final_placement=tuple(
                    next(iter(running)) if running else None for running in initial
                ),
                feasible=True,
            )

        visited: set[State] = {initial}
        parents: dict[State, tuple[State, Action]] = {}
        queue: deque[State] = deque([initial])
        goal: State | None = None

        while queue:
            state = queue.popleft()
            # Starts before stops, then replica/node order: deterministic plans.
            transitions = self._start_actions(state) + self._stop_actions(state)
            for action, new_state, _gates in transitions:
                if new_state in visited:
                    continue
                visited.add(new_state)
                parents[new_state] = (state, action)
                if self._is_balanced(new_state):
                    goal = new_state
                    queue.clear()
                    break
                queue.append(new_state)

        if goal is None:
            return RolloutPlan(
                actions=(),
                steps=(),
                final_placement=tuple(
                    next(iter(running)) if running else None for running in initial
                ),
                feasible=False,
            )

        return self._replay(initial, goal, parents)

    def _replay(
        self,
        initial: State,
        goal: State,
        parents: Mapping[State, tuple[State, Action]],
    ) -> RolloutPlan:
        reverse_actions: list[Action] = []
        cursor = goal
        while cursor != initial:
            previous, action = parents[cursor]
            reverse_actions.append(action)
            cursor = previous
        actions = list(reversed(reverse_actions))

        replica_by_id = {r.replica_id: r for r in self.input.replicas}
        state = initial
        steps: list[RolloutStep] = []
        for index, action in enumerate(actions):
            running_before = self._running_count(state)
            placement: dict[str, frozenset[str]] = {
                replica.replica_id: state[i] for i, replica in enumerate(self.input.replicas)
            }
            replica = replica_by_id[action.replica_id]
            stats_before: dict[str, DomainStat] = {}
            for rule in self.input.rules:
                stats_before[rule.name] = evaluate_domains(
                    self.input.nodes, placement, replica, rule
                )
            gates: dict[str, GateEvaluation] = {}
            if action.kind == "start":
                target_node = self._node_by_id[action.node_id]
                for rule in self.input.rules:
                    gates[rule.name] = evaluate_start(
                        self.input.nodes, placement, replica, target_node, rule
                    )
            if action.kind == "start":
                updated = set(state[self._replica_index(action.replica_id)])
                updated.add(action.node_id)
            else:
                updated = set(state[self._replica_index(action.replica_id)])
                updated.discard(action.node_id)
            new_state_list = list(state)
            new_state_list[self._replica_index(action.replica_id)] = frozenset(updated)
            state = tuple(new_state_list)
            steps.append(
                RolloutStep(
                    index=index,
                    action=action,
                    running_before=running_before,
                    running_after=self._running_count(state),
                    domain_stats_before=stats_before,
                    gate_results=gates,
                )
            )

        return RolloutPlan(
            actions=tuple(actions),
            steps=tuple(steps),
            final_placement=tuple(next(iter(running)) if running else None for running in state),
            feasible=True,
        )

    def _replica_index(self, replica_id: str) -> int:
        for i, replica in enumerate(self.input.replicas):
            if replica.replica_id == replica_id:
                return i
        raise KeyError(replica_id)


def find_plan(
    nodes: Sequence[Node],
    replicas: Sequence[Replica],
    rules: Sequence[SpreadRule],
    min_ready: int,
    max_surge: int,
) -> RolloutPlan:
    """Convenience entry point: build a :class:`Planner` and return its plan."""

    planner_input = PlannerInput(
        nodes=tuple(nodes),
        replicas=tuple(replicas),
        rules=tuple(rules),
        min_ready=min_ready,
        max_surge=max_surge,
    )
    return Planner(planner_input).plan()
