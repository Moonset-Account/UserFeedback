"""排程结果的文本与 JSON 渲染（只做展示，不参与任何判定）。"""

from __future__ import annotations

import json

from .model import Problem, Schedule
from .prefix import format_prefix


def render_text(problem: Problem, schedule: Schedule) -> str:
    """返回人类可读的逐轮归属与最小通告集文本。"""
    lines: list[str] = []
    lines.append(
        f"排程完成：总轮数={schedule.makespan}，"
        f"双地址轮数合计={schedule.dual_address_rounds}"
    )
    lines.append("最终归属：")
    for tnt in problem.tenants:
        pfx = schedule.placements.get(tnt.name)
        lines.append(f"  - {tnt.name}: {format_prefix(pfx) if pfx else '无'}")
    lines.append("")
    for r in range(1, schedule.makespan + 1):
        lines.append(f"第 {r} 轮")
        round_events = [e for e in schedule.events if e.round == r]
        if round_events:
            lines.append("  事件:")
            for ev in round_events:
                tag = " [临时]" if ev.temporary else ""
                verb = "委派" if ev.kind == "assign" else "撤告"
                lines.append(
                    f"    {verb} {format_prefix(ev.prefix)} -> {ev.tenant}"
                    f" @ {ev.upstream}{tag}"
                )
        by_up: dict[str, list] = {}
        for ad in schedule.advertisements:
            if ad.round == r:
                by_up.setdefault(ad.upstream, []).append(ad)
        lines.append("  最小通告集:")
        if not by_up:
            lines.append("    （无通告）")
        for ups_name in sorted(by_up):
            cap = problem.upstream_map[ups_name].max_advertisements
            ads = by_up[ups_name]
            lines.append(f"    {ups_name}（{len(ads)}/{cap} 条）:")
            for ad in ads:
                lines.append(
                    f"      {format_prefix(ad.prefix)}  归属={ad.tenant} 策略={ad.policy}"
                )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(problem: Problem, schedule: Schedule) -> str:
    """返回机器可读 JSON，字段与 :class:`~planner.model.Schedule` 对齐。"""
    payload = {
        "makespan": schedule.makespan,
        "dual_address_rounds": schedule.dual_address_rounds,
        "placements": {
            name: format_prefix(pfx) for name, pfx in sorted(schedule.placements.items())
        },
        "events": [
            {
                "round": ev.round,
                "kind": ev.kind,
                "tenant": ev.tenant,
                "upstream": ev.upstream,
                "prefix": format_prefix(ev.prefix),
                "temporary": ev.temporary,
            }
            for ev in schedule.events
        ],
        "advertisements": [
            {
                "round": ad.round,
                "upstream": ad.upstream,
                "prefix": format_prefix(ad.prefix),
                "tenant": ad.tenant,
                "policy": ad.policy,
            }
            for ad in schedule.advertisements
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
