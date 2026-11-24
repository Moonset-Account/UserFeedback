"""审查结果的纯字典序列化（只依赖标准库，便于 JSON 输出）。"""

from __future__ import annotations

from typing import Any

from .model import (
    Change,
    Finding,
    Prefix,
    Probe,
    Route,
    Step,
    TraceResult,
    TransitGrant,
)


def prefix_to_dict(prefix: Prefix) -> dict[str, Any]:
    return {"prefix": prefix.text, "prefix_length": prefix.prefix_length}


def route_to_dict(route: Route) -> dict[str, Any]:
    return {
        "route_id": route.route_id,
        "domain": route.domain,
        "prefix": route.prefix.text,
        "next_hop": route.next_hop,
        "metric": route.metric,
    }


def step_to_dict(step: Step) -> dict[str, Any]:
    return {
        "order": step.order,
        "domain": step.domain,
        "route_id": step.route.route_id,
        "matched_prefix": step.route.prefix.text,
        "next_hop": step.next_hop,
        "peer_domain": step.peer,
        "authorized": step.authorized,
        "is_origin_egress": step.is_origin_egress,
    }


def trace_to_dict(result: TraceResult) -> dict[str, Any]:
    return {
        "probe_id": result.probe_id,
        "target": result.target.text,
        "origin_domain": result.origin_domain,
        "status": result.status.value,
        "compliant": result.is_compliant,
        "steps": [step_to_dict(s) for s in result.steps],
        "unauthorized_step": (
            step_to_dict(result.unauthorized_step)
            if result.unauthorized_step is not None
            else None
        ),
    }


def finding_to_dict(finding: Finding) -> dict[str, Any]:
    return {
        "probe_id": finding.probe_id,
        "affected_prefix": finding.affected_prefix.text,
        "origin_domain": finding.origin_domain,
        "first_unauthorized_step": step_to_dict(finding.first_unauthorized_step),
        "trigger_change_id": finding.trigger_change_id,
        "status": finding.status.value,
    }


def grant_to_dict(grant: TransitGrant) -> dict[str, Any]:
    return {
        "transit_domain": grant.transit_domain,
        "from_domain": grant.from_domain,
        "to_domain": grant.to_domain,
        "direction": grant.direction.value,
    }


def change_to_dict(change: Change) -> dict[str, Any]:
    return {
        "change_id": change.change_id,
        "action": change.action.value,
        "domain": change.domain,
        "route_id": change.route_id,
        "route": route_to_dict(change.route) if change.route is not None else None,
    }


def probe_to_dict(probe: Probe) -> dict[str, Any]:
    return {
        "probe_id": probe.probe_id,
        "origin_domain": probe.origin_domain,
        "target": probe.target.text,
    }
