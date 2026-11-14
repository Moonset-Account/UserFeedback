#!/usr/bin/env python3
"""隔离状态模块 CLI 主入口。

用法：
    python3 app.py demo                      内置演示：先隔离再解除、解除回执先到；
                                             另含“失败后重发当前意图”场景。
    python3 app.py run <events.jsonl>        处理本地事件流文件（每行一个 JSON 事件）。

事件格式（JSONL，# 开头与空行忽略）：
    {"type": "send",    "device": "dev-01", "action": "isolate" | "release"}
    {"type": "resend",  "device": "dev-01"}
    {"type": "receipt", "device": "dev-01", "cmd_id": 2, "result": "success" | "failed"}

输出：每个事件的处理结果 + 处理后控制台视图，结尾输出每台设备的 FINAL 行：
    FINAL device=<id> expected=<期望状态> confirmed=<最后确认状态> pending=<待确认命令数>
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from isolation_module import IsolationConsole


def demo_events():
    """内置演示事件流。

    场景 A（dev-01）：先隔离再解除，解除回执(cmd 2)先于隔离回执(cmd 1)到达。
    场景 B（dev-02）：隔离命令回执失败，按原操作序号重发后成功。
    """
    return [
        # --- 场景 A：乱序回执 ---
        {"type": "send", "device": "dev-01", "action": "isolate"},   # cmd 1, op 1
        {"type": "send", "device": "dev-01", "action": "release"},   # cmd 2, op 2
        {"type": "receipt", "device": "dev-01", "cmd_id": 2, "result": "success"},  # 解除回执先到
        {"type": "receipt", "device": "dev-01", "cmd_id": 1, "result": "success"},  # 隔离回执补历史
        # --- 场景 B：失败后重发 ---
        {"type": "send", "device": "dev-02", "action": "isolate"},   # cmd 3, op 1
        {"type": "receipt", "device": "dev-02", "cmd_id": 3, "result": "failed"},
        {"type": "resend", "device": "dev-02"},                      # cmd 4, 沿用 op 1
        {"type": "receipt", "device": "dev-02", "cmd_id": 4, "result": "success"},
    ]


def load_events(path):
    events = []
    with open(path, "r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno} 非法 JSON: {exc}") from exc
    return events


def format_view(view):
    pending = ",".join(
        f"cmd{p['cmd_id']}:{p['action']}#op{p['op_seq']}" for p in view["pending_commands"]
    )
    return (
        f"    控制台 device={view['device']} "
        f"期望状态={view['expected_state']} "
        f"最后确认状态={view['last_confirmed_state']}(op{view['confirmed_op_seq']}) "
        f"待确认命令=[{pending}]"
    )


def process_events(events):
    console = IsolationConsole()
    lines = []
    touched = []
    for idx, ev in enumerate(events, 1):
        etype = ev.get("type")
        device = ev.get("device")
        if etype == "send":
            rec = console.request(device, ev.get("action"))
            lines.append(
                f"[事件{idx}] 发送请求 device={device} action={rec.action} "
                f"-> cmd_id={rec.cmd_id} op_seq={rec.op_seq}"
            )
        elif etype == "resend":
            rec = console.resend(device)
            lines.append(
                f"[事件{idx}] 重发当前意图 device={device} action={rec.action} "
                f"-> cmd_id={rec.cmd_id} 沿用 op_seq={rec.op_seq}（发送历史保留）"
            )
        elif etype == "receipt":
            out = console.handle_receipt(device, ev.get("cmd_id"), ev.get("result"))
            if out["result"] == "failed":
                desc = "执行失败，确认状态不变，可重发当前意图"
            elif out["applied"]:
                desc = f"确认状态推进为 {out['confirmed_state']}(op{out['confirmed_op_seq']})"
            else:
                desc = (
                    f"历史补全 op{out['op_seq']}，不覆盖已确认的 "
                    f"{out['confirmed_state']}(op{out['confirmed_op_seq']})"
                )
            lines.append(
                f"[事件{idx}] 回执 device={device} cmd_id={out['cmd_id']} "
                f"result={out['result']} -> {desc}"
            )
        else:
            raise ValueError(f"事件{idx} 非法类型: {etype!r}")
        lines.append(format_view(console.console_view(device)))
        if device not in touched:
            touched.append(device)

    for dev in console.device_ids():
        view = console.console_view(dev)
        lines.append(
            f"FINAL device={dev} expected={view['expected_state']} "
            f"confirmed={view['last_confirmed_state']} pending={len(view['pending_commands'])}"
        )
        lines.append(
            f"最终设备状态: {dev} 期望状态={view['expected_state']} "
            f"最后确认状态={view['last_confirmed_state']} "
            f"待确认命令={len(view['pending_commands'])}条"
        )
    return lines


USAGE = __doc__


def main(argv):
    if len(argv) >= 2 and argv[1] == "demo":
        events = demo_events()
    elif len(argv) >= 3 and argv[1] == "run":
        events = load_events(argv[2])
    else:
        sys.stderr.write(USAGE)
        return 2
    try:
        for line in process_events(events):
            print(line)
    except Exception as exc:  # 非法事件流：如实报错而非静默
        print(f"错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
