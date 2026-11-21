"""Domain models for the group-placement module.

All model objects are immutable (frozen dataclasses / tuples).  The planner
never mutates a :class:`Node`, so when placement fails the node's reported
remaining resources are provably left untouched.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Container:
    """A single container belonging to a Pod.

    Attributes:
        name: Container name (unique within one Pod).
        cpu: Requested CPU (non-negative number).
        memory: Requested memory (non-negative number).
        kind: ``"init"`` for an init container or ``"app"`` for a business
            container.
        resident: Only meaningful for init containers.  A resident init
            container keeps running from the moment it starts; an ordinary
            init container runs to completion and exits before the next one
            starts.
    """

    name: str
    cpu: float
    memory: float
    kind: str = "app"
    resident: bool = False


@dataclass(frozen=True)
class Pod:
    """A Pod described by its ordered init containers and app containers.

    Init containers start strictly in the given order.  Ordinary init
    containers must exit before the next one starts; resident init
    containers keep running.  All app containers start together only after
    every ordinary init container has exited.
    """

    name: str
    inits: tuple[Container, ...] = ()
    apps: tuple[Container, ...] = ()
    cpu_overhead: float = 0.0
    memory_overhead: float = 0.0
    eligible_nodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Node:
    """A candidate node with its currently remaining CPU and memory."""

    name: str
    cpu_available: float
    memory_available: float
