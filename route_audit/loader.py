"""从纯 dict / JSON 文件装载审查场景（合成数据入口）。

场景结构（全部字段名见 examples/）::

    {
      "domains": ["corp", "edge-a", ...],
      "next_hops": [{"domain": "corp", "next_hop": "nh1", "peer_domain": "edge-a"}],
      "grants":   [{"transit_domain": "edge-a", "from_domain": "corp",
                    "to_domain": "dc", "direction": "bidirectional"}],
      "routes":   [{"route_id": "r1", "domain": "corp",
                    "prefix": "10.10.0.0/16", "next_hop": "nh1", "metric": 100}],
      "probes":   [{"probe_id": "p1", "origin_domain": "corp",
                    "target": "10.10.1.0/24"}],
      "changes":  [{"change_id": "c1", "action": "withdraw",
                    "domain": "corp", "route_id": "r1"}]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .auditor import AuditReport, ChangeAuditor
from .exceptions import ScenarioFormatError
from .model import (
    Change,
    ChangeAction,
    Prefix,
    Probe,
    Route,
    TransitDirection,
    TransitGrant,
)
from .state import NextHopMap, RoutingState


@dataclass
class Scenario:
    state: RoutingState
    probes: tuple[Probe, ...]
    changes: tuple[Change, ...]

    def build_auditor(self) -> ChangeAuditor:
        return ChangeAuditor(self.state, self.probes)

    def run(self) -> AuditReport:
        auditor = self.build_auditor()
        return auditor.run_all(self.changes)


def _require(mapping: dict[str, Any], key: str, ctx: str) -> Any:
    if key not in mapping:
        raise ScenarioFormatError(f"{ctx} 缺少必填字段 {key!r}")
    return mapping[key]


def load_scenario_dict(data: dict[str, Any]) -> Scenario:
    if not isinstance(data, dict):
        raise ScenarioFormatError("场景根节点必须是对象")

    domains = list(_require(data, "domains", "场景"))
    if not domains or any(not isinstance(d, str) for d in domains):
        raise ScenarioFormatError("domains 必须是非空字符串列表")
    domain_set = set(domains)
    if len(domain_set) != len(domains):
        raise ScenarioFormatError("domains 存在重复登记")

    def check_domain(value: str, ctx: str) -> None:
        if value not in domain_set:
            raise ScenarioFormatError(f"{ctx} 引用了未登记域 {value!r}")

    # 下一跳映射（按域独立解释）
    next_hop_map = NextHopMap()
    for i, entry in enumerate(data.get("next_hops", [])):
        ctx = f"next_hops[{i}]"
        domain = _require(entry, "domain", ctx)
        next_hop = _require(entry, "next_hop", ctx)
        peer = _require(entry, "peer_domain", ctx)
        check_domain(domain, ctx)
        check_domain(peer, ctx)
        next_hop_map.add(domain, next_hop, peer)

    # 中转许可（方向性）
    grants: list[TransitGrant] = []
    for i, entry in enumerate(data.get("grants", [])):
        ctx = f"grants[{i}]"
        transit_domain = _require(entry, "transit_domain", ctx)
        from_domain = _require(entry, "from_domain", ctx)
        to_domain = _require(entry, "to_domain", ctx)
        for value, label in (
            (transit_domain, "transit_domain"),
            (from_domain, "from_domain"),
            (to_domain, "to_domain"),
        ):
            if value != "*":
                check_domain(value, f"{ctx}.{label}")
        direction = TransitDirection(
            entry.get("direction", TransitDirection.BIDIRECTIONAL.value)
        )
        grants.append(
            TransitGrant(
                transit_domain=transit_domain,
                from_domain=from_domain,
                to_domain=to_domain,
                direction=direction,
            )
        )

    state = RoutingState(domains, next_hop_map=next_hop_map, grants=grants)

    # 初始路由
    for i, entry in enumerate(_require(data, "routes", "场景")):
        ctx = f"routes[{i}]"
        domain = _require(entry, "domain", ctx)
        check_domain(domain, ctx)
        state.add_route(
            Route(
                route_id=_require(entry, "route_id", ctx),
                domain=domain,
                prefix=Prefix(_require(entry, "prefix", ctx)),
                next_hop=entry.get("next_hop"),
                metric=int(entry.get("metric", 100)),
            )
        )

    # 探测
    probes: list[Probe] = []
    for i, entry in enumerate(_require(data, "probes", "场景")):
        ctx = f"probes[{i}]"
        origin = _require(entry, "origin_domain", ctx)
        check_domain(origin, ctx)
        probes.append(
            Probe(
                probe_id=_require(entry, "probe_id", ctx),
                origin_domain=origin,
                target=Prefix(_require(entry, "target", ctx)),
            )
        )

    # 路由变化
    changes: list[Change] = []
    for i, entry in enumerate(data.get("changes", [])):
        ctx = f"changes[{i}]"
        action = ChangeAction(_require(entry, "action", ctx))
        domain = _require(entry, "domain", ctx)
        check_domain(domain, ctx)
        route_id = _require(entry, "route_id", ctx)
        route = None
        if action is ChangeAction.ADD:
            rctx = f"{ctx}.route"
            route_entry = _require(entry, "route", ctx)
            if route_entry.get("domain", domain) != domain:
                raise ScenarioFormatError(f"{rctx} 的 domain 与变化不一致")
            route = Route(
                route_id=route_id,
                domain=domain,
                prefix=Prefix(_require(route_entry, "prefix", rctx)),
                next_hop=route_entry.get("next_hop"),
                metric=int(route_entry.get("metric", 100)),
            )
        changes.append(
            Change(
                change_id=_require(entry, "change_id", ctx),
                action=action,
                domain=domain,
                route_id=route_id,
                route=route,
            )
        )

    return Scenario(state=state, probes=tuple(probes), changes=tuple(changes))


def load_scenario_file(path: str | Path) -> Scenario:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return load_scenario_dict(data)
