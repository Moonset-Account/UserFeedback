"""命令行入口：python -m precheck <snapshot_dir>

读取快照目录（defaults.json / org_boundary.json / resource_policies.json /
prefixes.json），将预检报告以 JSON 输出到标准输出。
"""

from __future__ import annotations

import json
import sys

from .evaluator import evaluate_snapshot
from .loader import load_snapshot


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("用法: python -m precheck <snapshot_dir>", file=sys.stderr)
        return 2
    snapshot = load_snapshot(args[0])
    report = evaluate_snapshot(snapshot)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
