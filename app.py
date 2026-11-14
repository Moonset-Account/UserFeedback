#!/usr/bin/env python3
"""漏洞台账判定程序 CLI 入口。

用法：
  python3 app.py <input.json> [--output out.json] [--pretty]
  cat input.json | python3 app.py
"""
from __future__ import annotations

import argparse
import json
import sys

from vuln_ledger.engine import evaluate
from vuln_ledger.models import InputError
from vuln_ledger.parser import parse_json_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="依据提交关系与构建来源记录判定每个部署的漏洞修复状态"
    )
    parser.add_argument("input", nargs="?", help="输入 JSON 文件路径；省略时从标准输入读取")
    parser.add_argument("--output", "-o", help="输出 JSON 文件路径；省略时写到标准输出")
    parser.add_argument("--pretty", action="store_true", help="缩进格式化输出")
    args = parser.parse_args(argv)

    try:
        if args.input:
            with open(args.input, "r", encoding="utf-8") as fh:
                text = fh.read()
        else:
            text = sys.stdin.read()
        ledger = parse_json_text(text)
        result = evaluate(ledger)
    except InputError as exc:
        print(json.dumps({"error": "input_error", "message": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    except OSError as exc:
        print(json.dumps({"error": "io_error", "message": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 3

    indent = 2 if args.pretty else None
    payload = json.dumps(result, ensure_ascii=False, indent=indent)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
