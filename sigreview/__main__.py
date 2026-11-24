"""命令行入口：对本地合成轨迹跑离线回归审查。

用法::

    python3 -m sigreview              # 输出中文文本报告
    python3 -m sigreview --json       # 输出机器可读 JSON
"""

from __future__ import annotations

import argparse
import json

from .review import RegressionReviewer
from .synthetic import build_fixture_traces


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="证书用途限制离线回归审查（合成数据，全程离线）"
    )
    parser.add_argument(
        "--json", action="store_true", help="输出 JSON 而非中文文本报告"
    )
    args = parser.parse_args(argv)

    traces = build_fixture_traces()
    report = RegressionReviewer().review(traces)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
