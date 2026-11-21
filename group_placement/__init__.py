"""Group placement module.

Public API:
    Container, Pod, Node, Stage, StageUsage, PodPeak,
    NodeResidual, PlacementResult, PlacementInputError,
    compute_pod_peak, place_group, MAX_PODS, MAX_NODES
"""

from __future__ import annotations

from .models import Container, Node, Pod
from .peaks import PodPeak, Stage, StageUsage, compute_pod_peak
from .placement import (
    EPSILON,
    NodeResidual,
    PlacementResult,
    place_group,
)
from .validation import MAX_NODES, MAX_PODS, PlacementInputError

__all__ = [
    "Container",
    "Pod",
    "Node",
    "Stage",
    "StageUsage",
    "PodPeak",
    "NodeResidual",
    "PlacementResult",
    "PlacementInputError",
    "compute_pod_peak",
    "place_group",
    "MAX_PODS",
    "MAX_NODES",
    "EPSILON",
]
