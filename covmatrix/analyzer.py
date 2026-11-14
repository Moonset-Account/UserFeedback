"""Coverage matrix analysis.

Pipeline:
1. Static reachability: cycle-safe BFS over the module call graph from
   each entry's module; a checkpoint is reachable from an entry when its
   module is reachable. Entries may share modules.
2. Trace attribution: a trace checkpoint hit counts as coverage for an
   entry only when the trace's build matches the target build AND the
   trace's entry matches the entry AND the checkpoint is a patched
   checkpoint statically reachable from that entry. One test hitting
   several checkpoints produces one coverage record per checkpoint.
3. Entry status:
   - observed_covered      at least one reachable checkpoint has a valid
                           attributed observation
   - unattributable        the entry has trace records but none could be
                           attributed (build mismatch, unknown checkpoint,
                           checkpoint not reachable from this entry)
   - statically_reachable  patched checkpoints are reachable but no trace
                           records exist for the entry at all
   - no_reachable_checkpoint  nothing patched is reachable (informational)
4. For every uncovered entry (statically_reachable / unattributable) the
   nearest observed checkpoint is computed by BFS distance from the
   entry's module, so maintainers can pick the next regression case
   close to existing coverage.
"""

from collections import deque
from typing import Dict, List, Optional, Tuple

from .model import CallGraph, Checkpoint, Entry, Trace

OBSERVED_COVERED = "observed_covered"
STATICALLY_REACHABLE = "statically_reachable"
UNATTRIBUTABLE = "unattributable"
NO_REACHABLE_CHECKPOINT = "no_reachable_checkpoint"

#: Statuses that mean the entry still needs a regression case.
UNCOVERED_STATUSES = (STATICALLY_REACHABLE, UNATTRIBUTABLE)

# Trace record rejection reasons.
REASON_BUILD_MISMATCH = "build_mismatch"
REASON_UNKNOWN_ENTRY = "unknown_entry"
REASON_UNKNOWN_CHECKPOINT = "unknown_checkpoint"
REASON_NOT_REACHABLE = "checkpoint_not_reachable"


def reachable_modules(graph: CallGraph, start: str) -> set:
    """All modules reachable from ``start``; cycle-safe via the seen set."""
    seen = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in graph.callees(node):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def bfs_distances(graph: CallGraph, start: str) -> Tuple[Dict[str, int], Dict[str, Optional[str]]]:
    """Shortest-hop distance and parent map from ``start`` (cycle-safe)."""
    dist = {start: 0}
    parent: Dict[str, Optional[str]] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for nxt in graph.callees(node):
            if nxt not in dist:
                dist[nxt] = dist[node] + 1
                parent[nxt] = node
                queue.append(nxt)
    return dist, parent


def _path_to(parent: Dict[str, Optional[str]], module: str) -> List[str]:
    path = []
    node: Optional[str] = module
    while node is not None:
        path.append(node)
        node = parent[node]
    path.reverse()
    return path


def analyze(
    entries: List[Entry],
    graph: CallGraph,
    checkpoints: List[Checkpoint],
    traces: List[Trace],
    build: str,
) -> dict:
    """Run the full coverage analysis for ``build`` and return a result dict."""
    cp_by_id = {c.id: c for c in checkpoints}
    entry_by_id = {e.id: e for e in entries}

    # 1. Static reachability per entry.
    reachable: Dict[str, List[str]] = {}
    for entry in entries:
        modules = reachable_modules(graph, entry.module)
        reachable[entry.id] = sorted(c.id for c in checkpoints if c.module in modules)

    # 2. Attribute every trace checkpoint hit separately.
    coverage: Dict[str, List[dict]] = {e.id: [] for e in entries}
    invalid: Dict[str, List[dict]] = {e.id: [] for e in entries}
    global_unattributable: List[dict] = []
    for trace in traces:
        for cp_id in trace.checkpoints:  # one test, many checkpoints: record each
            base = {"test_id": trace.test_id, "checkpoint_id": cp_id, "entry_id": trace.entry_id}
            if trace.build != build:
                rec = dict(base, reason=REASON_BUILD_MISMATCH,
                           detail=f"trace build {trace.build!r} != target build {build!r}")
                if trace.entry_id in entry_by_id:
                    invalid[trace.entry_id].append(rec)
                else:
                    global_unattributable.append(rec)
                continue
            entry = entry_by_id.get(trace.entry_id)
            if entry is None:
                global_unattributable.append(dict(
                    base, reason=REASON_UNKNOWN_ENTRY,
                    detail=f"no entry point with id {trace.entry_id!r}"))
                continue
            if cp_id not in cp_by_id:
                invalid[entry.id].append(dict(
                    base, reason=REASON_UNKNOWN_CHECKPOINT,
                    detail=f"checkpoint {cp_id!r} is not part of the patch"))
                continue
            if cp_id not in reachable[entry.id]:
                invalid[entry.id].append(dict(
                    base, reason=REASON_NOT_REACHABLE,
                    detail=f"checkpoint {cp_id!r} (module {cp_by_id[cp_id].module!r}) is not "
                           f"statically reachable from entry {entry.id!r} (module {entry.module!r})"))
                continue
            coverage[entry.id].append({"test_id": trace.test_id, "checkpoint_id": cp_id})

    # De-duplicate and sort coverage records for deterministic output.
    for entry_id, records in coverage.items():
        unique = {(r["test_id"], r["checkpoint_id"]) for r in records}
        coverage[entry_id] = [
            {"test_id": t, "checkpoint_id": c} for t, c in sorted(unique)
        ]

    observed_cps = sorted({r["checkpoint_id"] for recs in coverage.values() for r in recs})

    # 3. Classify each entry; 4. nearest observed checkpoint for uncovered ones.
    entry_results = []
    for entry in entries:
        covered_cps = sorted({r["checkpoint_id"] for r in coverage[entry.id]})
        if covered_cps:
            status = OBSERVED_COVERED
        elif invalid[entry.id]:
            status = UNATTRIBUTABLE
        elif reachable[entry.id]:
            status = STATICALLY_REACHABLE
        else:
            status = NO_REACHABLE_CHECKPOINT

        nearest = None
        if status in UNCOVERED_STATUSES and observed_cps:
            dist, parent = bfs_distances(graph, entry.module)
            for cp_id in observed_cps:
                cp_module = cp_by_id[cp_id].module
                d = dist.get(cp_module)
                if d is None:
                    continue
                if nearest is None or (d, cp_id) < (nearest["distance"], nearest["id"]):
                    nearest = {
                        "id": cp_id,
                        "module": cp_module,
                        "distance": d,
                        "path": _path_to(parent, cp_module),
                    }

        entry_results.append({
            "id": entry.id,
            "name": entry.name,
            "module": entry.module,
            "status": status,
            "reachable_checkpoints": reachable[entry.id],
            "covered_checkpoints": covered_cps,
            "missing_checkpoints": [c for c in reachable[entry.id] if c not in covered_cps],
            "coverage_records": coverage[entry.id],
            "invalid_traces": sorted(
                invalid[entry.id],
                key=lambda r: (r["test_id"], r["checkpoint_id"], r["reason"]),
            ),
            "nearest_observed_checkpoint": nearest,
        })

    uncovered = [e["id"] for e in entry_results if e["status"] in UNCOVERED_STATUSES]
    summary = {
        "total_entries": len(entry_results),
        OBSERVED_COVERED: sum(1 for e in entry_results if e["status"] == OBSERVED_COVERED),
        STATICALLY_REACHABLE: sum(1 for e in entry_results if e["status"] == STATICALLY_REACHABLE),
        UNATTRIBUTABLE: sum(1 for e in entry_results if e["status"] == UNATTRIBUTABLE),
        NO_REACHABLE_CHECKPOINT: sum(1 for e in entry_results if e["status"] == NO_REACHABLE_CHECKPOINT),
        "uncovered_entries": len(uncovered),
        "observed_checkpoints": len(observed_cps),
        "total_checkpoints": len(checkpoints),
        "unattributable_trace_records": len(global_unattributable)
        + sum(len(e["invalid_traces"]) for e in entry_results),
    }
    return {
        "build": build,
        "entries": entry_results,
        "uncovered_entries": uncovered,
        "observed_checkpoints": observed_cps,
        "unattributable_traces": sorted(
            global_unattributable,
            key=lambda r: (r["test_id"], r["checkpoint_id"], r["reason"]),
        ),
        "summary": summary,
    }
