"""Core execution semantics and interleaving exploration engine.

State representation
--------------------

A state records, for every DAG step, one of three statuses (pending /
running / ended), the frozen input tuple of every running step, and the
published workspace as an immutable name -> value mapping.  The holder of an
executor lock is always derivable: a running step that declares the lock
owns it.  States are immutable and hashable, so the reachable state space can
be explored with memoization.

Why an independent long chain stays cheap
-----------------------------------------

Two legal interleavings that merely reorder *commuting* events (events that
neither read/write the same workspace name nor contend on the same lock)
land in the same state and are therefore explored once.  Concretely the
number of memoized states is bounded by O(3^n * W) rather than (2n)!, where
W is the number of distinct workspace contents reachable.  Traces are never
enumerated: for every state and every possible output tuple we only retain
the lexicographically smallest completion suffix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .errors import LookupTableError, TraceError, ValidationError

# ---------------------------------------------------------------------------
# Sentinel for "name has never been published"
# ---------------------------------------------------------------------------


class _Unset:
    """Singleton marking a workspace read whose name was never published."""

    _instance: "Optional[_Unset]" = None

    def __new__(cls) -> "_Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __str__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return False

    def __hash__(self) -> int:
        return hash(("__unset__",))

    def __eq__(self, other: object) -> bool:
        return other is self

    def __lt__(self, other: Any) -> bool:
        # UNSET sorts before every ordinary value in safe ordering.
        return other is not self


UNSET = _Unset()

PENDING = "pending"
RUNNING = "running"
ENDED = "ended"

_EVENT_RE = re.compile(r"^(start|end)\(([^()]*)\)$")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


# ---------------------------------------------------------------------------
# Value freezing / safe ordering
# ---------------------------------------------------------------------------


def freeze(value: Any) -> Any:
    """Turn JSON-ish nested data into a hashable, immutable value."""
    if value is UNSET or value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return frozenset((str(k), freeze(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    if isinstance(value, frozenset) and all(_is_hashable(value)):
        return value
    if isinstance(value, set):
        return frozenset(freeze(v) for v in value)
    raise ValidationError(f"value of type {type(value).__name__} is not supported")


def _is_hashable(values: Iterable[Any]) -> bool:
    try:
        for v in values:
            hash(v)
    except TypeError:
        return False
    return True


_TYPE_RANK = {
    _Unset: 0,
    type(None): 1,
    bool: 2,
    int: 3,
    float: 4,
    str: 5,
    tuple: 6,
    frozenset: 7,
}


def order_key(value: Any) -> Tuple[int, Any]:
    """Total ordering key that works for mixed-type frozen values."""
    if isinstance(value, tuple):
        return (6, tuple(order_key(v) for v in value))
    if isinstance(value, frozenset):
        return (7, tuple(sorted((order_key(v) for v in value), key=lambda k: (k[0], k[1]))))
    rank = _TYPE_RANK.get(type(value), 9)
    return (rank, value)


# ---------------------------------------------------------------------------
# Declarative model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One build step.

    Attributes:
        name: unique step name.
        reads: workspace names read when the step starts; their current values
            are frozen into an input tuple in this exact order.
        table: finite total lookup table mapping a frozen input tuple to the
            dict of named values published atomically at end time.
        lock: optional mutually-exclusive executor held from start to end.
        deps: names of steps that must have ended before this one can start.
    """

    name: str
    reads: Tuple[str, ...]
    table: Dict[Tuple[Any, ...], Mapping[str, Any]]
    lock: Optional[str] = None
    deps: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
            raise ValidationError(f"invalid step name: {self.name!r}")
        reads = tuple(self.reads)
        if not all(isinstance(r, str) and r for r in reads):
            raise ValidationError(f"step {self.name}: reads must be non-empty strings")
        deps = tuple(self.deps)
        if not all(isinstance(d, str) and d for d in deps):
            raise ValidationError(f"step {self.name}: deps must be non-empty strings")
        normalized: Dict[Tuple[Any, ...], Mapping[str, Any]] = {}
        for key, published in self.table.items():
            if not isinstance(key, (tuple, list)):
                raise ValidationError(
                    f"step {self.name}: table keys must be tuples/lists matching reads"
                )
            tkey = tuple(freeze(v) for v in key)
            if len(tkey) != len(reads):
                raise ValidationError(
                    f"step {self.name}: table key {list(tkey)!r} does not match "
                    f"{len(reads)} read(s)"
                )
            if not isinstance(published, Mapping):
                raise ValidationError(
                    f"step {self.name}: table entry for {list(tkey)!r} must map names to values"
                )
            if not published:
                raise ValidationError(
                    f"step {self.name}: table entry for {list(tkey)!r} publishes nothing"
                )
            if not all(isinstance(n, str) and n for n in published):
                raise ValidationError(
                    f"step {self.name}: published names must be non-empty strings"
                )
            normalized[tkey] = {n: freeze(v) for n, v in published.items()}
        if not normalized:
            raise ValidationError(f"step {self.name}: lookup table must not be empty")
        object.__setattr__(self, "reads", reads)
        object.__setattr__(self, "deps", deps)
        object.__setattr__(self, "table", normalized)
        if self.lock is not None and (not isinstance(self.lock, str) or not self.lock):
            raise ValidationError(f"step {self.name}: lock must be a non-empty string or None")


@dataclass(frozen=True)
class Pipeline:
    """A validated DAG of steps plus initial values and outputs of interest."""

    steps: Tuple[Step, ...]
    initial_values: Mapping[str, Any]
    outputs_of_interest: Tuple[str, ...]
    _index: Mapping[str, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        names = [s.name for s in self.steps]
        if len(names) != len(set(names)):
            raise ValidationError("step names must be unique")
        index = {name: i for i, name in enumerate(names)}
        for step in self.steps:
            unknown = [d for d in step.deps if d not in index]
            if unknown:
                raise ValidationError(f"step {step.name}: unknown dependencies {unknown}")
        if len(self.steps) > 12:
            raise ValidationError("at most 12 steps are supported")
        if not all(isinstance(n, str) and n for n in self.outputs_of_interest):
            raise ValidationError("outputs of interest must be non-empty strings")
        object.__setattr__(self, "_index", index)
        object.__setattr__(
            self,
            "initial_values",
            {str(k): freeze(v) for k, v in self.initial_values.items()},
        )
        object.__setattr__(self, "outputs_of_interest", tuple(self.outputs_of_interest))
        self._assert_acyclic(index)

    def _assert_acyclic(self, index: Mapping[str, int]) -> None:
        remaining = {i: set(index[d] for d in self.steps[i].deps) for i in range(len(self.steps))}
        ended: List[int] = []
        while remaining:
            ready = sorted(i for i, deps in remaining.items() if not deps)
            if not ready:
                raise ValidationError("dependency graph contains a cycle")
            for i in ready:
                ended.append(i)
                del remaining[i]
            for deps in remaining.values():
                deps.difference_update(ready)

    @property
    def index(self) -> Mapping[str, int]:
        return self._index

    def step(self, name: str) -> Step:
        return self.steps[self._index[name]]

    def topo_order(self) -> Tuple[str, ...]:
        order: List[str] = []
        done = set()
        remaining = list(self.steps)
        while remaining:
            progress = [s for s in remaining if all(d in done for d in s.deps)]
            for s in sorted(progress, key=lambda s: s.name):
                order.append(s.name)
                done.add(s.name)
            remaining = [s for s in remaining if s.name not in done]
        return tuple(order)


def build_pipeline(
    steps: Sequence[Step],
    initial_values: Optional[Mapping[str, Any]] = None,
    outputs_of_interest: Sequence[str] = (),
) -> Pipeline:
    """Validate and construct a :class:`Pipeline`."""
    return Pipeline(tuple(steps), dict(initial_values or {}), tuple(outputs_of_interest))


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------


class State:
    """Immutable runtime state: step statuses, frozen inputs and workspace."""

    __slots__ = ("status", "frozen", "_workspace", "_key", "_hash")

    def __init__(
        self,
        status: Sequence[str],
        frozen: Sequence[Optional[Tuple[Any, ...]]],
        workspace: Mapping[str, Any],
    ) -> None:
        self.status = tuple(status)
        self.frozen = tuple(frozen)
        self._workspace = dict(workspace)
        self._key = (
            self.status,
            self.frozen,
            frozenset(self._workspace.items()),
        )
        self._hash = hash(self._key)

    @classmethod
    def initial(cls, pipeline: Pipeline) -> "State":
        return cls(
            [PENDING] * len(pipeline.steps),
            [None] * len(pipeline.steps),
            dict(pipeline.initial_values),
        )

    @property
    def workspace(self) -> Mapping[str, Any]:
        return self._workspace

    def value(self, name: str) -> Any:
        return self._workspace.get(name, UNSET)

    def is_complete(self, pipeline: Pipeline) -> bool:
        return all(s == ENDED for s in self.status)

    def lock_owner(self, pipeline: Pipeline) -> Dict[str, str]:
        owners: Dict[str, str] = {}
        for i, step in enumerate(pipeline.steps):
            if self.status[i] == RUNNING and step.lock is not None:
                owners[step.lock] = step.name
        return owners

    def enabled_events(self, pipeline: Pipeline) -> List[Tuple[str, int]]:
        """Return enabled (kind, step-index) events sorted by event string.

        End events of running steps are always enabled.  A pending step may
        start once every dependency has ended and its executor (if any) is
        free.  Sorting by the canonical event string fixes the exploration
        order used for lexicographic tie-breaking.
        """
        events: List[Tuple[str, int]] = []
        busy_locks = {
            step.lock
            for i, step in enumerate(pipeline.steps)
            if self.status[i] == RUNNING and step.lock is not None
        }
        for i, step in enumerate(pipeline.steps):
            if self.status[i] == RUNNING:
                events.append(("end", i))
            elif self.status[i] == PENDING:
                deps_done = all(
                    self.status[pipeline.index[d]] == ENDED for d in step.deps
                )
                if deps_done and (step.lock is None or step.lock not in busy_locks):
                    events.append(("start", i))
        events.sort(key=lambda e: event_string(e, pipeline))
        return events

    def apply(self, event: Tuple[str, int], pipeline: Pipeline) -> "State":
        kind, i = event
        step = pipeline.steps[i]
        status = list(self.status)
        frozen: List[Optional[Tuple[Any, ...]]] = list(self.frozen)
        workspace = dict(self._workspace)
        if kind == "start":
            if status[i] != PENDING:
                raise TraceError(f"step {step.name} cannot start: status is {status[i]}")
            frozen[i] = tuple(self.value(name) for name in step.reads)
            status[i] = RUNNING
        elif kind == "end":
            if status[i] != RUNNING:
                raise TraceError(f"step {step.name} cannot end: status is {status[i]}")
            key = frozen[i]
            assert key is not None
            if key not in step.table:
                raise LookupTableError(
                    f"step {step.name}: lookup table has no entry for frozen input "
                    f"{tuple('UNSET' if v is UNSET else v for v in key)!r}"
                )
            workspace.update(step.table[key])
            status[i] = ENDED
            frozen[i] = None
        else:  # pragma: no cover - exhaustive
            raise TraceError(f"unknown event kind {kind!r}")
        return State(status, frozen, workspace)

    def output_tuple(self, pipeline: Pipeline) -> Tuple[Any, ...]:
        return tuple(self.value(name) for name in pipeline.outputs_of_interest)

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, State):
            return NotImplemented
        return self._key == other._key

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"State(status={self.status!r}, workspace={self._workspace!r})"


def event_string(event: Tuple[str, int], pipeline: Pipeline) -> str:
    kind, i = event
    return f"{kind}({pipeline.steps[i].name})"


def event_strings(events: Sequence[Tuple[str, int]], pipeline: Pipeline) -> List[str]:
    return [event_string(e, pipeline) for e in events]


def parse_event(text: str, pipeline: Pipeline) -> Tuple[str, int]:
    match = _EVENT_RE.match(text) if isinstance(text, str) else None
    if match is None:
        raise TraceError(f"malformed event {text!r}; expected 'start(name)' or 'end(name)'")
    kind, name = match.groups()
    if name not in pipeline.index:
        raise TraceError(f"event {text!r} references unknown step {name!r}")
    return kind, pipeline.index[name]


# ---------------------------------------------------------------------------
# Breakpoint snapshot (failure recovery)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """Breakpoint state used to resume an analysis after a failure.

    Attributes:
        pipeline: the pipeline this snapshot belongs to.
        statuses: step name -> pending/running/ended (default pending).
        frozen_inputs: running step name -> the input tuple it froze at start.
        published: workspace contents visible at the breakpoint.
        lock_holders: executor name -> name of the running step holding it.
    """

    pipeline: Pipeline
    statuses: Mapping[str, str]
    frozen_inputs: Mapping[str, Tuple[Any, ...]]
    published: Mapping[str, Any]
    lock_holders: Mapping[str, str]

    def __post_init__(self) -> None:
        p = self.pipeline
        statuses = {s.name: PENDING for s in p.steps}
        for name, st in self.statuses.items():
            if name not in p.index:
                raise ValidationError(f"snapshot references unknown step {name!r}")
            if st not in (PENDING, RUNNING, ENDED):
                raise ValidationError(f"snapshot: invalid status {st!r} for step {name}")
            statuses[name] = st
        frozen: Dict[str, Tuple[Any, ...]] = {}
        for name, values in self.frozen_inputs.items():
            if name not in p.index:
                raise ValidationError(f"snapshot frozen input for unknown step {name!r}")
            step = p.step(name)
            tup = tuple(freeze(v) for v in values)
            if len(tup) != len(step.reads):
                raise ValidationError(
                    f"snapshot: frozen input of {name} has {len(tup)} value(s), "
                    f"step reads {len(step.reads)}"
                )
            frozen[name] = tup
        for name, st in statuses.items():
            if st == RUNNING and name not in frozen:
                raise ValidationError(
                    f"snapshot: running step {name} is missing its frozen inputs"
                )
            if st != RUNNING and name in frozen:
                raise ValidationError(
                    f"snapshot: step {name} is {st} but carries frozen inputs"
                )
        # Legal-start consistency: anything that started had all deps ended.
        for name, st in statuses.items():
            if st in (RUNNING, ENDED):
                for dep in p.step(name).deps:
                    if statuses[dep] != ENDED:
                        raise ValidationError(
                            f"snapshot: step {name} is {st} but dependency {dep} "
                            f"is {statuses[dep]}"
                        )
        holders = dict(self.lock_holders)
        for lock, holder in holders.items():
            if holder not in p.index:
                raise ValidationError(f"snapshot: lock {lock!r} held by unknown step {holder!r}")
            if statuses[holder] != RUNNING:
                raise ValidationError(
                    f"snapshot: lock {lock!r} holder {holder} is not running"
                )
            if p.step(holder).lock != lock:
                raise ValidationError(
                    f"snapshot: step {holder} does not declare executor {lock!r}"
                )
        for name, st in statuses.items():
            lock = p.step(name).lock
            if st == RUNNING and lock is not None and holders.get(lock) != name:
                raise ValidationError(
                    f"snapshot: running step {name} must hold its executor {lock!r}"
                )
        object.__setattr__(self, "statuses", statuses)
        object.__setattr__(self, "frozen_inputs", frozen)
        object.__setattr__(
            self, "published", {str(k): freeze(v) for k, v in self.published.items()}
        )
        object.__setattr__(self, "lock_holders", holders)

    def to_state(self) -> State:
        p = self.pipeline
        status = [self.statuses[s.name] for s in p.steps]
        frozen = [
            self.frozen_inputs[s.name] if s.name in self.frozen_inputs else None
            for s in p.steps
        ]
        return State(status, frozen, dict(self.published))


# ---------------------------------------------------------------------------
# Analysis result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisResult:
    """Outcome of exploring every legal interleaving from one analysis root."""

    deterministic: bool
    possible_outputs: Tuple[Tuple[Any, ...], ...]
    final_outputs: Optional[Tuple[Any, ...]]
    traces: Tuple[List[str], List[str]]
    witness_outputs: Optional[Tuple[Tuple[Any, ...], Tuple[Any, ...]]]
    states_explored: int
    transitions: int
    root: str
    shared_prefix_length: int = 0

    def to_dict(self) -> Dict[str, Any]:
        def convert(v: Any) -> Any:
            if v is UNSET:
                return {"$unset": True}
            if isinstance(v, tuple):
                return [convert(x) for x in v]
            if isinstance(v, frozenset):
                return [convert(x) for x in v]
            return v

        return {
            "deterministic": self.deterministic,
            "root": self.root,
            "states_explored": self.states_explored,
            "transitions": self.transitions,
            "shared_prefix_length": self.shared_prefix_length,
            "possible_outputs": [convert(o) for o in self.possible_outputs],
            "final_outputs": convert(self.final_outputs) if self.final_outputs is not None else None,
            "witness_outputs": (
                [convert(self.witness_outputs[0]), convert(self.witness_outputs[1])]
                if self.witness_outputs is not None
                else None
            ),
            "traces": [list(self.traces[0]), list(self.traces[1])] if self.traces else [],
        }


# ---------------------------------------------------------------------------
# Exploration engine
# ---------------------------------------------------------------------------


class _Engine:
    def __init__(self, pipeline: Pipeline, root: State, root_name: str) -> None:
        self.pipeline = pipeline
        self.root = root
        self.root_name = root_name
        self._outputs_cache: Dict[State, Tuple[frozenset, Dict[Any, Tuple[Any, ...]]]] = {}
        self._divergence_cache: Dict[
            State, Optional[Tuple[int, Tuple[Tuple[str, ...], Tuple[str, ...]]]]
        ] = {}
        # Memoized locator: state -> (max depth to a divergence state,
        # lexicographically smallest next event on a best path, leaf pair).
        self._locate_cache: Dict[
            State, Optional[Tuple[int, str, Tuple[Tuple[str, ...], Tuple[str, ...]]]]
        ] = {}
        self._local_cache: Dict[
            State, Optional[Tuple[Tuple[str, ...], Tuple[str, ...]]]
        ] = {}
        self.states_explored = 0
        self.transitions = 0

    def explore_outputs(
        self, state: State
    ) -> Tuple[frozenset, Dict[Tuple[Any, ...], Tuple[str, ...]]]:
        cached = self._outputs_cache.get(state)
        if cached is not None:
            return cached
        self.states_explored += 1
        if state.is_complete(self.pipeline):
            result = (frozenset((state.output_tuple(self.pipeline),)), {})
            self._outputs_cache[state] = result
            return result

        possible: set = set()
        best_suffix: Dict[Tuple[Any, ...], Tuple[str, ...]] = {}
        for event in state.enabled_events(self.pipeline):
            self.transitions += 1
            nxt = state.apply(event, self.pipeline)
            sub_outs, sub_suffix = self.explore_outputs(nxt)
            label = event_string(event, self.pipeline)
            possible.update(sub_outs)
            for out in sub_outs:
                candidate: Tuple[str, ...]
                if nxt.is_complete(self.pipeline):
                    candidate = (label,)
                else:
                    candidate = (label,) + sub_suffix[out]
                if out not in best_suffix or candidate < best_suffix[out]:
                    best_suffix[out] = candidate
        result = (frozenset(possible), best_suffix)
        self._outputs_cache[state] = result
        return result

    def _local_divergence(
        self, state: State
    ) -> Optional[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
        """Lex-smallest witness pair splitting across two events of ``state``.

        Returns ``None`` when every enabled event's subtree yields the same
        set of outputs (i.e. no divergence can start at this state).
        """
        if state in self._local_cache:
            return self._local_cache[state]
        events = state.enabled_events(self.pipeline)
        child_info = []
        for event in events:
            nxt = state.apply(event, self.pipeline)
            sub_outs, sub_suffix = self.explore_outputs(nxt)
            child_info.append((event, nxt, sub_outs, sub_suffix))
        local_pair: Optional[Tuple[Tuple[str, ...], Tuple[str, ...]]] = None
        for a in range(len(child_info)):
            event_a, nxt_a, outs_a, suffix_a = child_info[a]
            for b in range(a + 1, len(child_info)):
                event_b, nxt_b, outs_b, suffix_b = child_info[b]
                if outs_a == outs_b:
                    continue
                label_a = event_string(event_a, self.pipeline)
                label_b = event_string(event_b, self.pipeline)
                # Sorted output keys give a deterministic, total tie-break.
                for out_a in sorted(outs_a, key=order_key):
                    for out_b in sorted(outs_b, key=order_key):
                        if out_a == out_b:
                            continue
                        trace_a = (label_a,) + (
                            () if nxt_a.is_complete(self.pipeline) else suffix_a[out_a]
                        )
                        trace_b = (label_b,) + (
                            () if nxt_b.is_complete(self.pipeline) else suffix_b[out_b]
                        )
                        pair = tuple(sorted((trace_a, trace_b)))
                        if local_pair is None or pair < local_pair:
                            local_pair = pair
        self._local_cache[state] = local_pair
        return local_pair

    def _locate_divergence(
        self, state: State
    ) -> Optional[Tuple[int, str, Tuple[Tuple[str, ...], Tuple[str, ...]]]]:
        """Memoized locator for the deepest reachable divergence state.

        Returns ``(depth, next_event, leaf_pair)`` where ``depth`` is the
        number of events shared on the way from ``state`` to the deepest
        state at which two enabled events lead to different output sets,
        ``next_event`` is the first shared event on a best path, and
        ``leaf_pair`` is the lexicographically smallest local witness pair
        at the chosen divergence state (used only for equal-depth
        tie-breaking).  Returns ``None`` for deterministic subtrees.

        Full traces are never lifted through the recursion: each memoized
        entry stores one short label plus one pair built at a leaf, so the
        total work stays linear in the number of memoized states times the
        number of enabled events.
        """
        if state in self._locate_cache:
            return self._locate_cache[state]
        outs, _ = self.explore_outputs(state)
        if len(outs) < 2:
            self._locate_cache[state] = None
            return None

        candidates: List[
            Tuple[int, str, Tuple[Tuple[str, ...], Tuple[str, ...]]]
        ] = []
        # Local cross-event split at this state: depth zero.
        local_pair = self._local_divergence(state)
        if local_pair is not None:
            candidates.append((0, "", local_pair))
        for event in state.enabled_events(self.pipeline):
            nxt = state.apply(event, self.pipeline)
            sub_outs, _ = self.explore_outputs(nxt)
            if len(sub_outs) < 2:
                continue
            child = self._locate_divergence(nxt)
            assert child is not None
            child_depth, _child_label, leaf_pair = child
            candidates.append(
                (child_depth + 1, event_string(event, self.pipeline), leaf_pair)
            )
        assert candidates
        # Primary: maximum depth.  Secondary: smallest next event.
        # Tertiary: smallest leaf witness pair, which fully determines the
        # lexicographic order of the resulting sorted full-trace pair.
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        result = candidates[0]
        self._locate_cache[state] = result
        return result

    def best_divergence(
        self, state: State
    ) -> Optional[Tuple[int, Tuple[Tuple[str, ...], Tuple[str, ...]]]]:
        """Return (shared-prefix depth, lex-smallest full suffix trace pair).

        A ``None`` result means the subtree is deterministic.  Phase one
        (:meth:`_locate_divergence`) walks memoized depth information to the
        deepest divergence state; phase two builds the lexicographically
        smallest witness pair at that state once and prepends the single
        shared prefix path.  The work per state is bounded by the number of
        enabled events, independent of how many full traces exist.
        """
        if state in self._divergence_cache:
            return self._divergence_cache[state]
        located = self._locate_divergence(state)
        if located is None:
            self._divergence_cache[state] = None
            return None
        depth, _first_label, leaf_pair = located

        # Walk the chosen prefix path, always taking the locator's best event.
        prefix: List[str] = []
        current = state
        remaining = depth
        while remaining > 0:
            info = self._locate_divergence(current)
            assert info is not None and info[0] == remaining
            label = info[1]
            prefix.append(label)
            event = next(
                e for e in current.enabled_events(self.pipeline)
                if event_string(e, self.pipeline) == label
            )
            current = current.apply(event, self.pipeline)
            remaining -= 1

        local_pair = self._local_divergence(current)
        assert local_pair is not None
        # The leaf pair selected by the locator tie-break must be the one
        # returned; recompute and assert agreement to keep the walk honest.
        assert local_pair == leaf_pair
        full_pair = (
            tuple(prefix) + local_pair[0],
            tuple(prefix) + local_pair[1],
        )
        result = (depth, full_pair)
        self._divergence_cache[state] = result
        return result

    def run(self) -> AnalysisResult:
        outs, _ = self.explore_outputs(self.root)
        ordered = tuple(sorted(outs, key=order_key))
        if len(outs) == 1:
            return AnalysisResult(
                deterministic=True,
                possible_outputs=ordered,
                final_outputs=ordered[0],
                traces=([], []),
                witness_outputs=None,
                states_explored=self.states_explored,
                transitions=self.transitions,
                root=self.root_name,
                shared_prefix_length=0,
            )
        depth, pair = self.best_divergence(self.root)
        # depth is the number of prefix events the two witnesses share; the
        # returned pair already consists of full traces starting at the root.
        trace_a, trace_b = pair
        # Attach the concrete output each witness trace reaches.
        out_a = _replay_outputs(self.pipeline, self.root, trace_a)
        out_b = _replay_outputs(self.pipeline, self.root, trace_b)
        return AnalysisResult(
            deterministic=False,
            possible_outputs=ordered,
            final_outputs=None,
            traces=(list(trace_a), list(trace_b)),
            witness_outputs=(out_a, out_b),
            states_explored=self.states_explored,
            transitions=self.transitions,
            root=self.root_name,
            shared_prefix_length=depth,
        )


def _replay_outputs(pipeline: Pipeline, root: State, trace: Sequence[str]) -> Tuple[Any, ...]:
    state = root
    for text in trace:
        event = parse_event(text, pipeline)
        if event not in state.enabled_events(pipeline):
            raise TraceError(
                f"event {text!r} is not enabled in state {state.status!r}"
            )
        state = state.apply(event, pipeline)
    if not state.is_complete(pipeline):
        raise TraceError("witness trace does not end in a complete state")
    return state.output_tuple(pipeline)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze(pipeline: Pipeline) -> AnalysisResult:
    """Explore all legal interleavings of a fresh pipeline run."""
    root = State.initial(pipeline)
    return _Engine(pipeline, root, "initial").run()


def analyze_snapshot(snapshot: Snapshot) -> AnalysisResult:
    """Explore all legal continuations from a post-failure breakpoint."""
    return _Engine(snapshot.pipeline, snapshot.to_state(), "snapshot").run()


@dataclass(frozen=True)
class ReplayOutcome:
    """Result of replaying one concrete start/end event trace."""

    trace: Tuple[str, ...]
    statuses: Tuple[str, ...]
    workspace: Mapping[str, Any]
    outputs: Tuple[Any, ...]
    lock_owner: Mapping[str, str]

    def to_dict(self) -> Dict[str, Any]:
        def convert(v: Any) -> Any:
            if v is UNSET:
                return {"$unset": True}
            if isinstance(v, tuple):
                return [convert(x) for x in v]
            if isinstance(v, frozenset):
                return [convert(x) for x in v]
            return v

        return {
            "trace": list(self.trace),
            "statuses": list(self.statuses),
            "workspace": {k: convert(v) for k, v in self.workspace.items()},
            "outputs": [convert(v) for v in self.outputs],
            "lock_owner": dict(self.lock_owner),
        }


def _replay(pipeline: Pipeline, root: State, trace: Sequence[str]) -> ReplayOutcome:
    state = root
    consumed: List[str] = []
    for item in trace:
        text = item if isinstance(item, str) else f"{item[0]}({item[1]})"
        event = parse_event(text, pipeline)
        if event not in state.enabled_events(pipeline):
            raise TraceError(
                f"event {text!r} is not enabled; step statuses are {state.status}"
            )
        state = state.apply(event, pipeline)
        consumed.append(text)
    return ReplayOutcome(
        trace=tuple(consumed),
        statuses=state.status,
        workspace=dict(state.workspace),
        outputs=state.output_tuple(pipeline),
        lock_owner=state.lock_owner(pipeline),
    )


def replay(pipeline: Pipeline, trace: Sequence[str]) -> ReplayOutcome:
    """Replay a full trace against the pipeline's initial state."""
    return _replay(pipeline, State.initial(pipeline), trace)


def replay_snapshot(snapshot: Snapshot, trace: Sequence[str]) -> ReplayOutcome:
    """Replay a continuation trace from a breakpoint snapshot.

    Legality (enabled-event checks, lock ownership, preserved frozen inputs)
    is enforced entirely through the runtime state rebuilt from the snapshot.
    """
    return _replay(snapshot.pipeline, snapshot.to_state(), trace)
