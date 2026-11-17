"""Per-entry fixpoint capability analysis.

For every exported entry the analysis computes, by monotone fixpoint
iteration:

* the set of reachable functions (direct calls plus signature-compatible
  indirect call targets),
* the host (imported) functions reachable from the entry and the
  capabilities they map to in the registry,
* an evidence path from the entry root to each reachable host function,
* table elements that were *excluded* for this entry together with the
  reason (``type_mismatch`` or ``unreachable_update``),
* ``unknown`` markers when a consulted table is externally writable or a
  reached import has no registry entry -- such targets must never be
  treated as "no capability".

Element updates are may-writes: every update whose program point lies in
a reachable function contributes its function as a possible target of
the slot.  Because reachability and the active update set only grow, the
iteration is monotone over a finite domain and always converges, even
with call cycles and repeated updates to the same slot.
"""
from __future__ import annotations

import json
from collections import deque

from .model import Summary, Entry, program_point_function

MAX_ITERATIONS = 100000  # safety bound; monotone iteration converges far earlier


def type_compatible(summary: Summary, actual_type_id: str, expected_type_id: str) -> bool:
    """Signature compatibility is structural equality of params/results."""
    return summary.types[actual_type_id] == summary.types[expected_type_id]


def _index_summary(summary: Summary):
    direct_by_caller = {}
    for dc in summary.direct_calls:
        direct_by_caller.setdefault(dc.caller, []).append(dc)
    indirect_by_caller = {}
    for ic in summary.indirect_calls:
        indirect_by_caller.setdefault(ic.caller, []).append(ic)
    updates_by_table = {}
    for upd in summary.updates:
        updates_by_table.setdefault(upd.table, []).append(upd)
    return direct_by_caller, indirect_by_caller, updates_by_table


def analyze_entry(summary: Summary, entry: Entry) -> dict:
    """Analyze a single entry and return a JSON-serializable report."""
    direct_by_caller, indirect_by_caller, updates_by_table = _index_summary(summary)

    reachable = {entry.function}
    edges = []        # (src, dst, label dict) in discovery order
    seen_edge_keys = set()
    iterations = 0

    while True:
        iterations += 1
        if iterations > MAX_ITERATIONS:  # pragma: no cover - defensive
            raise RuntimeError("analysis did not converge")
        changed = False
        for fn in sorted(reachable):
            for dc in direct_by_caller.get(fn, ()):
                label = {"kind": "direct", "site": dc.site}
                key = (fn, dc.callee, json.dumps(label, sort_keys=True))
                if key not in seen_edge_keys:
                    seen_edge_keys.add(key)
                    edges.append((fn, dc.callee, label))
                if dc.callee not in reachable:
                    reachable.add(dc.callee)
                    changed = True
            for ic in indirect_by_caller.get(fn, ()):
                table = summary.tables[ic.table]
                candidates = []  # (index, function, origin, update_at)
                for idx, el in enumerate(table.initial):
                    if el is not None:
                        candidates.append((idx, el, "initial", None))
                for upd in updates_by_table.get(ic.table, ()):
                    if program_point_function(upd.at) in reachable:
                        candidates.append((upd.index, upd.function, "update", upd.at))
                for idx, target, origin, update_at in candidates:
                    func = summary.functions[target]
                    if not type_compatible(summary, func.type_id, ic.expected_type):
                        continue
                    label = {"kind": "indirect", "site": ic.site, "table": ic.table,
                             "index": idx, "origin": origin}
                    if update_at is not None:
                        label["update_at"] = update_at
                    key = (fn, target, json.dumps(label, sort_keys=True))
                    if key not in seen_edge_keys:
                        seen_edge_keys.add(key)
                        edges.append((fn, target, label))
                    if target not in reachable:
                        reachable.add(target)
                        changed = True
        if not changed:
            break

    # --- host functions, capabilities, unknown markers ----------------------
    hosts = sorted(f for f in reachable if summary.functions[f].kind == "import")
    capabilities = set()
    unknown_reasons = []
    for host in hosts:
        caps = summary.capabilities.get(host)
        if caps is None:
            unknown_reasons.append({
                "function": host,
                "detail": "reached import has no capability registry entry",
            })
        else:
            capabilities.update(caps)
    for ic in summary.indirect_calls:
        if ic.caller in reachable and summary.tables[ic.table].externally_writable:
            unknown_reasons.append({
                "site": ic.site,
                "table": ic.table,
                "detail": "table is externally writable; indirect targets are unknown",
            })
    unknown_reasons.sort(key=lambda r: json.dumps(r, sort_keys=True))

    # --- evidence paths (BFS over recorded edges) ----------------------------
    adjacency = {}
    for src, dst, label in edges:
        adjacency.setdefault(src, []).append((dst, label))
    for src in adjacency:
        adjacency[src].sort(key=lambda item: (item[0], json.dumps(item[1], sort_keys=True)))
    prev = {entry.function: None}
    queue = deque([entry.function])
    while queue:
        node = queue.popleft()
        for dst, label in adjacency.get(node, ()):
            if dst not in prev:
                prev[dst] = (node, label)
                queue.append(dst)
    evidence = {}
    for host in hosts:
        path = []
        node = host
        while node is not None:
            step = {"function": node}
            if prev[node] is not None:
                step["via"] = prev[node][1]
            path.append(step)
            node = prev[node][0] if prev[node] is not None else None
        path.reverse()
        evidence[host] = path

    # --- excluded table elements with reasons --------------------------------
    excluded = []
    reached_indirect_sites = [ic for ic in summary.indirect_calls
                              if ic.caller in reachable]
    for upd in summary.updates:
        if program_point_function(upd.at) not in reachable:
            excluded.append({
                "table": upd.table,
                "index": upd.index,
                "function": upd.function,
                "reason": "unreachable_update",
                "update_at": upd.at,
                "detail": "no path from this entry reaches the function "
                          "containing the write",
            })
    for ic in reached_indirect_sites:
        table = summary.tables[ic.table]
        candidates = []
        for idx, el in enumerate(table.initial):
            if el is not None:
                candidates.append((idx, el, "initial", None))
        for upd in updates_by_table.get(ic.table, ()):
            if program_point_function(upd.at) in reachable:
                candidates.append((upd.index, upd.function, "update", upd.at))
        for idx, target, origin, update_at in candidates:
            func = summary.functions[target]
            if type_compatible(summary, func.type_id, ic.expected_type):
                continue
            record = {
                "table": ic.table,
                "index": idx,
                "function": target,
                "reason": "type_mismatch",
                "site": ic.site,
                "origin": origin,
                "expected_type": ic.expected_type,
                "actual_type": func.type_id,
                "detail": "table element signature is not compatible with the "
                          "call_indirect expected type",
            }
            if update_at is not None:
                record["update_at"] = update_at
            excluded.append(record)
    excluded.sort(key=lambda r: json.dumps(r, sort_keys=True))

    return {
        "entry": entry.name,
        "root": entry.function,
        "reachable_functions": sorted(reachable),
        "reachable_host_functions": hosts,
        "capabilities": sorted(capabilities),
        "allowed_capabilities": sorted(entry.allowed_capabilities),
        "violations": sorted(capabilities - entry.allowed_capabilities),
        "unknown": bool(unknown_reasons),
        "unknown_reasons": unknown_reasons,
        "evidence": evidence,
        "excluded_elements": excluded,
        "converged": True,
        "iterations": iterations,
    }


def analyze(summary: Summary) -> dict:
    """Analyze every entry; per-entry conclusions are kept separate."""
    return {"entries": [analyze_entry(summary, entry) for entry in summary.entries]}
