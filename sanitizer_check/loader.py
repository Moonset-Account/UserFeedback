"""Load and validate rule-table and data-flow-graph JSON documents.

The documents are treated as untrusted input: every required key is checked
and structural problems raise :class:`LoaderError` instead of crashing with a
raw KeyError.
"""

from __future__ import annotations

import json

from .errors import LoaderError
from .model import Function, Graph, Node, RuleTable, VALID_KINDS


# ---------------------------------------------------------------------------
# Rule tables
# ---------------------------------------------------------------------------

def load_rule_table(path: str) -> RuleTable:
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return parse_rule_table(doc)


def parse_rule_table(doc: dict) -> RuleTable:
    if not isinstance(doc, dict):
        raise LoaderError("rule table must be a JSON object")
    version = doc.get("version")
    if not isinstance(version, str) or not version:
        raise LoaderError("rule table requires a non-empty string 'version'")
    raw = doc.get("sanitizers", {})
    if not isinstance(raw, dict):
        raise LoaderError("rule table 'sanitizers' must be an object")
    applicability = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not name:
            raise LoaderError("sanitizer names must be non-empty strings")
        if not isinstance(spec, dict):
            raise LoaderError(
                f"sanitizer '{name}' definition must be an object")
        cats = spec.get("categories", [])
        if not isinstance(cats, list) or not all(isinstance(c, str) for c in cats):
            raise LoaderError(
                f"sanitizer '{name}' categories must be a list of strings")
        applicability[name] = frozenset(cats)
    return RuleTable(version=version, applicability=applicability)


# ---------------------------------------------------------------------------
# Data-flow graph
# ---------------------------------------------------------------------------

def load_graph(path: str) -> Graph:
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return parse_graph(doc)


def parse_graph(doc: dict) -> Graph:
    if not isinstance(doc, dict):
        raise LoaderError("graph document must be a JSON object")
    graph_id = doc.get("graph_id", "unnamed-graph")
    if not isinstance(graph_id, str):
        raise LoaderError("graph_id must be a string")

    raw_nodes = doc.get("nodes", [])
    raw_edges = doc.get("edges", [])
    raw_funcs = doc.get("functions", [])
    if not isinstance(raw_nodes, list):
        raise LoaderError("'nodes' must be a list")
    if not isinstance(raw_edges, list):
        raise LoaderError("'edges' must be a list")
    if not isinstance(raw_funcs, list):
        raise LoaderError("'functions' must be a list")

    nodes: dict = {}
    incompleteness = set()
    for rn in raw_nodes:
        node = _parse_node(rn)
        if node.id in nodes:
            raise LoaderError(f"duplicate node id: {node.id}")
        nodes[node.id] = node
        if node.kind == "unmodeled":
            incompleteness.add(node.id)

    adj: dict = {nid: [] for nid in nodes}
    for re_ in raw_edges:
        src, dst = _parse_edge(re_, nodes)
        adj[src].append(dst)

    functions = {}
    for rf in raw_funcs:
        fn = _parse_function(rf, nodes)
        if fn.id in functions:
            raise LoaderError(f"duplicate function id: {fn.id}")
        functions[fn.id] = fn

    assumptions = tuple(doc.get("assumptions", ()))
    if not isinstance(assumptions, tuple) or not all(
            isinstance(a, str) for a in assumptions):
        raise LoaderError("'assumptions' must be a list of strings")

    watch = tuple(doc.get("watch", ()))
    for w in watch:
        _validate_watch(w, nodes)

    graph = Graph(
        graph_id=graph_id,
        nodes=nodes,
        adj=adj,
        functions=functions,
        watch=watch,
        assumptions=assumptions,
        incompleteness=frozenset(incompleteness),
    )
    _cross_check_calls(graph)
    return graph


def _parse_node(rn: dict) -> Node:
    if not isinstance(rn, dict):
        raise LoaderError("each node must be an object")
    nid = rn.get("id")
    kind = rn.get("kind")
    func = rn.get("function")
    if not isinstance(nid, str) or not nid:
        raise LoaderError("node requires a non-empty string 'id'")
    if kind not in VALID_KINDS:
        raise LoaderError(f"node {nid}: unknown kind {kind!r}")
    if not isinstance(func, str) or not func:
        raise LoaderError(f"node {nid}: requires a non-empty 'function'")

    def opt_str(key):
        val = rn.get(key)
        if val is not None and not isinstance(val, str):
            raise LoaderError(f"node {nid}: '{key}' must be a string")
        return val

    field_ = opt_str("field")
    sanitizers = tuple(rn.get("sanitizers", ()))
    if not isinstance(sanitizers, tuple) or not all(
            isinstance(s, str) for s in sanitizers):
        raise LoaderError(f"node {nid}: 'sanitizers' must be a list of strings")

    bindings_raw = rn.get("bindings", [])
    if not isinstance(bindings_raw, list):
        raise LoaderError(f"node {nid}: 'bindings' must be a list")
    bindings = []
    for b in bindings_raw:
        if (not isinstance(b, list) or len(b) != 2
                or not all(isinstance(x, str) for x in b)):
            raise LoaderError(
                f"node {nid}: each binding must be [formal, actual] strings")
        bindings.append((b[0], b[1]))

    return Node(
        id=nid,
        kind=kind,
        function=func,
        field=field_,
        source_label=opt_str("source_label"),
        sink_label=opt_str("sink_label"),
        category=opt_str("category"),
        sanitizers=sanitizers,
        callee=opt_str("callee"),
        bindings=tuple(bindings),
        ret_field=opt_str("ret_field"),
        reason=opt_str("reason"),
    )


def _parse_edge(re_: dict, nodes: dict):
    if not isinstance(re_, dict):
        raise LoaderError("each edge must be an object")
    src, dst = re_.get("src"), re_.get("dst")
    if not isinstance(src, str) or not isinstance(dst, str):
        raise LoaderError("edge requires string 'src' and 'dst'")
    if src not in nodes:
        raise LoaderError(f"edge references unknown node: {src}")
    if dst not in nodes:
        raise LoaderError(f"edge references unknown node: {dst}")
    return src, dst


def _parse_function(rf: dict, nodes: dict) -> Function:
    if not isinstance(rf, dict):
        raise LoaderError("each function must be an object")
    fid = rf.get("id")
    entry = rf.get("entry")
    returns = rf.get("returns", [])
    if not isinstance(fid, str) or not fid:
        raise LoaderError("function requires a non-empty 'id'")
    if not isinstance(entry, str) or entry not in nodes:
        raise LoaderError(f"function {fid}: entry node missing/unknown")
    if not isinstance(returns, list) or not all(
            isinstance(r, str) for r in returns):
        raise LoaderError(f"function {fid}: 'returns' must be a list of ids")
    for r in returns:
        if r not in nodes:
            raise LoaderError(f"function {fid}: unknown return node {r}")
    return Function(id=fid, entry=entry, returns=tuple(returns))


def _validate_watch(w: dict, nodes: dict) -> None:
    if not isinstance(w, dict):
        raise LoaderError("each watch entry must be an object")
    for key in ("sink", "field"):
        if not isinstance(w.get(key), str):
            raise LoaderError(f"watch entry requires string '{key}': {w}")
    if w["sink"] not in nodes:
        raise LoaderError(f"watch references unknown sink: {w['sink']}")
    if nodes[w["sink"]].kind != "sink":
        raise LoaderError(f"watch node is not a sink: {w['sink']}")


def _cross_check_calls(graph: Graph) -> None:
    """Record structural incompleteness: calls without a resolvable callee."""
    for node in graph.nodes.values():
        if node.kind == "call" and node.callee not in graph.functions:
            graph.incompleteness = graph.incompleteness | frozenset({node.id})
