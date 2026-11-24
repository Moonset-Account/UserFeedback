"""结果序列化与可读文本报告。"""

from __future__ import annotations

from dataclasses import asdict

from .models import PreflightResult


def to_dict(result: PreflightResult) -> dict:
    """把预检结果转成纯字典（条件触发器会渲染成字符串）。"""
    payload = {
        "name": result.name,
        "home_region": result.home_region,
        "passed": result.passed,
        "data_types": result.data_types,
        "reports": {},
        "findings": [],
    }
    for dtype, report in result.reports.items():
        payload["reports"][dtype] = {
            "approved_regions": report.approved_regions,
            "transitable_regions": report.transitable_regions,
            "persistable_regions": report.persistable_regions,
            "transit_only_regions": report.transit_only_regions,
            "main_route_regions": report.main_route_regions,
            "cross_region_landings": [
                {
                    "node_id": landing.node_id,
                    "step": landing.step,
                    "action": landing.action,
                    "region": landing.region,
                    "path": landing.path,
                    "trigger_conditions": landing.conditions,
                    "approved": landing.approved,
                }
                for landing in report.cross_region_landings
            ],
            "transit_records": [
                {
                    "node_id": record.node_id,
                    "step": record.step,
                    "action": record.action,
                    "region": record.region,
                    "on_failover": record.on_failover,
                    "path": record.path,
                    "trigger_conditions": [
                        t.render() for t in record.trigger_chain
                    ],
                }
                for record in report.transit_records
            ],
            "findings": [_finding_dict(f) for f in report.findings],
        }
    payload["findings"] = [_finding_dict(f) for f in result.findings]
    return payload


def _finding_dict(finding) -> dict:
    data = asdict(finding)
    data["trigger_chain"] = [t.render() for t in finding.trigger_chain]
    return data


def render_text(result: PreflightResult) -> str:
    """渲染面向发布预检评审的纯文本报告。"""
    lines: list[str] = []
    lines.append(f"发布地域预检报告：{result.name}")
    lines.append(f"本区（home_region）：{result.home_region}")
    lines.append(f"总体结论：{'通过' if result.passed else '不通过（存在 error）'}")
    lines.append("")

    for dtype in result.data_types:
        report = result.reports[dtype]
        lines.append(f"[数据类型] {dtype}")
        lines.append(f"  法务核准区域      : {', '.join(report.approved_regions)}")
        lines.append(f"  可经过区域        : {', '.join(report.transitable_regions) or '（无）'}")
        lines.append(f"  可持久化区域      : {', '.join(report.persistable_regions) or '（无）'}")
        lines.append(f"  仅途经（转发）区域: {', '.join(report.transit_only_regions) or '（无）'}")
        lines.append(f"  主路径涉及区域    : {', '.join(report.main_route_regions) or '（无）'}")

        if report.cross_region_landings:
            lines.append("  异地回退落地（主路径在本区、回退分支异地留副本）:")
            for landing in report.cross_region_landings:
                verdict = "已核准" if landing.approved else "未核准"
                lines.append(
                    f"    - 节点 {landing.node_id}（{landing.step}/{landing.action}）"
                    f" -> 区域 {landing.region} [{verdict}]"
                )
                lines.append(f"      入口路径: {' -> '.join(landing.path)}")
                lines.append(
                    "      触发条件: "
                    + (" -> ".join(landing.conditions) or "（无）")
                )
        else:
            lines.append("  异地回退落地      : （无）")

        if report.transit_records:
            lines.append("  仅转发节点（另行记录，不参与核准）:")
            for record in report.transit_records:
                branch = "回退分支" if record.on_failover else "主路径"
                lines.append(
                    f"    - {record.node_id}（{record.action}）区域 "
                    f"{record.region} [{branch}]"
                )

        if report.findings:
            lines.append("  发现:")
            for finding in report.findings:
                lines.append(
                    f"    [{finding.severity.upper()}] {finding.code}: "
                    f"{finding.message}"
                )
        else:
            lines.append("  发现: （无）")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
