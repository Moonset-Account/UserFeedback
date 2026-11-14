#!/usr/bin/env python3
"""CLI 主入口: 告警事件生命周期管理。

从文件(或 stdin)逐行读取 JSON Lines 指令, 逐行输出处理结果(JSON Lines)。

指令格式(每行一个 JSON 对象, 按顺序处理):

  {"op": "hit", "report_id": "r-1", "tenant": "acme", "endpoint": "ws-001",
   "detection": "trojan.x",
   "occurred_at": "2026-11-14T09:00:00Z", "arrived_at": "2026-11-14T09:00:02Z"}

  {"op": "receipt", "receipt_id": "rc-1", "event_id": "EVT-0001",
   "disposition": "confirmed", "received_at": "2026-11-14T09:09:45Z"}

  {"op": "advance", "now": "2026-11-14T09:21:00Z"}

  {"op": "dump"}

时间字段接受 ISO-8601 字符串(可带 Z)或 epoch 秒数。
用法:
  python3 app.py [输入文件]            # 缺省从 stdin 读取
  python3 app.py --window-seconds 600  # 聚合窗口, 默认 600 秒(十分钟)
"""

import argparse
import json
import sys

from alert_lifecycle import AlertManager


def process_command(manager, cmd):
    """处理单条指令, 返回一个 JSON 可序列化的结果字典。"""
    if not isinstance(cmd, dict):
        raise ValueError("command must be a JSON object")
    op = cmd.get("op")
    if op == "hit":
        return manager.handle_hit(
            report_id=cmd["report_id"],
            tenant=cmd["tenant"],
            endpoint=cmd["endpoint"],
            detection=cmd["detection"],
            occurred_at=cmd["occurred_at"],
            arrived_at=cmd["arrived_at"],
        )
    if op == "receipt":
        return manager.handle_receipt(
            receipt_id=cmd["receipt_id"],
            event_id=cmd["event_id"],
            disposition=cmd.get("disposition", ""),
            received_at=cmd["received_at"],
        )
    if op == "advance":
        return manager.advance_to(cmd["now"])
    if op == "dump":
        return {"action": "dump", "snapshot": manager.snapshot()}
    raise ValueError("unknown op: %r" % (op,))


def run_stream(lines, manager, out):
    """逐行处理指令流, 逐行写出结果; 返回处理条数。"""
    processed = 0
    for lineno, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cmd = json.loads(line)
            result = process_command(manager, cmd)
        except Exception as exc:  # 输入错误不中断整批处理, 以 error 行输出
            result = {"action": "error", "line": lineno, "message": str(exc)}
        out.write(json.dumps(result, ensure_ascii=False) + "\n")
        processed += 1
    return processed


def main(argv=None):
    parser = argparse.ArgumentParser(description="告警事件生命周期管理 CLI")
    parser.add_argument("input", nargs="?", default=None,
                        help="JSON Lines 指令文件, 缺省读取 stdin")
    parser.add_argument("--window-seconds", type=int, default=600,
                        help="聚合窗口秒数, 默认 600(十分钟)")
    args = parser.parse_args(argv)

    manager = AlertManager(window_seconds=args.window_seconds)
    if args.input is None:
        run_stream(sys.stdin, manager, sys.stdout)
    else:
        with open(args.input, "r", encoding="utf-8") as fh:
            run_stream(fh, manager, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
