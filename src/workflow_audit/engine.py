"""Content-provenance propagation engine.

The analyzer walks every feasible path of a JSON job graph and tracks, for
each artifact and shared-cache key, where its content could have come from.

Hard rules implemented here:

* Artifacts are identified by ``(name, commit)``.  Same name, different
  production commit => different artifacts, never merged by name.
* A shared cache key may have several possible writers.  All of them are
  retained as possible origins on every read.
* A pinned download (fixed production job + commit) keeps a source
  constraint.  An unpinned download is born without one and records the
  first position where the constraint is missing.
* Manual approval only gates whether a job runs.  It never upgrades the
  trust level of any content origin.
* When a dynamic job definition is missing, the affected paths are kept and
  reported as unprovable instead of being guessed into trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ArtifactKey = Tuple[str, str]

# Ordering used to decide whether replacing a constraint is a degradation.
_CONSTRAINT_RANK = {
    "pinned_commit": 3,
    "named_producer_only": 2,
    "unpinned": 1,
    "none": 0,
}


class AnalysisError(RuntimeError):
    """Raised on inconsistent graph state during analysis."""


@dataclass
class Origin:
    """One possible content origin attached to an artifact or cache value."""

    source: str                      # production | external_contribution | unknown | mixed
    kind: str                        # produced | download_pinned | download_unpinned |
                                     # cache_write | cache_read | merged | dynamic_unknown
    constraint: str                  # pinned_commit | named_producer_only | unpinned | none
    constraint_detail: Dict[str, Any] = field(default_factory=dict)
    writer_job: Optional[str] = None
    cache_key: Optional[str] = None
    introduced_at: Optional[str] = None
    losses: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "constraint": self.constraint,
            "constraint_detail": self.constraint_detail,
            "writer_job": self.writer_job,
            "cache_key": self.cache_key,
            "introduced_at": self.introduced_at,
            "source_constraint_losses": list(self.losses),
        }

    def signature(self) -> Tuple[Any, ...]:
        return (
            self.source,
            self.kind,
            self.constraint,
            self.writer_job,
            self.cache_key,
            self.introduced_at,
        )


@dataclass
class PathState:
    """Propagated state along one feasible path."""

    path_id: str
    trigger_id: str
    trigger_source: str
    branches: Tuple[str, ...]
    artifacts: Dict[ArtifactKey, Origin] = field(default_factory=dict)
    caches: Dict[str, List[Origin]] = field(default_factory=dict)
    approvals: List[str] = field(default_factory=list)
    loss_log: List[Dict[str, Any]] = field(default_factory=list)
    unprovable: List[Dict[str, Any]] = field(default_factory=list)
    job_order: List[str] = field(default_factory=list)

    def log_loss(self, loss: Dict[str, Any]) -> None:
        loss = dict(loss)
        loss.setdefault("path_id", self.path_id)
        self.loss_log.append(loss)


def analyze_graph(graph: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze a validated graph dict and return a JSON-serialisable report."""
    jobs = {job["id"]: job for job in graph["jobs"]}
    triggers = {trig["id"]: trig for trig in graph["triggers"]}
    dynamic = {d["ref"]: d for d in graph.get("dynamic_missing", []) or []}

    producer_index = _build_producer_index(graph, jobs, triggers)
    paths = enumerate_paths(graph, jobs, triggers, dynamic)

    # Phase 0: a shared cache is reachable across trigger lanes, so its
    # possible writers must be collected from *every* job in the graph,
    # including jobs that never appear on a release-reaching path.
    cache_writes: Dict[str, List[Tuple[str, Origin]]] = {}
    _sweep_cache_writers(graph, jobs, triggers, cache_writes)

    # Pass 1: execute every path so per-path cache states match the sweep.
    for path in paths:
        state = PathState(
            path_id=path["path_id"],
            trigger_id=path["trigger_id"],
            trigger_source=triggers[path["trigger_id"]]["source"],
            branches=tuple(path["branches"]),
        )
        _execute_path(state, path["job_order"], jobs, dynamic, producer_index,
                      cache_writes, resolve_reads=False)

    # Pass 2: re-execute with the global cache-writer view available.
    findings: List[Dict[str, Any]] = []
    path_reports: List[Dict[str, Any]] = []
    for path in paths:
        state = PathState(
            path_id=path["path_id"],
            trigger_id=path["trigger_id"],
            trigger_source=triggers[path["trigger_id"]]["source"],
            branches=tuple(path["branches"]),
        )
        _execute_path(state, path["job_order"], jobs, dynamic, producer_index,
                      cache_writes, resolve_reads=True)
        path_reports.append(_path_report(state))
        for job_id in path["job_order"]:
            job = jobs.get(job_id)
            if job is not None and job.get("is_release"):
                findings.extend(_evaluate_release(state, job))

    return {
        "schema_version": 1,
        "graph_id": graph.get("id"),
        "summary": _summary(findings, paths, dynamic),
        "artifact_producer_index": [
            {
                "artifact": {"name": name, "commit": commit},
                "producers": producers,
            }
            for (name, commit), producers in sorted(producer_index.items())
        ],
        "cache_possible_writers": _cache_writer_report(cache_writes),
        "paths": path_reports,
        "findings": findings,
        "dynamic_missing": graph.get("dynamic_missing", []) or [],
    }


def analyze_graph_dict(graph: Dict[str, Any]) -> Dict[str, Any]:
    """Alias used by callers that build the graph in memory."""
    return analyze_graph(graph)


# --------------------------------------------------------------------------- #
# Path enumeration
# --------------------------------------------------------------------------- #

def _need_edges(job: Dict[str, Any]) -> List[Tuple[str, Optional[str]]]:
    edges: List[Tuple[str, Optional[str]]] = []
    for need in job.get("needs", []):
        if isinstance(need, str):
            edges.append((need, None))
        else:
            edges.append((need["job"], need.get("if")))
    return edges


def enumerate_paths(
    graph: Dict[str, Any],
    jobs: Dict[str, Dict[str, Any]],
    triggers: Dict[str, Dict[str, Any]],
    dynamic: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Enumerate feasible trigger-to-release paths over condition branches."""
    conditions = sorted(
        {cond for job in jobs.values() for cond in job.get("conditions", [])}
        | {
            guard
            for job in jobs.values()
            for _, guard in _need_edges(job)
            if guard
        }
    )

    paths: List[Dict[str, Any]] = []
    seen: set = set()

    for trigger_id, trigger in sorted(triggers.items()):
        # Every job declaring this trigger is an entry point; traversal then
        # follows `needs` edges to pull in prerequisite jobs.
        roots = sorted(
            jid for jid, job in jobs.items() if trigger_id in job.get("triggers", [])
        )
        if not roots:
            continue

        for assignment in product((False, True), repeat=len(conditions)):
            active = {c for c, on in zip(conditions, assignment) if on}
            order = _topo_under_assignment(roots, active, jobs, dynamic)
            if order is None:
                continue
            reachable = set(order)
            release_jobs = [
                jid for jid in order
                if jid in jobs and jobs[jid].get("is_release")
            ]
            if not release_jobs:
                continue
            key = (trigger_id, frozenset(reachable), frozenset(sorted(active)))
            if key in seen:
                continue
            seen.add(key)
            paths.append({
                "path_id": f"P{len(paths) + 1:02d}",
                "trigger_id": trigger_id,
                "trigger_source": trigger["source"],
                "branches": tuple(sorted(active)),
                "job_order": order,
            })
    return paths


def _topo_under_assignment(
    roots: Sequence[str],
    active: set,
    jobs: Dict[str, Dict[str, Any]],
    dynamic: Dict[str, Dict[str, Any]],
) -> Optional[List[str]]:
    """Deterministic topological order of nodes reachable under a branch set."""
    visited: set = set()
    order: List[str] = []
    stack: List[str] = list(roots)
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        order.append(node)
        if node in dynamic:
            continue
        job = jobs.get(node)
        if job is None:
            continue
        if not set(job.get("conditions", [])) <= active:
            continue
        for target, guard in _need_edges(job):
            if guard and guard not in active:
                continue
            if target not in visited:
                stack.append(target)

    # Kahn-style deterministic ordering of the reachable subgraph.
    eligible = [n for n in order if n in dynamic or (
        n in jobs and set(jobs[n].get("conditions", [])) <= active
    )]
    eligible_set = set(eligible)
    indegree: Dict[str, int] = {n: 0 for n in eligible}
    adjacency: Dict[str, List[str]] = {n: [] for n in eligible}
    for node in eligible:
        job = jobs.get(node)
        if job is None:
            continue
        for need, guard in _need_edges(job):
            if guard and guard not in active:
                continue
            if need in eligible_set:
                adjacency[need].append(node)
                indegree[node] += 1

    ready = sorted(n for n, deg in indegree.items() if deg == 0)
    result: List[str] = []
    while ready:
        node = ready.pop(0)
        result.append(node)
        for nxt in sorted(adjacency[node]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    if len(result) != len(eligible):
        return None  # cycle or unsatisfiable conditional dependency
    return result


# --------------------------------------------------------------------------- #
# Static producer index
# --------------------------------------------------------------------------- #

def _sweep_cache_writers(
    graph: Dict[str, Any],
    jobs: Dict[str, Dict[str, Any]],
    triggers: Dict[str, Dict[str, Any]],
    cache_writes: Dict[str, List[Tuple[str, Origin]]],
) -> None:
    """Record every cache-writing job as a possible writer of its keys.

    Shared caches can be written by jobs that do not lie on any
    release-reaching path; those writers are still pollution candidates and
    must therefore be collected independently of path enumeration.
    """
    for job in graph["jobs"]:
        sources = sorted({
            triggers[tid]["source"]
            for tid in job.get("triggers", [])
            if tid in triggers
        })
        source = sources[0] if len(sources) == 1 else (
            "mixed" if sources else "unknown")
        for action in job.get("actions", []):
            if action["type"] != "cache_write":
                continue
            cache_key = action["cache_key"]
            location = f"{job['id']}:cache_write:{cache_key}"
            refs = action.get("from", [])
            if refs:
                # Best-effort static resolution of referenced artifacts.
                origins = []
                for ref in refs:
                    producers = _static_artifact_origins(ref, jobs, triggers)
                    origins.extend(producers)
                if not origins:
                    origins = [_static_unknown(job["id"], source, cache_key, location)]
            else:
                origins = [_static_unknown(job["id"], source, cache_key, location)]

            for origin in origins:
                degraded = Origin(
                    source=origin.source,
                    kind="cache_write",
                    constraint="none",
                    constraint_detail={
                        **origin.constraint_detail,
                        "swept_from_job": job["id"],
                    },
                    writer_job=job["id"],
                    cache_key=cache_key,
                    introduced_at=location,
                    losses=[{
                        "location": location,
                        "subject": {"cache_key": cache_key},
                        "from_constraint": origin.constraint,
                        "to_constraint": "none",
                        "note": "content enters a shared cache; the cache "
                                "enforces no source constraint, so multiple "
                                "writers become possible",
                    }],
                )
                cache_writes.setdefault(cache_key, []).append(
                    (f"sweep:{job['id']}", degraded))


def _static_artifact_origins(
    ref: Dict[str, Any],
    jobs: Dict[str, Dict[str, Any]],
    triggers: Dict[str, Dict[str, Any]],
) -> List[Origin]:
    if "cache_key" in ref:
        return []
    origins: List[Origin] = []
    for job in jobs.values():
        sources = sorted({
            triggers[tid]["source"]
            for tid in job.get("triggers", [])
            if tid in triggers
        })
        source = sources[0] if len(sources) == 1 else (
            "mixed" if sources else "unknown")
        for action in job.get("actions", []):
            if action["type"] != "produce":
                continue
            art = action["artifact"]
            if art["name"] != ref["name"]:
                continue
            if ref.get("commit") and art.get("commit") != ref["commit"]:
                continue
            origins.append(Origin(
                source=source,
                kind="produced",
                constraint="named_producer_only",
                constraint_detail={
                    "producer_job": job["id"],
                    "commit": art.get("commit"),
                },
                writer_job=job["id"],
                introduced_at=f"{job['id']}:produce:{art['name']}",
            ))
    return origins


def _static_unknown(
    job_id: str, source: str, cache_key: str, location: str
) -> Origin:
    return Origin(
        source=source,
        kind="cache_write",
        constraint="none",
        constraint_detail={"reason": "cache write with no traceable input"},
        writer_job=job_id,
        cache_key=cache_key,
        introduced_at=location,
    )


def _build_producer_index(
    graph: Dict[str, Any],
    jobs: Dict[str, Dict[str, Any]],
    triggers: Dict[str, Dict[str, Any]],
) -> Dict[ArtifactKey, List[Dict[str, Any]]]:
    index: Dict[ArtifactKey, List[Dict[str, Any]]] = {}
    for job in graph["jobs"]:
        sources = sorted({
            triggers[tid]["source"]
            for tid in job.get("triggers", [])
            if tid in triggers
        })
        source = sources[0] if len(sources) == 1 else ("mixed" if sources else "unknown")
        for action in job.get("actions", []):
            if action["type"] != "produce":
                continue
            ref = action["artifact"]
            key = (ref["name"], ref.get("commit", ""))
            index.setdefault(key, []).append({
                "producer_job": job["id"],
                "producer_commit": ref.get("commit"),
                "trigger_sources": sources,
                "source": source,
            })
    return index


# --------------------------------------------------------------------------- #
# State propagation
# --------------------------------------------------------------------------- #

def _execute_path(
    state: PathState,
    order: Sequence[str],
    jobs: Dict[str, Dict[str, Any]],
    dynamic: Dict[str, Dict[str, Any]],
    producer_index: Dict[ArtifactKey, List[Dict[str, Any]]],
    cache_writes: Dict[str, List[Tuple[str, Origin]]],
    resolve_reads: bool,
) -> None:
    for node in order:
        state.job_order.append(node)
        if node in dynamic:
            state.unprovable.append({
                "ref": node,
                "reason": dynamic[node].get("reason", "dynamic job definition missing"),
                "location": node,
            })
            continue
        job = jobs[node]
        if job.get("requires_approval"):
            # Approvals gate execution only; they never mutate origins.
            state.approvals.append(node)
        for action in job.get("actions", []):
            _apply_action(state, node, action, producer_index,
                          cache_writes, resolve_reads)


def _apply_action(
    state: PathState,
    job_id: str,
    action: Dict[str, Any],
    producer_index: Dict[ArtifactKey, List[Dict[str, Any]]],
    cache_writes: Dict[str, List[Tuple[str, Origin]]],
    resolve_reads: bool,
) -> None:
    atype = action["type"]
    if atype == "produce":
        _apply_produce(state, job_id, action)
    elif atype == "download":
        _apply_download(state, job_id, action, producer_index)
    elif atype == "cache_write":
        _apply_cache_write(state, job_id, action, cache_writes)
    elif atype == "cache_read":
        if resolve_reads:
            _apply_cache_read(state, job_id, action, cache_writes)


def _apply_produce(
    state: PathState, job_id: str, action: Dict[str, Any]
) -> None:
    ref = action["artifact"]
    key = (ref["name"], ref.get("commit", ""))
    location = f"{job_id}:produce:{ref['name']}"
    input_origins = _resolve_refs(state, action.get("from", []), location)

    if input_origins:
        origin = _merge_origins(input_origins, state, location,
                                kind="merged", writer_job=job_id)
    else:
        origin = Origin(
            source=state.trigger_source,
            kind="produced",
            constraint="named_producer_only",
            constraint_detail={
                "producer_job": job_id,
                "commit": ref.get("commit"),
            },
            writer_job=job_id,
            introduced_at=location,
        )
    state.artifacts[key] = origin


def _apply_download(
    state: PathState,
    job_id: str,
    action: Dict[str, Any],
    producer_index: Dict[ArtifactKey, List[Dict[str, Any]]],
) -> None:
    ref = action["artifact"]
    key = (ref["name"], ref.get("commit", ""))
    location = f"{job_id}:download:{ref['name']}"

    if action.get("pinned"):
        producer_job = action["producer_job"]
        producer_commit = action["producer_commit"]
        producers = producer_index.get((ref["name"], producer_commit), [])
        match = next((p for p in producers if p["producer_job"] == producer_job), None)
        source = match["source"] if match else "unknown"
        origin = Origin(
            source=source,
            kind="download_pinned",
            constraint="pinned_commit",
            constraint_detail={
                "producer_job": producer_job,
                "producer_commit": producer_commit,
            },
            writer_job=producer_job,
            introduced_at=location,
        )
        state.artifacts[key] = origin
        return

    # Unpinned download: no source constraint is established at all.
    origin = Origin(
        source="unknown",
        kind="download_unpinned",
        constraint="unpinned",
        constraint_detail={"reason": "download does not pin producer job/commit"},
        writer_job=None,
        introduced_at=location,
    )
    loss = {
        "location": location,
        "subject": {"artifact": {"name": ref["name"], "commit": ref.get("commit")}},
        "from_constraint": None,
        "to_constraint": "unpinned",
        "note": "download source is not pinned to a production job/commit; "
                "no source constraint is established",
    }
    origin.losses.append(loss)
    state.log_loss(loss)
    state.artifacts[key] = origin


def _apply_cache_write(
    state: PathState,
    job_id: str,
    action: Dict[str, Any],
    cache_writes: Dict[str, List[Tuple[str, Origin]]],
) -> None:
    cache_key = action["cache_key"]
    location = f"{job_id}:cache_write:{cache_key}"
    refs = action.get("from", [])
    if refs:
        origins = _resolve_refs(state, refs, location)
    else:
        origins = list(state.artifacts.values())
    if not origins:
        origins = [Origin(
            source=state.trigger_source,
            kind="cache_write",
            constraint="none",
            constraint_detail={"reason": "cache write with no traceable input"},
            writer_job=job_id,
            cache_key=cache_key,
            introduced_at=location,
        )]

    stored: List[Origin] = []
    for origin in origins:
        degraded = _degrade(
            origin, "none", state, location,
            subject={"cache_key": cache_key},
            note="content enters a shared cache; the cache enforces no "
                 "source constraint, so multiple writers become possible",
            extra={"cache_key": cache_key, "kind": "cache_write",
                   "writer_job": job_id, "introduced_at": location},
        )
        stored.append(degraded)
        cache_writes.setdefault(cache_key, []).append((state.path_id, degraded))
    state.caches.setdefault(cache_key, []).extend(stored)


def _apply_cache_read(
    state: PathState,
    job_id: str,
    action: Dict[str, Any],
    cache_writes: Dict[str, List[Tuple[str, Origin]]],
) -> None:
    cache_key = action["cache_key"]
    into = action["into"]
    key = (into["name"], into.get("commit", ""))
    location = f"{job_id}:cache_read:{cache_key}"

    possible: Dict[Tuple[Any, ...], Origin] = {}
    for path_id, origin in cache_writes.get(cache_key, []):
        candidate = Origin(**{**origin.__dict__, "losses": list(origin.losses)})
        possible.setdefault(candidate.signature(), candidate)
    for origin in state.caches.get(cache_key, []):
        possible.setdefault(origin.signature(), origin)

    if not possible:
        origin = Origin(
            source="unknown",
            kind="cache_read",
            constraint="none",
            constraint_detail={"reason": f"no known writer for cache key {cache_key!r}"},
            writer_job=None,
            cache_key=cache_key,
            introduced_at=location,
        )
        loss = {
            "location": location,
            "subject": {"cache_key": cache_key},
            "from_constraint": None,
            "to_constraint": "none",
            "note": "cache key has no known writer; content cannot be proven",
        }
        origin.losses.append(loss)
        state.log_loss(loss)
        state.artifacts[key] = origin
        return

    origins = list(possible.values())
    merged = _merge_origins(origins, state, location,
                            kind="cache_read", writer_job=job_id,
                            cache_key=cache_key)
    state.artifacts[key] = merged


# --------------------------------------------------------------------------- #
# Origin helpers
# --------------------------------------------------------------------------- #

def _resolve_refs(
    state: PathState, refs: Iterable[Dict[str, Any]], location: str
) -> List[Origin]:
    resolved: List[Origin] = []
    for ref in refs:
        if "cache_key" in ref:
            origins = state.caches.get(ref["cache_key"], [])
            if not origins:
                origins = [Origin(
                    source="unknown",
                    kind="dynamic_unknown",
                    constraint="none",
                    constraint_detail={"reason": "unresolved cache reference"},
                    cache_key=ref["cache_key"],
                    introduced_at=location,
                )]
            resolved.extend(origins)
            continue
        key = (ref["name"], ref.get("commit", ""))
        if key in state.artifacts:
            resolved.append(state.artifacts[key])
        else:
            # Name-only lookups must not merge distinct commits together.
            same_name = [o for (n, _c), o in state.artifacts.items() if n == ref["name"]]
            if same_name and not ref.get("commit"):
                resolved.extend(same_name)
            else:
                resolved.append(Origin(
                    source="unknown",
                    kind="dynamic_unknown",
                    constraint="none",
                    constraint_detail={
                        "reason": "referenced artifact is not produced on this path",
                        "artifact": {"name": ref["name"], "commit": ref.get("commit")},
                    },
                    introduced_at=location,
                ))
    return resolved


def _degrade(
    origin: Origin,
    new_constraint: str,
    state: PathState,
    location: str,
    subject: Dict[str, Any],
    note: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Origin:
    """Copy an origin with a weaker constraint, recording the first loss."""
    kwargs = dict(origin.__dict__)
    kwargs["losses"] = list(origin.losses)
    if extra:
        kwargs.update(extra)
    current_rank = _CONSTRAINT_RANK[origin.constraint]
    target_rank = _CONSTRAINT_RANK[new_constraint]
    if target_rank < current_rank:
        loss = {
            "location": location,
            "subject": subject,
            "from_constraint": origin.constraint,
            "to_constraint": new_constraint,
            "note": note,
        }
        kwargs["losses"].append(loss)
        state.log_loss(loss)
    kwargs["constraint"] = new_constraint
    return Origin(**kwargs)


def _merge_origins(
    origins: Sequence[Origin],
    state: PathState,
    location: str,
    kind: str,
    writer_job: Optional[str] = None,
    cache_key: Optional[str] = None,
) -> Origin:
    """Merge possible origins conservatively (weakest constraint wins)."""
    sources = {o.source for o in origins}
    if sources == {"production"}:
        source = "production"
    elif "unknown" in sources and len(sources) == 1:
        source = "unknown"
    elif "external_contribution" in sources:
        source = "external_contribution"
    elif "unknown" in sources:
        source = "mixed"
    else:
        source = "mixed"

    weakest = min(origins, key=lambda o: _CONSTRAINT_RANK[o.constraint])
    merged = Origin(
        source=source,
        kind=kind,
        constraint=weakest.constraint,
        constraint_detail={
            "possible_origins": [o.to_dict() for o in origins],
            "merge_reason": "conservative merge of all possible writers/inputs",
        },
        writer_job=writer_job,
        cache_key=cache_key,
        introduced_at=location,
    )
    for origin in origins:
        if _CONSTRAINT_RANK[origin.constraint] > _CONSTRAINT_RANK[merged.constraint]:
            loss = {
                "location": location,
                "subject": {"cache_key": cache_key} if cache_key else {"artifact": location},
                "from_constraint": origin.constraint,
                "to_constraint": merged.constraint,
                "note": "merging possible content sources drops the stronger "
                        "source constraint",
            }
            merged.losses.append(loss)
            state.log_loss(loss)
    # Retain every earlier loss as part of the merged origin's history.
    for origin in origins:
        for loss in origin.losses:
            if loss not in merged.losses:
                merged.losses.append(loss)
    return merged


# --------------------------------------------------------------------------- #
# Release evaluation and reporting
# --------------------------------------------------------------------------- #

def _evaluate_release(state: PathState, job: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    dynamic_refs = {m["ref"] for m in state.unprovable}
    job_deps = _job_dependency_set(job, state)
    for sensitive in job.get("sensitive_inputs", []):
        origins = _resolve_present_refs(state, [sensitive])
        if not origins:
            # Absent input: only unprovable if this job actually depends on a
            # missing dynamic definition that could have produced the content.
            if dynamic_refs & job_deps:
                origin = Origin(
                    source="unknown",
                    kind="dynamic_unknown",
                    constraint="none",
                    constraint_detail={
                        "reason": "input can only be supplied by a job whose "
                                  "dynamic definition is missing",
                        "artifact": _safe_subject(sensitive),
                        "missing_dynamic_refs": sorted(dynamic_refs & job_deps),
                    },
                    introduced_at=f"{job['id']}:sensitive_input",
                )
                origins = [origin]
            else:
                # Genuinely unreachable on this path; do not invent an origin.
                continue
        for origin in origins:
            findings.append(_build_finding(state, job, sensitive, origin))
    return findings


def _job_dependency_set(job: Dict[str, Any], state: PathState) -> set:
    """Static/dynamic refs that may feed this job, from the path ordering."""
    try:
        index = state.job_order.index(job["id"])
    except ValueError:
        return set()
    return set(state.job_order[:index])


def _resolve_present_refs(
    state: PathState, refs: Iterable[Dict[str, Any]]
) -> List[Origin]:
    """Like _resolve_refs but returns nothing for artifacts absent on the path."""
    resolved: List[Origin] = []
    for ref in refs:
        if "cache_key" in ref:
            if ref["cache_key"] in state.caches:
                resolved.extend(state.caches[ref["cache_key"]])
            continue
        key = (ref["name"], ref.get("commit", ""))
        if key in state.artifacts:
            resolved.append(state.artifacts[key])
        elif not ref.get("commit"):
            same_name = [
                o for (name, _commit), o in state.artifacts.items()
                if name == ref["name"]
            ]
            resolved.extend(same_name)
    return resolved


def _build_finding(
    state: PathState,
    job: Dict[str, Any],
    sensitive: Dict[str, Any],
    origin: Origin,
) -> Dict[str, Any]:
    unprovable_markers = list(state.unprovable)
    possible = origin.constraint_detail.get("possible_origins", [])
    origin_chain_dynamic = (
        origin.kind == "dynamic_unknown"
        or any(po.get("kind") == "dynamic_unknown" for po in possible)
    )
    # Unprovability is a property of *this input's* provenance chain.  A
    # missing dynamic definition elsewhere on the path does not taint an
    # unrelated pinned artifact.
    is_unprovable = origin_chain_dynamic or (
        origin.source == "unknown" and origin.constraint == "none"
    )
    via_cache = origin.cache_key is not None or any(
        po.get("cache_key") for po in origin.constraint_detail.get(
            "possible_origins", [])
    )

    if is_unprovable:
        classification = "unprovable_missing_dynamic_definition"
    elif origin.source == "external_contribution" and via_cache:
        classification = "external_contribution_via_shared_cache"
    elif origin.source == "external_contribution":
        classification = "external_contribution"
    elif (
        origin.source == "production"
        and origin.constraint == "pinned_commit"
    ):
        classification = "trusted_pinned_production"
    elif state.approvals:
        classification = "approved_but_unpinned_source"
    else:
        classification = "unpinned_untrusted_source"

    first_loss = origin.losses[0] if origin.losses else None
    return {
        "path_id": state.path_id,
        "release_job": job["id"],
        "trigger_id": state.trigger_id,
        "trigger_source": state.trigger_source,
        "branches": list(state.branches),
        "sensitive_input": _safe_subject(sensitive),
        "origin": origin.to_dict(),
        "classification": classification,
        "trusted": classification == "trusted_pinned_production",
        "approval_gate": bool(state.approvals),
        "approval_jobs": list(state.approvals),
        "approval_note": (
            "manual approval only permits job execution; it does not pin or "
            "attest the source of any input content"
            if state.approvals else "no approval gate on this path"
        ),
        "first_source_constraint_loss": first_loss,
        "unprovable_markers": unprovable_markers,
    }


def _safe_subject(ref: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {"name", "commit", "cache_key", "type"}
    return {k: v for k, v in ref.items() if k in allowed}


def _path_report(state: PathState) -> Dict[str, Any]:
    return {
        "path_id": state.path_id,
        "trigger_id": state.trigger_id,
        "trigger_source": state.trigger_source,
        "branches": list(state.branches),
        "job_order": list(state.job_order),
        "approvals": list(state.approvals),
        "artifacts": [
            {
                "artifact": {"name": name, "commit": commit},
                "origin": origin.to_dict(),
            }
            for (name, commit), origin in sorted(state.artifacts.items())
        ],
        "caches": [
            {
                "cache_key": key,
                "possible_writer_origins": [o.to_dict() for o in origins],
            }
            for key, origins in sorted(state.caches.items())
        ],
        "source_constraint_losses": list(state.loss_log),
        "unprovable": list(state.unprovable),
    }


def _cache_writer_report(
    cache_writes: Dict[str, List[Tuple[str, Origin]]]
) -> List[Dict[str, Any]]:
    report = []
    for key in sorted(cache_writes):
        writers: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for path_id, origin in cache_writes[key]:
            sig = (path_id, origin.writer_job, origin.source, origin.introduced_at)
            writers[sig] = {
                "path_id": path_id,
                "writer_job": origin.writer_job,
                "source": origin.source,
                "introduced_at": origin.introduced_at,
            }
        report.append({
            "cache_key": key,
            "possible_writers": list(writers.values()),
            "writer_count": len(writers),
        })
    return report


def _summary(
    findings: List[Dict[str, Any]],
    paths: List[Dict[str, Any]],
    dynamic: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    by_class: Dict[str, int] = {}
    for finding in findings:
        by_class[finding["classification"]] = \
            by_class.get(finding["classification"], 0) + 1
    return {
        "feasible_path_count": len(paths),
        "release_finding_count": len(findings),
        "findings_by_classification": by_class,
        "dynamic_definitions_missing": sorted(dynamic),
        "any_untrusted_path_to_release": any(
            not f["trusted"] for f in findings
        ),
    }
