#!/usr/bin/env python3
"""端到端可重放示例：两条启动路径的能力推演。

运行：
    python -I examples/demo_graph.py
不依赖第三方库，仅使用本工作区 capsim 包。
场景：非 root 服务从 S 出发，左路普通执行（丢 effective）、
右路文件能力执行（fE 保住 effective），并演示降 bounding 与越界最短序列。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capsim import ALL_MASK, Edge, CapState, Graph, analyze  # noqa: E402
from capsim.caps import NAME_TO_BIT  # noqa: E402


def main() -> int:
    kill = NAME_TO_BIT["CAP_KILL"]
    net_raw = NAME_TO_BIT["CAP_NET_RAW"]
    chown = NAME_TO_BIT["CAP_CHOWN"]
    setpcap = NAME_TO_BIT["CAP_SETPCAP"]

    initial = CapState(
        permitted=kill | net_raw | chown | setpcap,
        effective=kill | net_raw | chown | setpcap,
        inheritable=kill | net_raw | chown,
        ambient=0,
        bounding=ALL_MASK,
    )

    edges = [
        # 普通执行后 E 清空，KILL 门控必阻断
        Edge("S", "plain", {"type": "plain_exec"}),
        Edge("plain", "exit_plain", {"type": "plain_exec"},
             requires_effective=("CAP_KILL",), label="plain-gated[KILL]"),
        # 文件能力 fE=1 保住 NET_RAW 的 effective
        Edge("S", "fcap", {"type": "file_cap_exec",
                           "permitted": ["CAP_NET_RAW"], "effective": True}),
        Edge("fcap", "exit_fcap", {"type": "plain_exec"},
             requires_effective=("CAP_NET_RAW",), label="fcap-gated[NET_RAW]"),
        # 降 bounding：初始 E 含 SETPCAP，可达，删除 KILL 后不可恢复
        Edge("S", "drop", {"type": "lower_bounding", "cap": "CAP_KILL"}),
        Edge("drop", "exit_drop", {"type": "plain_exec"}),
    ]
    graph = Graph(edges, exits=["exit_plain", "exit_fcap", "exit_drop"])
    result = analyze(graph, initial, allowed=["CAP_NET_RAW", "CAP_SETPCAP", "CAP_CHOWN"],
                     start="S")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
