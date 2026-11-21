"""Backtracking group placement engine.

Placement rule (request-based reservation): each Pod reserves, on its node,
the per-resource peak computed from its lifecycle stages plus fixed
overhead (see :mod:`group_placement.peaks`).  A node can host a set of Pods
only when the sum of their CPU peaks and the sum of their memory peaks both
fit within the node's remaining capacity.

The search is a depth-first backtracking assignment.  At every step the Pod
with the fewest currently feasible eligible nodes (minimum-remaining-values
heuristic) is placed next.  This is what makes the interleaved case work:
when Pod A has two candidate nodes and Pod B can only use one of them, B is
placed first, so A never greedily steals B's only node.

Node objects are never mutated: consumed capacity is tracked in local
dictionaries that are rolled back on backtracking, and on total failure the
reported residuals equal the nodes' original remaining resources.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Node, Pod
from .peaks import PodPeak, compute_pod_peak
from .validation import validate

# Tolerance for floating-point capacity comparisons.
EPSILON = 1e-9


@dataclass(frozen=True)
class NodeResidual:
    """Remaining node capacity after a placement."""

    node_name: str
    cpu_available: float
    memory_available: float


@dataclass(frozen=True)
class PlacementResult:
    """Outcome of a group placement request.

    Attributes:
        success: Whether every Pod was placed.
        peaks: Mapping of Pod name to its :class:`PodPeak` (always populated,
            even on failure, so callers can inspect the two peaks and stages).
        placement: Mapping of Pod name to node name; empty on failure.
        residuals: Mapping of node name to :class:`NodeResidual`.  On failure
            this is exactly the node's original remaining capacity.
        reason: Human-readable failure reason; ``None`` on success.
    """

    success: bool
    peaks: dict[str, PodPeak]
    placement: dict[str, str] = field(default_factory=dict)
    residuals: dict[str, NodeResidual] = field(default_factory=dict)
    reason: str | None = None


def _fits(
    node: Node,
    used_cpu: float,
    used_memory: float,
    peak: PodPeak,
) -> bool:
    return (
        used_cpu + peak.cpu_peak <= node.cpu_available + EPSILON
        and used_memory + peak.memory_peak <= node.memory_available + EPSILON
    )


def place_group(pods: list[Pod], nodes: list[Node]) -> PlacementResult:
    """Try to place the whole Pod group on the given nodes.

    Returns a :class:`PlacementResult`.  When the full group fits, one
    complete assignment is returned; otherwise ``success`` is ``False`` and
    every node residual equals its input remaining capacity.
    """

    validate(pods, nodes)

    peaks = {pod.name: compute_pod_peak(pod) for pod in pods}
    node_by_name = {node.name: node for node in nodes}
    eligible = {
        pod.name: list(dict.fromkeys(pod.eligible_nodes)) for pod in pods
    }

    used_cpu = {node.name: 0.0 for node in nodes}
    used_memory = {node.name: 0.0 for node in nodes}
    assignment: dict[str, str] = {}

    def original_residuals() -> dict[str, NodeResidual]:
        return {
            node.name: NodeResidual(
                node_name=node.name,
                cpu_available=node.cpu_available,
                memory_available=node.memory_available,
            )
            for node in nodes
        }

    def feasible_nodes(pod: Pod) -> list[str]:
        peak = peaks[pod.name]
        return [
            node_name
            for node_name in eligible[pod.name]
            if _fits(
                node_by_name[node_name],
                used_cpu[node_name],
                used_memory[node_name],
                peak,
            )
        ]

    def search(remaining: list[Pod]) -> bool:
        if not remaining:
            return True

        # Dynamic MRV: place the most constrained Pod first.  Ties are broken
        # by larger CPU peak, so heavy Pods are also handled early.
        def mrv_key(pod: Pod) -> tuple[int, float]:
            return (len(feasible_nodes(pod)), -peaks[pod.name].cpu_peak)

        ordered = sorted(remaining, key=mrv_key)
        chosen = ordered[0]
        candidates = feasible_nodes(chosen)

        if not candidates:
            return False

        peak = peaks[chosen.name]
        rest = ordered[1:]
        for node_name in candidates:
            assignment[chosen.name] = node_name
            used_cpu[node_name] += peak.cpu_peak
            used_memory[node_name] += peak.memory_peak

            if search(rest):
                return True

            used_cpu[node_name] -= peak.cpu_peak
            used_memory[node_name] -= peak.memory_peak
            del assignment[chosen.name]

        return False

    if not pods:
        return PlacementResult(
            success=True,
            peaks=peaks,
            placement={},
            residuals=original_residuals(),
        )

    if search(list(pods)):
        residuals: dict[str, NodeResidual] = {}
        for node in nodes:
            cpu_left = node.cpu_available - used_cpu[node.name]
            memory_left = node.memory_available - used_memory[node.name]
            # Clamp away negative zero / float noise at the boundary.
            if abs(cpu_left) <= EPSILON:
                cpu_left = 0.0
            if abs(memory_left) <= EPSILON:
                memory_left = 0.0
            residuals[node.name] = NodeResidual(
                node_name=node.name,
                cpu_available=cpu_left,
                memory_available=memory_left,
            )
        return PlacementResult(
            success=True,
            peaks=peaks,
            placement=dict(assignment),
            residuals=residuals,
        )

    return PlacementResult(
        success=False,
        peaks=peaks,
        placement={},
        residuals=original_residuals(),
        reason="no node assignment satisfies every pod's eligibility and "
        "peak CPU/memory reservation",
    )
