"""Full-domain k-anonymity / l-diversity anonymization search.

Given a batch of records, one rooted generalization tree per quasi-identifier
(QI) field, a sensitive categorical field, parameters ``k`` and ``l``, a
suppression budget and per-field/per-level loss weights, this module searches
*every* field-level vector (one uniform generalization level per field for the
whole batch) and returns the feasible solution with the lowest total loss.

Model
-----
* Full-domain generalization: every record uses the same level vector.
* Records are grouped by the tuple of generalized QI values.  Groups are
  compared by **node identity**, never by display name (names may repeat
  inside a tree).
* A group is retained iff ``size >= k`` and it contains at least ``l``
  distinct sensitive values; otherwise the *whole* group is suppressed.
* Total loss = ``retained_count * per_record_generalization_loss``
  + ``suppressed_count * suppression_penalty`` where the per-record
  generalization loss is ``sum(loss_weights[field][level[field]])``.
* Ties in total loss are broken deterministically: the lexicographically
  smallest level vector (in the insertion order of ``quasi_identifiers``)
  wins, i.e. less generalization on earlier fields is preferred.
* Missing values (``None``) map to a dedicated, independent missing leaf
  designated on each tree.

Limits: at most 6 quasi-identifier fields, each tree at most 4 layers
(root + 3).  The search space is therefore at most 4**6 = 4096 vectors.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _dc_field
from itertools import product
from typing import Any, Dict, Hashable, List, Mapping, Optional, Sequence, Tuple

MAX_QUASI_IDENTIFIERS = 6
MAX_TREE_LAYERS = 4  # root counts as one layer


class GenNode:
    """A node in a generalization tree.

    Nodes compare by *identity*: two nodes with the same display ``name``
    are still distinct generalization targets and must never be merged
    when grouping records.
    """

    __slots__ = ("name", "children", "parent")

    def __init__(self, name: Any, children: Sequence["GenNode"] = ()):
        self.name = str(name)
        self.children: Tuple[GenNode, ...] = tuple(children)
        self.parent: Optional[GenNode] = None
        for child in self.children:
            if not isinstance(child, GenNode):
                raise TypeError("children must be GenNode instances")
            if child.parent is not None:
                raise ValueError(f"node {child.name!r} is already attached to a parent")
            child.parent = self

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"GenNode({self.name!r})"


class GeneralizationTree:
    """A rooted generalization tree for one quasi-identifier field.

    ``missing_leaf`` (optional) designates the independent leaf used for
    missing (``None``) record values; it must be a leaf of this tree.
    """

    def __init__(self, root: GenNode, missing_leaf: Optional[GenNode] = None):
        if not isinstance(root, GenNode):
            raise TypeError("root must be a GenNode")
        if root.parent is not None:
            raise ValueError("root must be the top of the tree (parent is set)")
        self.root = root

        depth: Dict[GenNode, int] = {root: 0}
        path: Dict[GenNode, Tuple[int, ...]] = {root: ()}
        order: List[GenNode] = []
        stack = [root]
        while stack:
            node = stack.pop()
            order.append(node)
            for index, child in enumerate(node.children):
                depth[child] = depth[node] + 1
                path[child] = path[node] + (index,)
                stack.append(child)

        self._depth = depth
        self._path = path
        self.leaves: Tuple[GenNode, ...] = tuple(n for n in order if n.is_leaf)
        self._leaf_set = frozenset(self.leaves)
        self.layers = max(depth.values()) + 1
        if self.layers > MAX_TREE_LAYERS:
            raise ValueError(
                f"tree has {self.layers} layers; at most {MAX_TREE_LAYERS} allowed"
            )
        if missing_leaf is not None and missing_leaf not in self._leaf_set:
            raise ValueError("missing_leaf must be a leaf of this tree")
        self.missing_leaf = missing_leaf
        # Deepest leaf determines the number of usable generalization levels.
        self.max_level = max(depth[n] for n in self.leaves)

        self._by_name: Dict[str, List[GenNode]] = {}
        for leaf in self.leaves:
            self._by_name.setdefault(leaf.name, []).append(leaf)

    # -- node helpers -----------------------------------------------------
    def depth_of(self, node: GenNode) -> int:
        if node not in self._depth:
            raise ValueError(f"node {node!r} does not belong to this tree")
        return self._depth[node]

    def path_of(self, node: GenNode) -> Tuple[int, ...]:
        """Stable identity token: child-index path from the root."""
        if node not in self._path:
            raise ValueError(f"node {node!r} does not belong to this tree")
        return self._path[node]

    def generalize(self, node: GenNode, level: int) -> GenNode:
        """Walk ``level`` steps towards the root (stopping at the root)."""
        if node not in self._depth:
            raise ValueError(f"node {node!r} does not belong to this tree")
        if level < 0:
            raise ValueError("level must be >= 0")
        steps = 0
        while node.parent is not None and steps < level:
            node = node.parent
            steps += 1
        return node

    def resolve(self, value: Any) -> GenNode:
        """Map a raw record value to a leaf of this tree.

        * ``None`` resolves to the designated missing leaf.
        * A ``GenNode`` is accepted only if it is a leaf of *this* tree
          (identity check).
        * Any other value is matched against leaf display names; the match
          must be unique.  Because display names may repeat, an ambiguous
          name raises ``ValueError`` and the caller must pass the leaf
          ``GenNode`` directly.
        """
        if value is None:
            if self.missing_leaf is None:
                raise ValueError(
                    "record has a missing value but the tree defines no missing leaf"
                )
            return self.missing_leaf
        if isinstance(value, GenNode):
            if value not in self._leaf_set:
                raise ValueError(f"node {value!r} is not a leaf of this tree")
            return value
        matches = self._by_name.get(value if isinstance(value, str) else str(value))
        if not matches:
            raise ValueError(f"value {value!r} matches no leaf of this tree")
        if len(matches) > 1:
            raise ValueError(
                f"value {value!r} matches {len(matches)} leaves (duplicate display "
                "names); pass the intended GenNode object directly"
            )
        return matches[0]


@dataclass(frozen=True)
class GroupReport:
    """Per-group verification basis for one generalized QI combination."""

    key_names: Tuple[str, ...]        # display names per field (may repeat!)
    key_paths: Tuple[Tuple[int, ...], ...]  # identity paths per field
    members: Tuple[int, ...]          # indices of records in the group
    size: int
    distinct_sensitive: int
    meets_k: bool
    meets_l: bool
    decision: str                     # "retained" | "suppressed"
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key_names": list(self.key_names),
            "key_paths": [list(p) for p in self.key_paths],
            "members": list(self.members),
            "size": self.size,
            "distinct_sensitive": self.distinct_sensitive,
            "meets_k": self.meets_k,
            "meets_l": self.meets_l,
            "decision": self.decision,
            "reason": self.reason,
        }


@dataclass
class AnonymizationResult:
    feasible: bool
    levels: Optional[Dict[str, int]]          # chosen level per field
    retained: List[Dict[str, Any]]            # generalized retained records
    suppressed_indices: List[int]
    suppressed_count: int
    groups: List[GroupReport]
    total_loss: Optional[float]
    generalization_loss: Optional[float]
    suppression_loss: Optional[float]
    reason: str
    candidates_evaluated: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feasible": self.feasible,
            "levels": dict(self.levels) if self.levels is not None else None,
            "retained": self.retained,
            "suppressed_indices": list(self.suppressed_indices),
            "suppressed_count": self.suppressed_count,
            "groups": [g.to_dict() for g in self.groups],
            "total_loss": self.total_loss,
            "generalization_loss": self.generalization_loss,
            "suppression_loss": self.suppression_loss,
            "reason": self.reason,
            "candidates_evaluated": self.candidates_evaluated,
        }


def _validate_weights(
    fields: Sequence[str],
    trees: Mapping[str, GeneralizationTree],
    loss_weights: Mapping[str, Sequence[float]],
) -> Dict[str, Tuple[float, ...]]:
    weights: Dict[str, Tuple[float, ...]] = {}
    for f in fields:
        if f not in loss_weights:
            raise ValueError(f"missing loss weights for field {f!r}")
        w = tuple(loss_weights[f])
        need = trees[f].max_level + 1
        if len(w) != need:
            raise ValueError(
                f"field {f!r}: expected {need} loss weights (levels 0..{need - 1}), "
                f"got {len(w)}"
            )
        if any(not isinstance(x, (int, float)) or isinstance(x, bool) for x in w):
            raise ValueError(f"field {f!r}: loss weights must be numbers")
        if any(x < 0 for x in w):
            raise ValueError(f"field {f!r}: loss weights must be >= 0")
        weights[f] = w
    return weights


def anonymize(
    records: Sequence[Mapping[str, Any]],
    *,
    quasi_identifiers: Mapping[str, GeneralizationTree],
    sensitive_field: str,
    k: int,
    l: int,
    max_suppressed: int,
    loss_weights: Mapping[str, Sequence[float]],
    suppression_penalty: float,
) -> AnonymizationResult:
    """Search all field-level vectors and return the minimum-loss solution.

    See the module docstring for the exact loss model and tie-break rule.
    When no level vector keeps the suppressed-record count within
    ``max_suppressed`` the result has ``feasible=False`` and ``reason``
    states the minimum achievable suppression count.
    """
    fields = list(quasi_identifiers.keys())
    if not 1 <= len(fields) <= MAX_QUASI_IDENTIFIERS:
        raise ValueError(
            f"need 1..{MAX_QUASI_IDENTIFIERS} quasi-identifier fields, got {len(fields)}"
        )
    trees: Dict[str, GeneralizationTree] = {}
    for f in fields:
        t = quasi_identifiers[f]
        if not isinstance(t, GeneralizationTree):
            raise TypeError(f"quasi_identifiers[{f!r}] must be a GeneralizationTree")
        trees[f] = t
    if not records:
        raise ValueError("records must be a non-empty sequence")
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError("k must be an integer >= 1")
    if not isinstance(l, int) or isinstance(l, bool) or l < 1:
        raise ValueError("l must be an integer >= 1")
    if not isinstance(max_suppressed, int) or isinstance(max_suppressed, bool) or max_suppressed < 0:
        raise ValueError("max_suppressed must be an integer >= 0")
    if not isinstance(suppression_penalty, (int, float)) or isinstance(suppression_penalty, bool):
        raise ValueError("suppression_penalty must be a number")
    if suppression_penalty < 0:
        raise ValueError("suppression_penalty must be >= 0")
    weights = _validate_weights(fields, trees, loss_weights)

    # Resolve every record's QI values to tree leaves up front.
    resolved: List[Tuple[GenNode, ...]] = []
    sensitive: List[Hashable] = []
    for i, rec in enumerate(records):
        for f in fields:
            if f not in rec:
                raise ValueError(f"record {i} is missing quasi-identifier field {f!r}")
        if sensitive_field not in rec:
            raise ValueError(f"record {i} is missing sensitive field {sensitive_field!r}")
        resolved.append(tuple(trees[f].resolve(rec[f]) for f in fields))
        sval = rec[sensitive_field]
        try:
            hash(sval)
        except TypeError as exc:
            raise ValueError(
                f"record {i}: sensitive value must be hashable, got {type(sval).__name__}"
            ) from exc
        sensitive.append(sval)

    n = len(records)
    level_ranges = [range(trees[f].max_level + 1) for f in fields]

    best = None  # (total_loss, vector, reports, retained_idx, suppressed_idx)
    candidates = 0
    min_suppressed_seen: Optional[int] = None
    min_suppressed_vector: Optional[Tuple[int, ...]] = None

    # product() iterates lexicographically (last field fastest); combined
    # with a strict "<" update this yields the deterministic tie-break:
    # the lexicographically smallest level vector among min-loss candidates.
    for vector in product(*level_ranges):
        candidates += 1
        per_record_loss = 0
        for f, lvl in zip(fields, vector):
            per_record_loss += weights[f][lvl]

        groups: Dict[Tuple[GenNode, ...], List[int]] = {}
        for idx in range(n):
            key = tuple(
                trees[f].generalize(resolved[idx][j], lvl)
                for j, (f, lvl) in enumerate(zip(fields, vector))
            )
            groups.setdefault(key, []).append(idx)

        reports: List[GroupReport] = []
        retained_idx: List[int] = []
        suppressed_idx: List[int] = []
        for key, members in groups.items():
            size = len(members)
            distinct = len({sensitive[i] for i in members})
            meets_k = size >= k
            meets_l = distinct >= l
            if meets_k and meets_l:
                decision = "retained"
                reason = (
                    f"size {size} >= k={k} and {distinct} distinct sensitive "
                    f"values >= l={l}"
                )
                retained_idx.extend(members)
            else:
                decision = "suppressed"
                causes = []
                if not meets_k:
                    causes.append(f"size {size} < k={k}")
                if not meets_l:
                    causes.append(f"{distinct} distinct sensitive values < l={l}")
                reason = "whole group suppressed: " + "; ".join(causes)
                suppressed_idx.extend(members)
            reports.append(
                GroupReport(
                    key_names=tuple(node.name for node in key),
                    key_paths=tuple(
                        trees[f].path_of(node) for f, node in zip(fields, key)
                    ),
                    members=tuple(sorted(members)),
                    size=size,
                    distinct_sensitive=distinct,
                    meets_k=meets_k,
                    meets_l=meets_l,
                    decision=decision,
                    reason=reason,
                )
            )

        if min_suppressed_seen is None or len(suppressed_idx) < min_suppressed_seen:
            min_suppressed_seen = len(suppressed_idx)
            min_suppressed_vector = vector

        if len(suppressed_idx) > max_suppressed:
            continue  # exceeds the suppression budget: infeasible vector

        total = len(retained_idx) * per_record_loss + len(suppressed_idx) * suppression_penalty
        if best is None or total < best[0]:
            best = (total, vector, reports, retained_idx, suppressed_idx)

    if best is None:
        reason = (
            f"no feasible solution: every level vector suppresses more than the "
            f"budget of {max_suppressed} records; minimum achievable suppressed "
            f"count is {min_suppressed_seen} at levels "
            f"{dict(zip(fields, min_suppressed_vector))}"
        )
        return AnonymizationResult(
            feasible=False,
            levels=None,
            retained=[],
            suppressed_indices=[],
            suppressed_count=0,
            groups=[],
            total_loss=None,
            generalization_loss=None,
            suppression_loss=None,
            reason=reason,
            candidates_evaluated=candidates,
        )

    total, vector, reports, retained_idx, suppressed_idx = best
    levels = dict(zip(fields, vector))
    retained = [
        {
            "index": i,
            "quasi": {
                f: trees[f].generalize(resolved[i][j], vector[j]).name
                for j, f in enumerate(fields)
            },
            "quasi_paths": {
                f: list(trees[f].path_of(trees[f].generalize(resolved[i][j], vector[j])))
                for j, f in enumerate(fields)
            },
            "sensitive": sensitive[i],
        }
        for i in sorted(retained_idx)
    ]
    reports.sort(key=lambda g: g.key_paths)
    per_record_loss = sum(weights[f][lvl] for f, lvl in zip(fields, vector))
    gen_loss = len(retained_idx) * per_record_loss
    sup_loss = len(suppressed_idx) * suppression_penalty
    return AnonymizationResult(
        feasible=True,
        levels=levels,
        retained=retained,
        suppressed_indices=sorted(suppressed_idx),
        suppressed_count=len(suppressed_idx),
        groups=reports,
        total_loss=total,
        generalization_loss=gen_loss,
        suppression_loss=sup_loss,
        reason=(
            f"minimum total loss {total} over {candidates} candidate level "
            f"vectors (ties broken by lexicographically smallest level vector)"
        ),
        candidates_evaluated=candidates,
    )
