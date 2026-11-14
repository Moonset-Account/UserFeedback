"""把分析报告渲染为人类可读的排查结论。"""

_STATUS_LABEL = {
    "reachable": "已证实可达",
    "unreachable": "在完整证据下不可达",
    "insufficient_evidence": "证据不足",
}


def render_conclusions(report):
    lines = []
    lines.append("== SEC-VULN-004 受影响依赖符号可达性排查结论 ==")
    for mode in sorted(report["modes"]):
        result = report["modes"][mode]
        lines.append("")
        lines.append("[模式 {0}] 入口: {1}".format(mode, ", ".join(result["entries"])))
        lines.append("  证据完整性: {0}".format(
            "完整" if result["evidence_complete"] else "不完整(存在可达的未解析动态边)"))
        for symbol in sorted(result["symbols"]):
            info = result["symbols"][symbol]
            status = info["status"]
            lines.append("  - {0}: {1}".format(symbol, _STATUS_LABEL[status]))
            if status == "reachable":
                lines.append("      可达路径: " + " -> ".join(info["path"]))
            elif status == "insufficient_evidence":
                for missing in info["missing_evidence"]:
                    lines.append("      缺少证据: " + missing)
    lines.append("")
    if report["priority_fixes"]:
        lines.append("需要优先修复的发布入口:")
        for fix in report["priority_fixes"]:
            lines.append("  ! {entry} -> {symbol} ({advisory}, 严重性 {severity})".format(**fix))
            lines.append("      路径: " + " -> ".join(fix["path"]))
    else:
        lines.append("发布模式没有已证实可达受影响符号的入口, 无需优先修复项。")
    return "\n".join(lines)
