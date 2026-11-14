#!/usr/bin/env python3
"""粘贴裁决模块 CLI 主入口。

用法：
    python3 app.py                     运行内置演示链路（复制->弹框->切换窗口->重新粘贴）
    python3 app.py <scenario.json>     执行场景文件并输出每步裁决结果与审计记录

场景文件格式（JSON）：
    {"steps": [
        {"op": "copy",   "workspace": "managed", "kind": "text",
         "classification": "confidential", "payload": "...", "text": "..."},
        {"op": "focus",  "workspace": "personal"},
        {"op": "answer", "value": true},
        {"op": "tick",   "seconds": 5},
        {"op": "paste"}
    ]}

输出（stdout，JSON）：
    {"results": [{"step": N, "action": ..., "reason": ..., ...}, ...],
     "audit":   [{...}, ...]}
审计记录只引用剪贴板版本与数据分级，不含剪贴板内容。
"""
from __future__ import annotations

import json
import sys

from adjudicator import Adjudicator
from memory_adapter import InMemorySystemAdapter


def run_steps(steps) -> dict:
    adapter = InMemorySystemAdapter()
    adjudicator = Adjudicator()
    results = []
    for i, step in enumerate(steps):
        op = step.get("op")
        if op == "copy":
            adapter.copy(
                payload=step["payload"],
                content_kind=step["kind"],
                classification=step["classification"],
                workspace=step["workspace"],
                text_representation=step.get("text"),
            )
        elif op == "focus":
            adapter.focus(step["workspace"])
        elif op == "answer":
            adapter.answer(bool(step["value"]))
        elif op == "tick":
            adapter.tick(float(step["seconds"]))
        elif op == "paste":
            result = adapter.paste(adjudicator)
            results.append({"step": i, **result.to_dict()})
        else:
            raise ValueError("unknown op at step %d: %r" % (i, op))
    return {
        "results": results,
        "audit": [r.to_dict() for r in adjudicator.audit.records],
    }


def demo_steps():
    """内置演示：受管编辑器复制机密文本 -> 个人应用粘贴（弹框确认）
    -> 切到另一目标窗口重新粘贴（重新弹框）-> 剪贴板内容变化后再次粘贴（重新判断）。"""
    return [
        {"op": "copy", "workspace": "managed", "kind": "text",
         "classification": "confidential", "payload": "Q3 roadmap (confidential)"},
        {"op": "focus", "workspace": "personal"},
        {"op": "answer", "value": True},
        {"op": "paste"},                                   # 弹框 -> 确认 -> 粘贴
        {"op": "paste"},                                   # 同源同目标同版本：确认仍有效
        {"op": "focus", "workspace": "personal-notes"},    # 切换目标应用
        {"op": "answer", "value": False},
        {"op": "paste"},                                   # 目标变了 -> 重新弹框 -> 拒绝
        {"op": "copy", "workspace": "managed", "kind": "text",
         "classification": "internal", "payload": "<b>hello</b>", "text": "hello"},
        {"op": "focus", "workspace": "personal"},
        {"op": "paste"},                                   # 版本变了 -> 重新判断：internal 文本降级
    ]


def main(argv) -> int:
    if len(argv) > 2:
        print(__doc__, file=sys.stderr)
        return 2
    if len(argv) == 2:
        with open(argv[1], "r", encoding="utf-8") as fh:
            scenario = json.load(fh)
        steps = scenario["steps"]
    else:
        steps = demo_steps()
    output = run_steps(steps)
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
