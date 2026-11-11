"""Sink verdicts, shortest witness paths and version classification."""

from __future__ import annotations

from collections import deque

from .model import Graph, PathState, RuleTable, diff_rule_tables

# Verdict labels -----------------------------------------------------------
ALLOWED = "allowed"
BLOCKED = "blocked"
UNDETERMINED = "undetermined"
UNREACHABLE = "unreachable"

# Classification labels (deliverable buckets) ------------------------------
NEWLY_ALLOWED = "newly_allowed"
NEWLY_BLOCKED = "newly_blocked"
UNCHANGED = "unchanged"
UNDETERMINED_BUCKET = "undetermined"
UNREACHABLE_BUCKET = "unreachable"


# ---------------------------------------------------------------------------
# Sink-level evaluation
# ---------------------------------------------------------------------------

def evaluate_sink(states, rule_table: RuleTable, category: str) -> dict:
    """Evaluate one (sink, field) pair under one rule version.

    Three-way verdict:
      ALLOWED       - every reaching path is tainted-and-covered;
      BLOCKED       - at least one reaching path is tainted and open.  Paths
                      newly covered by the rule are still reported as covered
                      alternatives so a partial release shows up in the diff;
      UNDETERMINED  - some reaching path has an unknown (unmodelled) origin;
      UNREACHABLE   - no reaching path state at all.
    """
    states = frozenset(states)
    if not states:
        return {
            "verdict": UNREACHABLE,
            "alternatives": [],
            "effective_sanitizers": [],
        }

    alternatives = []
    any_unknown = False
    any_tainted_open = False
    any_tainted_covered = False

    for ps in sorted(states, key=_state_sort_key):
        effective = tuple(
            s for s in ps.sanitizers
            if rule_table.covers(s, category)
        )
        is_open = ps.tainted and not effective
        if ps.unknown:
            any_unknown = True
        if is_open:
            any_tainted_open = True
        elif ps.tainted:
            any_tainted_covered = True
        alternatives.append({
            "tainted": ps.tainted,
            "unknown": ps.unknown,
            "sanitizers_on_path": list(ps.sanitizers),
            "effective_sanitizers": list(effective),
            "origins": list(ps.origins),
            "path_is_open": is_open,
        })

    effective_union = sorted({
        s for alt in alternatives for s in alt["effective_sanitizers"]
    })
    if any_unknown:
        verdict = UNDETERMINED
    elif any_tainted_open:
        verdict = BLOCKED
    elif any_tainted_covered:
        verdict = ALLOWED
    else:
        # Values present but untainted (e.g. constant-only flows).
        verdict = ALLOWED
    return {
        "verdict": verdict,
        "alternatives": alternatives,
        "effective_sanitizers": effective_union,
    }


def _state_sort_key(ps: PathState):
    return (not ps.tainted, ps.unknown, ps.sanitizers, ps.origins)


# ---------------------------------------------------------------------------
# Watch items
# ---------------------------------------------------------------------------

def derive_watch_items(graph: Graph):
    """Explicit watch declarations win; otherwise one item per sink/field."""
    if graph.watch:
        return [dict(w) for w in graph.watch]
    items = []
    for node in sorted(graph.nodes.values(), key=lambda n: n.id):
        if node.kind == "sink" and node.field is not None:
            items.append({
                "sink": node.id,
                "field": node.field,
                "name": f"{node.id}:{node.field}",
            })
    return items


# ---------------------------------------------------------------------------
# Shortest witness path (BFS over the local supergraph)
# ---------------------------------------------------------------------------

def find_witness(graph: Graph, sink_id: str, field_name: str,
                 require_open: bool, rule_table: RuleTable,
                 node_envs: dict):
    """Breadth-first search for the shortest source -> sink witness.

    The search walks the local *supergraph*: at a resolved call node it
    descends into the callee entry (following the formal/actual bindings) and
    rejoins at the caller-side successor after the callee return, so the
    witness includes the statements where interprocedural sanitization
    happens.

    require_open=True looks for a path reaching the sink with tainted data and
    no effective sanitizer; otherwise any tainted reaching path is accepted.
    Returns a tuple of node ids, or None.
    """
    sink = graph.nodes[sink_id]
    category = sink.category
    start_candidates = [
        nid for nid, n in graph.nodes.items() if n.kind == "source"
    ]
    best = None
    for source_id in start_candidates:
        result = _bfs_path(graph, source_id, sink_id, field_name,
                           require_open, rule_table, category, node_envs)
        if result is not None and (best is None or len(result) < len(best)):
            best = result
    if best is None:
        for source_id in start_candidates:
            result = _bfs_path(graph, source_id, sink_id, field_name,
                               False, None, category, node_envs)
            if result is not None and (best is None or len(result) < len(best)):
                best = result
    return best


def _supergraph_successors(graph: Graph, node_id: str, post_return: bool):
    """Yield (next_node_id, next_post_return) supergraph edges.

    A resolved call descends into the callee instead of following the plain
    edge, so the witness must traverse callee statements.  A callee return
    rejoins its caller-side call node (post_return=True), after which normal
    control flow continues.
    """
    node = graph.nodes[node_id]
    if node.kind == "call" and not post_return \
            and node.callee in graph.functions:
        yield graph.functions[node.callee].entry, False
        return
    if node.kind == "return":
        for caller in graph.nodes.values():
            if (caller.kind == "call"
                    and caller.callee == node.function):
                yield caller.id, True
        return
    for succ in graph.successors(node_id):
        yield succ, False


def _bfs_path(graph, source_id, sink_id, field_name,
              require_open, rule_table, category, node_envs):
    # State is (node_id, post_return).  post_return=True means we arrived at a
    # call node from its callee's return and must take the plain successor
    # edge instead of descending into the callee again.
    queue = deque([(source_id, False, (source_id,))])
    seen = {(source_id, False)}
    while queue:
        current, post_return, path = queue.popleft()
        if current == sink_id:
            if _path_satisfies(graph, path, field_name, require_open,
                               rule_table, category, node_envs):
                return path
            continue
        for nxt, next_post in _supergraph_successors(
                graph, current, post_return):
            state = (nxt, next_post)
            if state in seen:
                continue
            seen.add(state)
            queue.append((nxt, next_post, path + (nxt,)))
    return None


def _path_satisfies(graph, path, field_name, require_open,
                    rule_table, category, node_envs) -> bool:
    if rule_table is None or not require_open:
        return True
    sink_env = node_envs.get(path[-1], {})
    for ps in sink_env.get(field_name, frozenset()):
        if not ps.tainted or ps.unknown:
            continue
        effective = [s for s in ps.sanitizers
                     if rule_table.covers(s, category)]
        if not effective:
            return True
    return False


def explain_steps(graph: Graph, path, field_name: str,
                  rule_table: RuleTable, node_envs: dict):
    """Human-readable explanation of where sanitization takes effect.

    Field names are tracked through call/return edges: descending into a
    callee maps caller actuals to bound formals; returning maps formals back
    to actuals and adds the call's return field.  A call node appears twice
    in a supergraph path (before descent and after return); the first visit
    performs the descent mapping, the second the return mapping.
    """
    steps = []
    current_fields = {field_name}
    in_callee = None

    for nid in path:
        node = graph.nodes[nid]

        if node.kind == "call":
            if in_callee is None:
                # First visit: descend into the callee.
                next_fields = set()
                for formal, actual in node.bindings:
                    if actual in current_fields:
                        next_fields.add(formal)
                if node.ret_field in current_fields:
                    # Return field: every bound formal may contribute.
                    next_fields.update(f for f, _ in node.bindings)
                current_fields = next_fields or current_fields
                in_callee = node.callee
            else:
                # Post-return visit: map formals back to the caller side.
                next_fields = set()
                for formal, actual in node.bindings:
                    if formal in current_fields:
                        next_fields.add(actual)
                if node.ret_field is not None:
                    next_fields.add(node.ret_field)
                current_fields = next_fields or current_fields
                in_callee = None

        merged_states = frozenset()
        env = node_envs.get(nid, {})
        for f in current_fields:
            merged_states = merged_states | frozenset(env.get(f, frozenset()))

        detail = {
            "node": nid,
            "kind": node.kind,
            "function": node.function,
            "observed_fields": sorted(current_fields),
            "field_states": [
                _state_explanation(ps, rule_table, node.category)
                for ps in sorted(merged_states, key=_state_sort_key)
            ],
        }
        if node.kind == "stmt" and node.sanitizers:
            detail["sanitizers_applied_here"] = list(node.sanitizers)
        if node.kind == "source":
            detail["source_label"] = node.source_label
        if node.kind == "sink":
            detail["category"] = node.category
        steps.append(detail)
    return steps


def _state_explanation(ps: PathState, rule_table: RuleTable,
                       sink_category):
    effective = []
    if sink_category is not None:
        effective = [
            s for s in ps.sanitizers
            if rule_table.covers(s, sink_category)
        ]
    return {
        "tainted": ps.tainted,
        "unknown": ps.unknown,
        "sanitizers_on_path": list(ps.sanitizers),
        "effective_for_this_sink": effective,
        "origins": list(ps.origins),
    }


# ---------------------------------------------------------------------------
# Version comparison and classification
# ---------------------------------------------------------------------------

def classify(graph: Graph, old_rules: RuleTable, new_rules: RuleTable,
             node_envs: dict):
    added_pairs, removed_pairs = diff_rule_tables(old_rules, new_rules)
    items = derive_watch_items(graph)
    results = []

    for item in items:
        sink = graph.nodes[item["sink"]]
        field_name = item["field"]
        states = node_envs.get(sink.id, {}).get(field_name, frozenset())
        old_eval = evaluate_sink(states, old_rules, sink.category)
        new_eval = evaluate_sink(states, new_rules, sink.category)
        old_v, new_v = old_eval["verdict"], new_eval["verdict"]

        bucket, minimal_evidence = _bucket_and_evidence(
            old_v, new_v, sink.category, added_pairs, removed_pairs)

        witness = find_witness(
            graph, sink.id, field_name,
            require_open=(new_v == BLOCKED),
            rule_table=new_rules, node_envs=node_envs)
        explanation = None
        if witness is not None:
            explanation = explain_steps(
                graph, witness, field_name, new_rules, node_envs)

        results.append({
            "name": item.get("name", f"{sink.id}:{field_name}"),
            "sink": sink.id,
            "field": field_name,
            "category": sink.category,
            "old_verdict": old_v,
            "new_verdict": new_v,
            "classification": bucket,
            "alternatives": new_eval["alternatives"],
            "minimal_rule_evidence": minimal_evidence,
            "witness_path": list(witness) if witness else None,
            "step_explanation": explanation,
            "incomplete_reasons": _incompleteness_reasons(graph),
        })

    order = {
        NEWLY_ALLOWED: 0, NEWLY_BLOCKED: 1, UNCHANGED: 2,
        UNDETERMINED_BUCKET: 3, UNREACHABLE_BUCKET: 4,
    }
    results.sort(key=lambda r: (order[r["classification"]], r["name"]))
    return {
        "added_rule_pairs": [list(p) for p in added_pairs],
        "removed_rule_pairs": [list(p) for p in removed_pairs],
        "items": results,
    }


def _bucket_and_evidence(old_v, new_v, category, added_pairs, removed_pairs):
    if old_v == new_v:
        if old_v == UNDETERMINED:
            return UNDETERMINED_BUCKET, []
        if old_v == UNREACHABLE:
            return UNREACHABLE_BUCKET, []
        return UNCHANGED, []

    if old_v == BLOCKED and new_v == ALLOWED:
        evidence = [
            {"change": "added", "sanitizer": san, "category": cat}
            for san, cat in added_pairs if cat == category
        ]
        return NEWLY_ALLOWED, evidence
    if old_v == ALLOWED and new_v == BLOCKED:
        evidence = [
            {"change": "removed", "sanitizer": san, "category": cat}
            for san, cat in removed_pairs if cat == category
        ]
        return NEWLY_BLOCKED, evidence
    # Any transition touching undetermined stays in the undetermined bucket.
    return UNDETERMINED_BUCKET, []


def _incompleteness_reasons(graph: Graph):
    reasons = list(graph.assumptions)
    for nid in sorted(graph.incompleteness):
        node = graph.nodes[nid]
        if node.kind == "unmodeled":
            reasons.append(
                f"node {nid} is unmodeled ({node.reason or 'no reason given'})")
        elif node.kind == "call":
            reasons.append(
                f"call {nid} targets unresolved callee {node.callee!r}")
    return reasons
