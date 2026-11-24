"""命令行入口：python -m region_preflight <config.json> [--json]。"""

from __future__ import annotations

import argparse
import json
import sys

from .config import load_config
from .errors import PreflightConfigError
from .preflight import PreflightEngine
from .report import render_text, to_dict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="发布预检：数据地域与故障切换路径核对"
    )
    parser.add_argument("config", help="流水线配置 JSON 文件路径")
    parser.add_argument(
        "--json", action="store_true", help="以 JSON 输出完整结构化结果"
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
        result = PreflightEngine(config).run()
    except PreflightConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(to_dict(result), ensure_ascii=False, indent=2))
    else:
        print(render_text(result), end="")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
