"""Loaders for the four input records.

Every loader validates its file and raises ValueError with a precise
message on malformed input; the CLI turns that into exit code 2.

Input formats (all JSON):

entries.json     {"entries": [{"id", "name", "module"}, ...]}
graph.json       {"modules": [...], "edges": [[caller, callee], ...]}
checkpoints.json {"build": "...", "checkpoints": [{"id", "module", "description"?}, ...]}
traces.json      {"traces": [{"test_id", "build", "entry", "checkpoints": [...]}, ...]}
"""

import json
from typing import List, Tuple

from .model import CallGraph, Checkpoint, Entry, Trace


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc


def _require_str(raw, key, where):
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: missing or invalid string field {key!r}")
    return value


def load_entries(path: str) -> List[Entry]:
    data = _load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"{path}: expected an object with an 'entries' list")
    entries: List[Entry] = []
    seen = set()
    for i, raw in enumerate(data["entries"]):
        where = f"{path}: entries[{i}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: expected an object")
        entry_id = _require_str(raw, "id", where)
        module = _require_str(raw, "module", where)
        name = raw.get("name", entry_id)
        if not isinstance(name, str):
            raise ValueError(f"{where}: 'name' must be a string")
        if entry_id in seen:
            raise ValueError(f"{where}: duplicate entry id {entry_id!r}")
        seen.add(entry_id)
        entries.append(Entry(id=entry_id, name=name, module=module))
    return entries


def load_graph(path: str) -> CallGraph:
    data = _load_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected an object with 'modules' and 'edges'")
    modules = data.get("modules", [])
    edges = data.get("edges", [])
    if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
        raise ValueError(f"{path}: 'modules' must be a list of strings")
    if not isinstance(edges, list):
        raise ValueError(f"{path}: 'edges' must be a list of [caller, callee] pairs")
    graph = CallGraph(modules=set(modules))
    adjacency = {}
    for i, edge in enumerate(edges):
        where = f"{path}: edges[{i}]"
        if (
            not isinstance(edge, list)
            or len(edge) != 2
            or not all(isinstance(m, str) and m for m in edge)
        ):
            raise ValueError(f"{where}: expected a [caller, callee] pair of strings")
        caller, callee = edge
        graph.modules.update((caller, callee))
        adjacency.setdefault(caller, set()).add(callee)
        adjacency.setdefault(callee, adjacency.get(callee, set()))
    # Deterministic ordering for reproducible reports.
    graph.adjacency = {m: sorted(targets) for m, targets in adjacency.items()}
    return graph


def load_checkpoints(path: str) -> Tuple[str, List[Checkpoint]]:
    data = _load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("checkpoints"), list):
        raise ValueError(f"{path}: expected an object with a 'checkpoints' list")
    build = data.get("build")
    if not isinstance(build, str) or not build:
        raise ValueError(f"{path}: missing or invalid 'build' field")
    checkpoints: List[Checkpoint] = []
    seen = set()
    for i, raw in enumerate(data["checkpoints"]):
        where = f"{path}: checkpoints[{i}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: expected an object")
        cp_id = _require_str(raw, "id", where)
        module = _require_str(raw, "module", where)
        description = raw.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"{where}: 'description' must be a string")
        if cp_id in seen:
            raise ValueError(f"{where}: duplicate checkpoint id {cp_id!r}")
        seen.add(cp_id)
        checkpoints.append(Checkpoint(id=cp_id, module=module, description=description))
    return build, checkpoints


def load_traces(path: str) -> List[Trace]:
    data = _load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("traces"), list):
        raise ValueError(f"{path}: expected an object with a 'traces' list")
    traces: List[Trace] = []
    for i, raw in enumerate(data["traces"]):
        where = f"{path}: traces[{i}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: expected an object")
        test_id = _require_str(raw, "test_id", where)
        build = _require_str(raw, "build", where)
        entry_id = _require_str(raw, "entry", where)
        checkpoints = raw.get("checkpoints")
        if (
            not isinstance(checkpoints, list)
            or not checkpoints
            or not all(isinstance(c, str) and c for c in checkpoints)
        ):
            raise ValueError(f"{where}: 'checkpoints' must be a non-empty list of strings")
        # A single test may hit several checkpoints; keep each one so the
        # analyzer can record them separately. De-duplicate exact repeats.
        traces.append(
            Trace(
                test_id=test_id,
                build=build,
                entry_id=entry_id,
                checkpoints=tuple(dict.fromkeys(checkpoints)),
            )
        )
    return traces
