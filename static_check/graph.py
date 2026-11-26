"""Loading and normalization for restricted error-flow graphs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .model import DiagnosticUse, Finding


TYPE_ALIASES = {
    "start": "start",
    "begin": "start",
    "entry": "start",
    "task": "task",
    "step": "task",
    "activity": "task",
    "job": "task",
    "work": "task",
    "raise": "raise",
    "throw": "raise",
    "fault": "raise",
    "error-source": "raise",
    "error_source": "raise",
    "source-error": "raise",
    "wrap": "wrap",
    "wrapping": "wrap",
    "exception-wrap": "wrap",
    "exception_wrapper": "wrap",
    "wrapper": "wrap",
    "recover": "recover",
    "recovery": "recover",
    "catch": "recover",
    "handler": "recover",
    "exception-handler": "recover",
    "replace": "replace",
    "replacement": "replace",
    "fallback": "replace",
    "aggregate": "aggregate",
    "aggregator": "aggregate",
    "aggregate-error": "aggregate",
    "join": "aggregate",
    "join-errors": "aggregate",
    "all-errors": "aggregate",
    "filter": "filter",
    "redact": "filter",
    "identity-filter": "filter",
    "response": "response",
    "exit": "response",
    "respond": "response",
    "output": "response",
    "error-response": "response",
    "ok-response": "response",
    "success-response": "response",
}

ERROR_TYPES = {"raise", "wrap", "recover", "replace", "aggregate", "filter"}


@dataclass
class NormalizedGraph:
    raw: dict[str, Any]
    nodes: dict[str, dict[str, Any]]
    edges: list[dict[str, Any]]
    entries: tuple[str, ...]
    response_ids: tuple[str, ...]
    findings: list[Finding] = field(default_factory=list)

    @property
    def entry(self) -> str | None:
        return self.entries[0] if self.entries else None

    def outgoing(self, node_id: str) -> list[dict[str, Any]]:
        return [edge for edge in self.edges if edge["from"] == node_id]

    def incoming(self, node_id: str) -> list[dict[str, Any]]:
        return [edge for edge in self.edges if edge["to"] == node_id]


def load_graph(graph: Any) -> NormalizedGraph:
    """Accept a dict, JSON document/path, or an already normalized graph."""

    if isinstance(graph, NormalizedGraph):
        return graph
    if isinstance(graph, (str, os.PathLike)):
        text = os.fspath(graph)
        # Treat an existing path or a one-line path string as a file only when
        # it exists.  Otherwise try JSON parsing for inline JSON documents.
        path = Path(text)
        if len(text) < 4096 and path.exists() and path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = json.loads(text)
    elif isinstance(graph, Mapping):
        raw = dict(graph)
    elif isinstance(graph, list):
        raw = {"nodes": graph}
    else:
        raise TypeError("graph must be a mapping, list, JSON string, or path")
    return normalize_graph(raw)


def normalize_graph(raw: dict[str, Any]) -> NormalizedGraph:
    findings: list[Finding] = []
    node_list = raw.get("nodes") or raw.get("steps") or raw.get("vertices") or []
    if isinstance(node_list, Mapping):
        node_list = [dict(value, id=key) if isinstance(value, Mapping) else value
                     for key, value in node_list.items()]

    nodes: dict[str, dict[str, Any]] = {}
    for index, original in enumerate(node_list):
        if not isinstance(original, Mapping):
            findings.append(
                Finding(
                    "INVALID_GRAPH",
                    f"node at index {index} is not an object",
                    node_id=str(index),
                )
            )
            continue
        node = dict(original)
        node_id = str(
            node.get("id")
            or node.get("name")
            or node.get("key")
            or f"node-{index}"
        )
        node["id"] = node_id
        raw_type = str(
            node.get("type")
            or node.get("kind")
            or node.get("node_type")
            or node.get("op")
            or "task"
        ).strip().lower()
        node["raw_type"] = raw_type
        node["type"] = TYPE_ALIASES.get(raw_type, raw_type.replace(" ", "-"))
        if node["id"] in nodes:
            findings.append(
                Finding(
                    "DUPLICATE_NODE_ID",
                    f"duplicate node id {node['id']!r}",
                    node_id=node["id"],
                )
            )
        nodes[node["id"]] = node

    edges = _normalize_edges(raw, nodes, findings)
    entries = _find_entries(raw, nodes, edges, findings)
    response_ids = tuple(
        node_id for node_id, node in nodes.items() if node["type"] == "response"
    )

    for edge in edges:
        if edge["from"] not in nodes:
            findings.append(
                Finding(
                    "INVALID_EDGE_SOURCE",
                    f"edge source {edge['from']!r} does not exist",
                    node_id=edge.get("id"),
                )
            )
        if edge["to"] not in nodes:
            findings.append(
                Finding(
                    "INVALID_EDGE_TARGET",
                    f"edge target {edge['to']!r} does not exist",
                    node_id=edge.get("id"),
                )
            )

    if not entries:
        findings.append(Finding("NO_ENTRY", "graph has no reachable entry node"))

    return NormalizedGraph(
        raw=raw,
        nodes=nodes,
        edges=edges,
        entries=entries,
        response_ids=response_ids,
        findings=findings,
    )


def _normalize_edges(
    raw: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    findings: list[Finding],
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    explicit = raw.get("edges") or raw.get("transitions") or raw.get("links") or []
    if isinstance(explicit, Mapping):
        explicit = [dict(value, id=key) if isinstance(value, Mapping) else value
                    for key, value in explicit.items()]

    for index, item in enumerate(explicit):
        if isinstance(item, Mapping):
            source = item.get("from") or item.get("source") or item.get("src")
            target = item.get("to") or item.get("target") or item.get("dst")
            branch = item.get("branch") or item.get("on") or item.get("when") or item.get("label")
            edge = {
                "id": str(item.get("id") or f"edge-{index}"),
                "from": str(source) if source is not None else None,
                "to": str(target) if target is not None else None,
                "branch": normalize_branch(branch),
                "raw": dict(item),
            }
            if item.get("input") or item.get("input_name"):
                edge["input"] = str(item.get("input") or item.get("input_name"))
            edges.append(edge)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            edges.append(
                {
                    "id": f"edge-{index}",
                    "from": str(item[0]),
                    "to": str(item[1]),
                    "branch": normalize_branch(item[2] if len(item) > 2 else None),
                    "raw": {},
                }
            )
        else:
            findings.append(Finding("INVALID_EDGE", f"edge {index} is invalid"))

    # Convenience: node-declared transitions.  These make small test graphs
    # readable while the canonical representation remains edge-list based.
    for node_id, node in nodes.items():
        transition_keys = [
            ("next", "any"),
            ("to", "any"),
            ("then", "any"),
            ("success_to", "normal"),
            ("on_success", "normal"),
            ("ok_to", "normal"),
            ("normal_to", "normal"),
            ("error_to", "error"),
            ("on_error", "error"),
            ("catch_to", "error"),
            ("handler_to", "error"),
            ("exception_to", "error"),
            ("recovery_to", "any"),
            ("recover_to", "any"),
        ]
        for key, branch in transition_keys:
            target = node.get(key)
            if target is None:
                continue
            targets = target if isinstance(target, (list, tuple, set)) else [target]
            for target_id in targets:
                edges.append(
                    {
                        "id": f"implicit-{node_id}-{key}-{target_id}",
                        "from": node_id,
                        "to": str(target_id),
                        "branch": normalize_branch(branch),
                        "raw": {key: target_id},
                    }
                )

    # Aggregate inputs often name nodes directly.  Turn those references into
    # member edges so reachability and topological evaluation see them.
    for node_id, node in nodes.items():
        if node.get("type") != "aggregate":
            continue
        for key in ("inputs", "input_nodes", "depends_on"):
            value = node.get(key)
            if value is None:
                continue
            refs = value if isinstance(value, (list, tuple, set)) else [value]
            for ref in refs:
                target = None
                if isinstance(ref, str) and ref in nodes:
                    target = ref
                elif isinstance(ref, Mapping):
                    target = ref.get("node") or ref.get("ref") or ref.get("id")
                if target is not None and str(target) in nodes:
                    edges.append(
                        {
                            "id": f"aggregate-{node_id}-{target}",
                            "from": str(target),
                            "to": node_id,
                            "branch": "member",
                            "raw": {key: ref},
                        }
                    )

    # If no edge list was supplied and nodes look like a pipeline, connect the
    # declared order.  Explicit next/error keys above still work.
    if not explicit and not edges and len(nodes) > 1:
        ordered_ids = list(nodes)
        for index in range(len(ordered_ids) - 1):
            edges.append(
                {
                    "id": f"sequence-{index}",
                    "from": ordered_ids[index],
                    "to": ordered_ids[index + 1],
                    "branch": "any",
                    "raw": {},
                }
            )

    # De-duplicate exact edges.
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        if edge["from"] is None or edge["to"] is None:
            findings.append(Finding("INVALID_EDGE", f"edge lacks endpoints: {edge}"))
            continue
        key = (edge["from"], edge["to"], edge["branch"])
        unique.setdefault(key, edge)
    return list(unique.values())


def normalize_branch(value: Any) -> str:
    if value is None:
        return "any"
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if text in {"error", "exception", "failure", "failed", "catch", "caught", "on_error", "err", "throws", "throw"}:
        return "error"
    if text in {"normal", "success", "successful", "ok", "next", "then", "on_success", "resume", "response", "done"}:
        return "normal"
    if text in {"member", "input", "aggregate", "part"}:
        return "member"
    return "any"


def _find_entries(
    raw: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    findings: list[Finding],
) -> tuple[str, ...]:
    explicit = (
        raw.get("entry")
        or raw.get("entrypoint")
        or raw.get("start")
        or raw.get("start_node")
        or raw.get("root")
        or raw.get("roots")
        or raw.get("entries")
    )
    if explicit is not None:
        values = explicit if isinstance(explicit, (list, tuple, set)) else [explicit]
        result = tuple(str(value) for value in values)
        for node_id in result:
            if node_id not in nodes:
                findings.append(
                    Finding("INVALID_ENTRY", f"entry node {node_id!r} does not exist")
                )
        return tuple(node_id for node_id in result if node_id in nodes)

    starts = tuple(node_id for node_id, node in nodes.items() if node["type"] == "start")
    if starts:
        return starts[:1]

    targets = {edge["to"] for edge in edges}
    sources = {edge["from"] for edge in edges}
    roots = tuple(node_id for node_id in nodes if node_id not in targets)
    if roots:
        return roots
    if nodes:
        return (next(iter(nodes)),)
    return ()


def diagnostic_specs(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, Mapping)):
        return [value]
    if isinstance(value, DiagnosticUse):
        return [value]
    result: list[Any] = []
    for item in value:
        if isinstance(item, (list, tuple, set)):
            result.extend(diagnostic_specs(item))
        else:
            result.append(item)
    return result


def parse_diagnostics(
    value: Any,
    node_id: str,
    *,
    default_source: str | None = None,
    route: tuple[str, ...] | None = None,
) -> tuple[DiagnosticUse, ...]:
    result: list[DiagnosticUse] = []
    for item in diagnostic_specs(value):
        if isinstance(item, DiagnosticUse):
            result.append(item)
            continue
        if isinstance(item, str):
            diag_id = item
            source = default_source or node_id
            locations: tuple[str, ...] = ()
        elif isinstance(item, Mapping):
            diag_id = str(
                item.get("id")
                or item.get("diagnostic_id")
                or item.get("identifier")
                or item.get("name")
                or item.get("ref")
            )
            source = str(
                item.get("source")
                or item.get("source_id")
                or item.get("origin")
                or item.get("provenance")
                or default_source
                or node_id
            )
            loc = item.get("locations") or item.get("location") or ()
            if isinstance(loc, str):
                locations = (loc,)
            else:
                locations = tuple(str(x) for x in loc)
        else:
            diag_id = str(getattr(item, "id", item))
            source = str(getattr(item, "source", default_source or node_id))
            locations = tuple(getattr(item, "locations", ()) or ())
        result.append(
            DiagnosticUse(
                diagnostic_id=diag_id,
                source=source,
                origin_node=node_id,
                route=route or (node_id,),
                locations=locations,
            )
        )
    # De-duplicate exact IDs from the same source/origin, but preserve the same
    # ID from distinct sources because provenance matters.
    unique: dict[tuple[str, str, str], DiagnosticUse] = {}
    for use in result:
        unique[(use.diagnostic_id, use.source, use.origin_node)] = use
    return tuple(unique.values())


def first_present(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default
