"""JSON job-graph loading and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


class GraphError(ValueError):
    """Raised when the input graph is missing required structure."""


_KNOWN_ACTION_TYPES = {
    "produce",
    "download",
    "cache_write",
    "cache_read",
}


def load_graph(path: str | Path) -> Dict[str, Any]:
    """Load a job graph from a JSON file on disk."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    validate_graph(data)
    return data


def validate_graph(graph: Any) -> None:
    """Validate the structural assumptions the analyzer relies on."""
    if not isinstance(graph, dict):
        raise GraphError("graph root must be a JSON object")

    version = graph.get("schema_version", 1)
    if version != 1:
        raise GraphError(f"unsupported schema_version: {version!r} (expected 1)")

    triggers = graph.get("triggers", [])
    if not isinstance(triggers, list) or not triggers:
        raise GraphError("graph.triggers must be a non-empty list")
    for trig in triggers:
        if not isinstance(trig, dict) or "id" not in trig:
            raise GraphError("every trigger needs an id")
        if trig.get("source") not in ("production", "external_contribution", "manual"):
            raise GraphError(
                f"trigger {trig.get('id')!r} has unsupported source "
                f"{trig.get('source')!r}"
            )

    jobs = graph.get("jobs", [])
    if not isinstance(jobs, list):
        raise GraphError("graph.jobs must be a list")

    seen_ids = set()
    for job in jobs:
        if not isinstance(job, dict) or "id" not in job:
            raise GraphError("every job needs an id")
        jid = job["id"]
        if jid in seen_ids:
            raise GraphError(f"duplicate job id: {jid!r}")
        seen_ids.add(jid)

        if not isinstance(job.get("needs", []), list):
            raise GraphError(f"job {jid!r}: needs must be a list")
        if not isinstance(job.get("conditions", []), list):
            raise GraphError(f"job {jid!r}: conditions must be a list")
        if not isinstance(job.get("actions", []), list):
            raise GraphError(f"job {jid!r}: actions must be a list")
        if not isinstance(job.get("requires_approval", False), bool):
            raise GraphError(f"job {jid!r}: requires_approval must be boolean")
        if not isinstance(job.get("is_release", False), bool):
            raise GraphError(f"job {jid!r}: is_release must be boolean")

        for action in job.get("actions", []):
            atype = action.get("type")
            if atype not in _KNOWN_ACTION_TYPES:
                raise GraphError(
                    f"job {jid!r}: unknown action type {atype!r}; "
                    f"expected one of {sorted(_KNOWN_ACTION_TYPES)}"
                )
            _validate_action(jid, action)

    dynamic_refs = {
        item["ref"] for item in graph.get("dynamic_missing", []) or []
    }
    for job in jobs:
        for need in job.get("needs", []):
            target = need if isinstance(need, str) else need.get("job")
            if target not in seen_ids and target not in dynamic_refs:
                raise GraphError(
                    f"job {job['id']!r} needs unknown job {target!r}"
                )

    for missing in graph.get("dynamic_missing", []) or []:
        if not isinstance(missing, dict) or "ref" not in missing:
            raise GraphError("dynamic_missing entries need a ref string")

    if not any(j.get("is_release") for j in jobs):
        raise GraphError("graph has no release job (is_release=true)")


def _validate_action(job_id: str, action: Dict[str, Any]) -> None:
    atype = action["type"]
    if atype == "produce":
        _require_artifact_ref(job_id, action)
    elif atype == "download":
        _require_artifact_ref(job_id, action)
        if not isinstance(action.get("pinned", False), bool):
            raise GraphError(f"job {job_id!r}: download.pinned must be boolean")
        if action.get("pinned"):
            if not action.get("producer_job"):
                raise GraphError(
                    f"job {job_id!r}: pinned download needs producer_job"
                )
            if not action.get("producer_commit"):
                raise GraphError(
                    f"job {job_id!r}: pinned download needs producer_commit"
                )
    elif atype == "cache_write":
        if not action.get("cache_key"):
            raise GraphError(f"job {job_id!r}: cache_write needs cache_key")
    elif atype == "cache_read":
        if not action.get("cache_key"):
            raise GraphError(f"job {job_id!r}: cache_read needs cache_key")
        if not action.get("into"):
            raise GraphError(f"job {job_id!r}: cache_read needs into artifact ref")


def _require_artifact_ref(job_id: str, action: Dict[str, Any]) -> None:
    ref = action.get("artifact") or {}
    if not isinstance(ref, dict) or not ref.get("name"):
        raise GraphError(f"job {job_id!r}: action needs artifact.name")
    if "commit" in ref and not isinstance(ref["commit"], str):
        raise GraphError(f"job {job_id!r}: artifact.commit must be a string")
