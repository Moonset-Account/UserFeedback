"""Core data model: graph nodes/edges, path states and rule tables.

Labels (input sources, output categories, field tags) are all *abstract*
strings -- the checker never interprets their concrete runtime meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional

# ---------------------------------------------------------------------------
# Graph model
# ---------------------------------------------------------------------------

VALID_KINDS = frozenset(
    {"entry", "source", "stmt", "sink", "merge", "branch", "call",
     "return", "unmodeled"}
)


@dataclass(frozen=True)
class Node:
    """One statement in the data-flow graph.

    Kind-specific attributes
    ------------------------
    source   -> field, source_label
    stmt     -> field, sanitizers (ordered list applied at this statement)
    sink     -> field, category, sink_label
    call     -> callee, bindings ((formal, actual), ...), ret_field
    unmodeled-> reason
    entry/return/merge/branch -> no extra attributes
    """

    id: str
    kind: str
    function: str
    field: Optional[str] = None
    source_label: Optional[str] = None
    sink_label: Optional[str] = None
    category: Optional[str] = None
    sanitizers: tuple = ()
    callee: Optional[str] = None
    bindings: tuple = ()          # tuple of (formal_field, actual_field)
    ret_field: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class Function:
    id: str
    entry: str
    returns: tuple = ()           # node ids of return statements


@dataclass
class Graph:
    graph_id: str
    nodes: dict = field(default_factory=dict)                 # id -> Node
    adj: dict = field(default_factory=dict)                   # id -> [id]
    functions: dict = field(default_factory=dict)             # id -> Function
    watch: tuple = ()             # explicit watch declarations
    assumptions: tuple = ()       # human-readable incompleteness notes
    # node ids that introduce an unknown (unmodelled) data origin
    incompleteness: FrozenSet[str] = frozenset()

    def successors(self, node_id: str):
        return self.adj.get(node_id, ())


# ---------------------------------------------------------------------------
# Taint / sanitization state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PathState:
    """State of one field along *one* path reaching a program point.

    tainted     : value derives from an abstract input source
    unknown     : value may derive from an origin the graph does not model
    sanitizers  : sanitizers that were applied on this path (ordered -> tuple)
    origins     : source labels that contributed the taint
    """

    tainted: bool = False
    unknown: bool = False
    sanitizers: tuple = ()
    origins: tuple = ()

    def with_sanitizer(self, name: str) -> "PathState":
        if name in self.sanitizers:
            return self
        return PathState(
            tainted=self.tainted,
            unknown=self.unknown,
            sanitizers=self.sanitizers + (name,),
            origins=self.origins,
        )

    def as_unknown_return(self) -> "PathState":
        """Return value of a call whose body/edge is absent from the graph."""
        return PathState(
            tainted=True,
            unknown=True,
            sanitizers=(),
            origins=self.origins,
        )


# An environment maps field name -> set of PathState (one set element per
# distinct reaching path; joins are set unions so branch alternatives survive).
Env = dict


def empty_env() -> Env:
    return {}


def field_states(env: Env, field_name: str) -> FrozenSet[PathState]:
    return frozenset(env.get(field_name, ()))


def join_field(states_a, states_b) -> FrozenSet[PathState]:
    """Merge at a confluence point: keep *both* branch alternatives."""
    return frozenset(states_a) | frozenset(states_b)


def join_envs(env_a: Env, env_b: Env) -> Env:
    out: Env = {}
    for f in set(env_a) | set(env_b):
        out[f] = join_field(env_a.get(f, frozenset()),
                            env_b.get(f, frozenset()))
    return out


# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleTable:
    """One version of the static-analysis rule table.

    applicability maps sanitizer name -> output categories it is allowed to
    sanitize for.  A (sanitizer, category) pair is the atomic rule tuple.
    """

    version: str
    applicability: dict = field(default_factory=dict)

    def covers(self, sanitizer: str, category: str) -> bool:
        return category in self.applicability.get(sanitizer, frozenset())

    def pairs(self) -> FrozenSet[tuple]:
        out = set()
        for san, cats in self.applicability.items():
            for cat in cats:
                out.add((san, cat))
        return frozenset(out)


def diff_rule_tables(old: RuleTable, new: RuleTable):
    """Return (added_pairs, removed_pairs) between two rule versions."""
    old_pairs, new_pairs = old.pairs(), new.pairs()
    return sorted(new_pairs - old_pairs), sorted(old_pairs - new_pairs)
