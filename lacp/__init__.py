"""Simplified LACP aggregation controller package."""

from .clock import ManualClock
from .controller import (
    DETACHED_GROUP,
    GroupId,
    LacpController,
    MarkerRequest,
    NeighborInfo,
    PortState,
)

__all__ = [
    "ManualClock",
    "LacpController",
    "NeighborInfo",
    "MarkerRequest",
    "PortState",
    "GroupId",
    "DETACHED_GROUP",
]
