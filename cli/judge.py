"""命令行判定工具：对单个 URL 或本地 HTML 文件执行采集 + 语义判定。

示例：
    python -m cli.judge http://127.0.0.1:8000/scenarios/ambiguous
    python -m cli.judge --mode file runtime/samples/pages/empty-ja.html
    python -m cli.judge --mode browser http://127.0.0.1:8000/scenarios/broken-truncated
    python -m cli.judge --rules runtime/rules/custom_rules.json <url>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from classifier.custom_rules import load_rules  # noqa: E402
from classifier.engine import classify_response  # noqa: E402
from collector import FetchError, fetch  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="响应语义判定 CLI")
    parser.add_argument("target", help="http(s) URL 或本地 HTML 文件路径")
    parser.add_argument("--mode", choices=["http", "file", "browser"], default="http")
    parser.add_argument("--referer", default="")
    parser.add_argument("--rules", help="自定义规则 JSON 文件路径")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--json", action="store_true", help="输出完整 JSON（含证据）")
    args = parser.parse_args()

    rules = load_rules(args.rules) if args.rules else []
    try:
        page = fetch(args.target, mode=args.mode, referer=args.referer,
                     timeout=args.timeout)
    except FetchError as exc:
        payload = {"category": "FETCH_FAILED", "error": str(exc), "isolated": False}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json
              else f"FETCH_FAILED — {exc}")
        return 2

    result = classify_response(page, rules=rules)
    if args.json:
        print(json.dumps({
            "target": args.target,
            "mode": args.mode,
            "http_status": page.status_code,
            "elapsed_ms": page.elapsed_ms,
            **result.to_dict(),
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"目标       : {args.target}（{args.mode}，HTTP {page.status_code}）")
    print(f"分类       : {result.category}"
          + ("（已隔离）" if result.isolated else ""))
    print(f"置信度/边界: {result.confidence:.2f}，"
          f"区间 {max(0, result.confidence - result.margin):.2f} ~ "
          f"{min(1, result.confidence + result.margin):.2f}，"
          f"前名分差 {result.margin:.2f}")
    if result.isolation_reasons:
        print("隔离原因   : " + "；".join(result.isolation_reasons))
    print("证据链     :")
    for e in result.evidences:
        sign = "+" if e.weight > 0 else ""
        print(f"  [{e.layer:10s}] {e.code:32s} {sign}{e.weight:<5.2f} "
              f"-> {e.supports:16s} {e.message}")
        if e.excerpt:
            print(f"               片段: {e.excerpt[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
