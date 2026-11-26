"""Dependency graph construction and cycle search."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import CycleEdge


def _record_for(state: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return state.get("steps", {}).get(name, {})


def step_dependency_names(
    spec: Any,
    record: Mapping[str, Any],
    producers: Mapping[str, str],
    *,
    include_self: bool = True,
) -> set[str]:
    """Return step-level dependencies from static paths and old dynamic edges."""

    dependencies = set(getattr(spec, "depends_on", frozenset()))
    paths = set(getattr(spec, "static_dependencies", frozenset()))
    paths.update((record.get("dynamic_inputs") or {}).keys())
    for path in paths:
        producer = producers.get(path)
        if producer is not None and (include_self or producer != spec.name):
            dependencies.add(producer)
    if not include_self:
        dependencies.discard(spec.name)
    return dependencies


def build_adjacency(
    specs: Mapping[str, Any],
    producers: Mapping[str, str],
    state: Mapping[str, Any],
    override_step: str | None = None,
    override_dynamic_inputs: Mapping[str, Any] | None = None,
) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {name: set() for name in specs}
    for name, spec in specs.items():
        record = _record_for(state, name)
        if name == override_step and override_dynamic_inputs is not None:
            # Candidate manifest replaces the old dynamic manifest wholesale.
            record = {**record, "dynamic_inputs": dict(override_dynamic_inputs)}
        adjacency[name] = step_dependency_names(
            spec,
            record,
            producers,
            include_self=True,
        )
    return adjacency


def find_cycle(adjacency: Mapping[str, set[str]]) -> list[str] | None:
    """Return one closed loop ``[a, b, ..., a]`` or ``None``.

    Nodes and edges are sorted so diagnostics are deterministic.
    """

    color: dict[str, int] = {}  # 0 unseen, 1 on stack, 2 finished
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(adjacency.get(node, set())):
            if nxt not in adjacency:
                # Explicit forward dependency to an unregistered step is a
                # usage error elsewhere; do not invent a graph node here.
                continue
            if color.get(nxt, 0) == 1:
                start = stack.index(nxt)
                return stack[start:] + [nxt]
            if color.get(nxt, 0) == 0:
                found = dfs(nxt)
                if found is not None:
                    return found
        stack.pop()
        color[node] = 2
        return None

    for node in sorted(adjacency):
        if color.get(node, 0) == 0:
            found = dfs(node)
            if found is not None:
                return found
    return None


def describe_edge(
    source: str,
    target: str,
    specs: Mapping[str, Any],
    producers: Mapping[str, str],
    state: Mapping[str, Any],
    override_step: str | None = None,
    override_dynamic_inputs: Mapping[str, Any] | None = None,
) -> CycleEdge:
    spec = specs[source]
    old_record = _record_for(state, source)
    old_dynamic = set((old_record.get("dynamic_inputs") or {}).keys())
    candidate_dynamic = (
        set(override_dynamic_inputs.keys())
        if source == override_step and override_dynamic_inputs is not None
        else old_dynamic
    )

    # Prefer a file-backed explanation when one exists.
    static_paths = sorted(
        path for path in spec.static_dependencies if producers.get(path) == target
    )
    if static_paths:
        return CycleEdge(source, target, static_paths[0], "static", False)

    dynamic_paths = sorted(
        path for path in candidate_dynamic if producers.get(path) == target
    )
    if dynamic_paths:
        path = dynamic_paths[0]
        is_new = source == override_step and path not in old_dynamic
        return CycleEdge(source, target, path, "dynamic", is_new)

    if target in spec.depends_on:
        return CycleEdge(source, target, None, "explicit", False)

    return CycleEdge(source, target, None, "dependency", False)


def describe_cycle(
    cycle: list[str],
    specs: Mapping[str, Any],
    producers: Mapping[str, str],
    state: Mapping[str, Any],
    override_step: str | None = None,
    override_dynamic_inputs: Mapping[str, Any] | None = None,
) -> list[CycleEdge]:
    edges: list[CycleEdge] = []
    for source, target in zip(cycle, cycle[1:]):
        edges.append(
            describe_edge(
                source,
                target,
                specs,
                producers,
                state,
                override_step,
                override_dynamic_inputs,
            )
        )
    return edges
