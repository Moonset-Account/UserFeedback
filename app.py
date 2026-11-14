#!/usr/bin/env python3
"""SEC-VULN-033 会话代次参考模型 CLI 主入口。

用法：
    python3 app.py [run] [--scenario NAME] [--json]   运行场景并输出回归报告
    python3 app.py boundary [--json]                  逐条输出样例/边界断言
    python3 app.py rules                              列出回归规则
    python3 app.py scenarios                          列出场景

退出码：0 = 全部通过；1 = 存在失败或证据不足。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from boundary_checks import run_boundary_checks
from session_model.regression import RULE_DESCRIPTIONS, evaluate
from session_model.scenarios import SCENARIOS, run_all


def _trace_to_dict(trace):
    return {
        "scenario": trace.scenario,
        "decisions": [dataclasses.asdict(d) for d in trace.decisions],
        "primary_log": [
            {
                "seq": e.seq,
                "session_id": e.session_id,
                "op_id": e.op_id,
                "op": e.op.value,
                "from_state": e.from_state.value,
                "to_state": e.to_state.value,
                "generation": e.generation,
                "ack_delivered": e.ack_delivered,
            }
            for e in trace.primary.log
        ],
    }


def _result_to_dict(r):
    return {
        "rule_id": r.rule_id,
        "title": r.title,
        "status": r.status,
        "checked": r.checked,
        "failures": [dataclasses.asdict(f) for f in r.failures],
    }


def render_text(traces, results) -> str:
    lines = ["=== SEC-VULN-033 会话代次回归报告 ===", ""]
    for t in traces:
        lines.append("场景 %s: %d 条决策记录, 主节点日志 %d 条"
                     % (t.scenario, len(t.decisions), len(t.primary.log)))
        for d in t.decisions:
            note = "  # " + d.note if d.note else ""
            lines.append(
                "  #%02d %-14s node=%-10s sid=%-8s gen=%-4s ack=%-5s rver=%-4s -> %s%s"
                % (d.order, d.action, d.node, d.session_id, d.generation,
                   d.write_ack, d.replica_version, d.verdict, note)
            )
        lines.append("")
    lines.append("--- 回归规则 ---")
    for r in results:
        lines.append("规则 %s [%s] %s (检查 %d 项, 失败 %d)"
                     % (r.rule_id, r.title, r.status, r.checked, len(r.failures)))
        for f in r.failures:
            lines.append(
                "  失败: scenario=%s order=%s gen=%s write_ack=%s replica_version=%s 原因=%s"
                % (f.scenario, f.decision_order, f.generation, f.write_ack,
                   f.replica_version, f.reason)
            )
    overall = "PASS" if all(r.status == "PASS" for r in results) else "FAIL"
    lines.append("")
    lines.append("总体: " + overall)
    return "\n".join(lines)


def cmd_run(args) -> int:
    if args.scenario:
        if args.scenario not in SCENARIOS:
            print("未知场景: %s（可选: %s）" % (args.scenario, ", ".join(SCENARIOS)), file=sys.stderr)
            return 1
        traces = [SCENARIOS[args.scenario]()]
    else:
        traces = run_all()
    results = evaluate(traces)
    if args.json:
        print(json.dumps({
            "traces": [_trace_to_dict(t) for t in traces],
            "rules": [_result_to_dict(r) for r in results],
            "overall": "PASS" if all(r.status == "PASS" for r in results) else "FAIL",
        }, ensure_ascii=False, indent=2))
    else:
        print(render_text(traces, results))
    return 0 if all(r.status == "PASS" for r in results) else 1


def cmd_boundary(args) -> int:
    checks = run_boundary_checks()
    if args.json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        for c in checks:
            print("[%s] %s | 实际=%r | 期望=%r"
                  % ("OK" if c["ok"] else "FAIL", c["case_id"], c["actual"], c["expected"]))
    bad = sum(1 for c in checks if not c["ok"])
    if not args.json:
        print("样例/边界断言: %d 条, 未通过 %d 条" % (len(checks), bad))
    return 0 if bad == 0 else 1


def cmd_rules(_args) -> int:
    for rid, desc in RULE_DESCRIPTIONS.items():
        print("%s: %s" % (rid, desc))
    return 0


def cmd_scenarios(_args) -> int:
    for name, fn in SCENARIOS.items():
        print("%-28s %s" % (name, (fn.__doc__ or "").strip()))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="SEC-VULN-033 会话代次参考模型")
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "boundary", "rules", "scenarios"])
    parser.add_argument("--scenario", help="只运行指定场景")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    if args.command == "run":
        return cmd_run(args)
    if args.command == "boundary":
        return cmd_boundary(args)
    if args.command == "rules":
        return cmd_rules(args)
    return cmd_scenarios(args)


if __name__ == "__main__":
    sys.exit(main())
