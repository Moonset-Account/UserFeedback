"""Aggregating resolved stacks into a call tree and leaf hotspots.

The tree is built from the expanded, root-first frame lists.  Inline
expansion means one raw frame can contribute several tree levels, which is
exactly what makes inlined hot paths visible.  Placeholder frames take part
in aggregation under their placeholder marker so nothing is silently
dropped; use :attr:`CallTreeNode.is_placeholder` to filter them out.

Counts are *sample* counts: each resolved stack contributes one to every
node on its root-to-leaf path (``inclusive``), and one to the innermost
node of that path (``self_samples``).

Children are keyed by ``(name, module, version, placeholder)``, so the same
symbol name from two different builds stays as two distinct nodes and each
remains individually traceable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from .frames import ResolvedFrame, ResolvedStack

__all__ = ["CallTreeNode", "Hotspot", "aggregate_stacks", "build_call_tree"]


@dataclass
class CallTreeNode:
    """One node of the merged call tree."""

    name: str
    module: Optional[str] = None
    version: Optional[str] = None
    placeholder: bool = False
    inclusive: int = 0
    self_samples: int = 0
    children: Dict[Tuple[str, Optional[str], Optional[str], bool], "CallTreeNode"] = (
        field(default_factory=dict)
    )

    @property
    def is_placeholder(self) -> bool:
        return self.placeholder

    def child(self, frame: ResolvedFrame) -> "CallTreeNode":
        key = (frame.name, frame.module, frame.version, not frame.resolved)
        node = self.children.get(key)
        if node is None:
            node = CallTreeNode(
                name=frame.name,
                module=frame.module,
                version=frame.version,
                placeholder=not frame.resolved,
            )
            self.children[key] = node
        return node

    def sorted_children(self) -> List["CallTreeNode"]:
        # Tie-break on the module/version as well, so two same-named symbols
        # from different builds order deterministically.
        return sorted(
            self.children.values(),
            key=lambda node: (
                -node.inclusive,
                node.name,
                node.module or "",
                node.version or "",
            ),
        )

    def walk(self) -> Iterable[Tuple[int, "CallTreeNode"]]:
        """Depth-first traversal yielding ``(depth, node)``."""

        stack: List[Tuple[int, CallTreeNode]] = [(0, self)]
        while stack:
            depth, node = stack.pop()
            yield depth, node
            for child in reversed(node.sorted_children()):
                stack.append((depth + 1, child))


@dataclass(frozen=True)
class Hotspot:
    """A leaf symbol and how many samples ended there."""

    name: str
    module: Optional[str]
    version: Optional[str]
    self_samples: int
    placeholder: bool = False


def build_call_tree(
    stacks: Iterable[ResolvedStack], root_name: str = "<all>"
) -> CallTreeNode:
    """Merge ``stacks`` into a single call tree rooted at ``root_name``."""

    root = CallTreeNode(name=root_name)
    for stack in stacks:
        node = root
        node.inclusive += 1
        for frame in stack.frames:
            node = node.child(frame)
            node.inclusive += 1
        node.self_samples += 1
    return root


def aggregate_stacks(stacks: Iterable[ResolvedStack]) -> List[Hotspot]:
    """Return leaf hotspots, hottest first, stable by name on ties."""

    counts: Dict[Tuple[str, Optional[str], Optional[str], bool], int] = {}
    for stack in stacks:
        leaf = stack.frames[-1]
        key = (leaf.name, leaf.module, leaf.version, not leaf.resolved)
        counts[key] = counts.get(key, 0) + 1

    hotspots = [
        Hotspot(
            name=name,
            module=module,
            version=version,
            self_samples=count,
            placeholder=placeholder,
        )
        for (name, module, version, placeholder), count in counts.items()
    ]
    hotspots.sort(
        key=lambda spot: (
            -spot.self_samples,
            spot.name,
            spot.module or "",
            spot.version or "",
        )
    )
    return hotspots
