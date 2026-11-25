"""Immutable input/output data models for the rolling re-layout planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class NodeSelector:
    """Node selection condition attached to a replica.

    - ``required`` labels must all be present on the node with equal values
      (Kubernetes ``In`` / equal semantics).
    - ``excluded`` labels reject a node only when the label is present *and*
      its value equals the excluded value (Kubernetes ``NotIn`` semantics: a
      missing label does not match the exclusion).
    """

    required: Mapping[str, str] = field(default_factory=dict)
    excluded: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Freeze into plain dict copies so the selector is hashable/stable.
        object.__setattr__(self, "required", dict(self.required))
        object.__setattr__(self, "excluded", dict(self.excluded))
        overlap = set(self.required) & set(self.excluded)
        if overlap:
            raise ValueError(f"label key appears in both required and excluded: {sorted(overlap)}")

    def matches(self, node: "Node") -> bool:
        for key, value in self.required.items():
            if node.labels.get(key) != value:
                return False
        for key, value in self.excluded.items():
            if node.labels.get(key) == value:
                return False
        return True


@dataclass(frozen=True)
class Node:
    """A scheduler node.

    ``free_slots`` is the number of empty slots at the *initial* state. Slots
    released by stopping replicas are added back during planning.
    """

    node_id: str
    labels: Mapping[str, str]
    free_slots: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "labels", dict(self.labels))
        if self.free_slots < 0:
            raise ValueError(f"node {self.node_id!r} has negative free_slots")


@dataclass(frozen=True)
class SpreadRule:
    """One spreading rule (rack-level or region-level, etc.).

    ``min_domains`` is the eligible-domain threshold: when fewer than
    ``min_domains`` domains are eligible for a replica, the global minimum
    replica count is treated as zero.
    """

    name: str
    topology_key: str
    max_skew: int
    min_domains: int = 1

    def __post_init__(self) -> None:
        if self.max_skew < 1:
            raise ValueError(f"rule {self.name!r}: max_skew must be >= 1")
        if self.min_domains < 1:
            raise ValueError(f"rule {self.name!r}: min_domains must be >= 1")


@dataclass(frozen=True)
class Replica:
    """One replica of the service with its own node selection condition."""

    replica_id: str
    selector: NodeSelector
    current_node: str | None = None


@dataclass(frozen=True)
class DomainStat:
    """Per-rule domain statistics for one replica's condition."""

    rule_name: str
    topology_key: str
    eligible_domains: tuple[str, ...]
    replica_counts: Mapping[str, int]
    eligible_domain_count: int
    min_domains: int
    effective_min: int
    max_count: int
    max_skew_observed: int


@dataclass(frozen=True)
class GateEvaluation:
    """Result of applying one spreading rule to a candidate start action."""

    rule_name: str
    topology_key: str
    target_domain: str | None
    allowed: bool
    max_skew: int
    observed_skew: int
    effective_min: int
    eligible_domain_count: int
    replica_counts_after: Mapping[str, int]
    reason: str


@dataclass(frozen=True)
class Action:
    """A single planning step: start or stop one replica on one node."""

    kind: str  # "start" | "stop"
    replica_id: str
    node_id: str

    def __post_init__(self) -> None:
        if self.kind not in ("start", "stop"):
            raise ValueError(f"unknown action kind {self.kind!r}")


@dataclass(frozen=True)
class RolloutStep:
    """One executed action with the domain statistics it was based on."""

    index: int
    action: Action
    running_before: int
    running_after: int
    domain_stats_before: Mapping[str, DomainStat]
    gate_results: Mapping[str, GateEvaluation]


@dataclass(frozen=True)
class RolloutPlan:
    """The full minimum-action rollout and its per-step evidence."""

    actions: tuple[Action, ...]
    steps: tuple[RolloutStep, ...]
    final_placement: tuple[str | None, ...]
    feasible: bool

    @property
    def length(self) -> int:
        return len(self.actions)


@dataclass(frozen=True)
class PlannerInput:
    """Complete planner input."""

    nodes: tuple[Node, ...]
    replicas: tuple[Replica, ...]
    rules: tuple[SpreadRule, ...]
    min_ready: int
    max_surge: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "replicas", tuple(self.replicas))
        object.__setattr__(self, "rules", tuple(self.rules))
        if not self.nodes:
            raise ValueError("at least one node is required")
        node_ids = [n.node_id for n in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("duplicate node ids")
        replica_ids = [r.replica_id for r in self.replicas]
        if len(set(replica_ids)) != len(replica_ids):
            raise ValueError("duplicate replica ids")
        rule_names = [r.name for r in self.rules]
        if len(set(rule_names)) != len(rule_names):
            raise ValueError("duplicate rule names")
        node_by_id = {n.node_id: n for n in self.nodes}
        for replica in self.replicas:
            if replica.current_node is not None and replica.current_node not in node_by_id:
                raise ValueError(
                    f"replica {replica.replica_id!r} runs on unknown node {replica.current_node!r}"
                )
        if not self.replicas:
            raise ValueError("at least one replica is required")
        if self.min_ready < 0:
            raise ValueError("min_ready must be >= 0")
        if self.min_ready > len(self.replicas):
            raise ValueError("min_ready cannot exceed the number of replicas")
        if self.max_surge < 0:
            raise ValueError("max_surge must be >= 0")
