"""命令行入口：python -m sigreview <cfg.json>

退出码：
    0  全部发布点通过且无结构性问题
    1  审查完成但存在阻断（证据不足或结构违规）
    2  CFG 文档格式错误
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .analysis import analyze_graph
from .loader import CFGFormatError, load_file


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sigreview",
        description="异步验签制品发布静态审查器",
    )
    parser.add_argument("cfg", help="构建器导出的控制流图 JSON 文件")
    parser.add_argument(
        "--trace-bound",
        type=int,
        default=8,
        help="分支轨迹截断长度（默认 8，保证分析有限）",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_file(args.cfg)
    except (CFGFormatError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2

    result = analyze_graph(cfg, trace_bound=args.trace_bound)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 1 if result.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
