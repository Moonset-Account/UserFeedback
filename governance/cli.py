"""治理裁决命令行入口。

用法::

    python -m governance.cli payload.json [--pretty]

退出码：
    0 —— 裁决流程成功执行（结论可能是 proceed 或 suspended，
          suspended 是正常业务结果，不是工具失败）；
    2 —— 载荷文件或输入校验失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from governance.engine import evaluate_application
from governance.errors import GovernanceInputError
from governance.jsonio import load_application_payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="上线防护证据治理裁决：共因去重扣减与上线限额检查"
    )
    parser.add_argument("payload", help="裁决输入 JSON 文件路径")
    parser.add_argument(
        "--pretty", action="store_true", help="缩进输出 JSON 结果"
    )
    args = parser.parse_args(argv)

    try:
        with open(args.payload, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        assets, segments, controls, receipts, default_limit = (
            load_application_payload(payload)
        )
        decision = evaluate_application(
            assets,
            segments,
            controls,
            receipts,
            default_go_live_limit=default_limit,
        )
    except (OSError, json.JSONDecodeError, GovernanceInputError) as exc:
        print(f"裁决输入无效: {exc}", file=sys.stderr)
        return 2

    indent = 2 if args.pretty else None
    json.dump(decision.to_dict(), sys.stdout, ensure_ascii=False, indent=indent)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
