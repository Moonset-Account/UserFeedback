"""Tree model for hierarchical integer post-processing.

Only three kinds of data enter this module:

* noisy measurements (``noisy``), which may be absent (missing / "缺测"),
* a positive integer weight per node (``weight``),
* the root total that is allowed to be published (``root_total``).

No individual-level records are consulted anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional


def _is_int(value: object) -> bool:
    """``bool`` is a subclass of ``int``; we do not want it here."""
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Node:
    """A single hierarchy node.

    Parameters
    ----------
    node_id:
        Stable identifier, unique within the hierarchy.
    parent:
        Identifier of the parent, or ``None`` for the single root.
    weight:
        Positive integer weight used in the squared-error objective.  A node
        without a measurement does not contribute a term, but a weight may
        still be supplied and is validated.
    noisy:
        Noisy integer measurement, or ``None`` when the node is missing.
        It may be negative.
    leaf_index:
        Position used for the lexicographic tie-break.  Must be supplied for
        either every leaf or for no leaf at all (see :class:`Hierarchy`).
    """

    node_id: str
    parent: Optional[str]
    weight: Optional[int] = None
    noisy: Optional[int] = None
    leaf_index: Optional[int] = None


class HierarchyError(ValueError):
    """Raised when the hierarchy description is inconsistent."""


class Hierarchy:
    """Validated tree plus derived lookups.

    Validates the tree shape, weights, measurements and the public root
    total, then exposes ``children``, ``depth``, traversal orders and the
    ordered leaf list used for the lexicographic tie-break.
    """

    def __init__(self, nodes: Iterable[Node], root_total: int) -> None:
        node_list = list(nodes)
        if not node_list:
            raise HierarchyError("hierarchy must contain at least one node")
        if not _is_int(root_total) or root_total < 0:
            raise HierarchyError("root_total must be a non-negative integer")

        by_id: dict[str, Node] = {}
        for node in node_list:
            if not isinstance(node.node_id, str) or not node.node_id:
                raise HierarchyError("every node needs a non-empty string id")
            if node.node_id in by_id:
                raise HierarchyError(f"duplicate node id: {node.node_id!r}")
            by_id[node.node_id] = node

        roots = [n for n in node_list if n.parent is None]
        if len(roots) != 1:
            raise HierarchyError(
                f"hierarchy must have exactly one root, found {len(roots)}"
            )
        root = roots[0]

        children: dict[str, list[str]] = {n.node_id: [] for n in node_list}
        for node in node_list:
            if node.parent is None:
                continue
            if node.parent not in by_id:
                raise HierarchyError(
                    f"node {node.node_id!r} references unknown parent "
                    f"{node.parent!r}"
                )
            children[node.parent].append(node.node_id)

        # Reachability from the root both validates connectivity and rules
        # out disconnected cycles.
        depth: dict[str, int] = {root.node_id: 0}
        preorder: list[str] = []
        stack = [root.node_id]
        while stack:
            current = stack.pop()
            preorder.append(current)
            for child in reversed(children[current]):
                if child in depth:
                    raise HierarchyError(
                        f"node {child!r} is reachable twice; not a tree"
                    )
                depth[child] = depth[current] + 1
                stack.append(child)
        if len(depth) != len(node_list):
            missing = sorted(set(by_id) - set(depth))
            raise HierarchyError(
                "hierarchy is not connected to the root: "
                f"{missing}"
            )

        for node in node_list:
            if node.weight is not None:
                if not _is_int(node.weight) or node.weight <= 0:
                    raise HierarchyError(
                        f"node {node.node_id!r}: weight must be a positive "
                        "integer"
                    )
            if node.noisy is not None:
                if not _is_int(node.noisy):
                    raise HierarchyError(
                        f"node {node.node_id!r}: noisy measurement must be an "
                        "integer"
                    )
                if node.weight is None:
                    raise HierarchyError(
                        f"node {node.node_id!r}: a measured node needs a weight"
                    )

        leaves = [n.node_id for n in node_list if not children[n.node_id]]
        if not leaves:
            raise HierarchyError("hierarchy must contain at least one leaf")

        supplied = [n for n in node_list if n.leaf_index is not None]
        if supplied and len(supplied) != len(leaves):
            raise HierarchyError(
                "leaf_index must be supplied for all leaves or for none"
            )
        if supplied:
            indices = {}
            for n in node_list:
                if n.leaf_index is None:
                    continue
                if not _is_int(n.leaf_index) or n.leaf_index < 0:
                    raise HierarchyError(
                        f"node {n.node_id!r}: leaf_index must be a "
                        "non-negative integer"
                    )
                if n.leaf_index in indices.values():
                    raise HierarchyError(
                        f"duplicate leaf_index {n.leaf_index} at "
                        f"{n.node_id!r}"
                    )
                if n.node_id not in leaves:
                    raise HierarchyError(
                        f"node {n.node_id!r} has children but was given a "
                        "leaf_index"
                    )
                indices[n.node_id] = n.leaf_index
            leaf_index = indices
        else:
            leaf_index = {
                node_id: i for i, node_id in enumerate(sorted(leaves))
            }

        self.nodes = by_id
        self.root_total = root_total
        self.root = root.node_id
        self.children = children
        self.depth = depth
        self.preorder = preorder
        self.postorder = list(reversed(preorder))
        self.leaf_index = leaf_index
        self.leaves = sorted(leaves, key=lambda nid: leaf_index[nid])

    # -- convenience -----------------------------------------------------
    def is_leaf(self, node_id: str) -> bool:
        return not self.children[node_id]

    def node(self, node_id: str) -> Node:
        return self.nodes[node_id]

    @classmethod
    def from_dict(cls, spec: Mapping[str, object]) -> "Hierarchy":
        """Build a hierarchy from the plain-dict form used by examples.

        Expected shape::

            {
              "root_total": 200,
              "nodes": {
                "root": {"parent": None, "weight": 1, "noisy": 205},
                "A": {"parent": "root", "weight": 2, "noisy": -3,
                      "leaf_index": 0},
                ...
              }
            }
        """
        try:
            root_total = spec["root_total"]  # type: ignore[index]
            raw_nodes = spec["nodes"]  # type: ignore[index]
        except (KeyError, TypeError) as exc:  # pragma: no cover - guard
            raise HierarchyError(
                "spec must be a mapping with 'root_total' and 'nodes'"
            ) from exc
        if not isinstance(raw_nodes, Mapping):
            raise HierarchyError("'nodes' must be a mapping")
        nodes = []
        for node_id, payload in raw_nodes.items():
            if not isinstance(payload, Mapping):
                raise HierarchyError(f"node {node_id!r} payload must be a map")
            unknown = set(payload) - {
                "parent",
                "weight",
                "noisy",
                "leaf_index",
            }
            if unknown:
                raise HierarchyError(
                    f"node {node_id!r} has unknown keys: {sorted(unknown)}"
                )
            nodes.append(
                Node(
                    node_id=node_id,
                    parent=payload.get("parent"),
                    weight=payload.get("weight"),
                    noisy=payload.get("noisy"),
                    leaf_index=payload.get("leaf_index"),
                )
            )
        return cls(nodes, root_total)  # type: ignore[arg-type]
