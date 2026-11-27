"""命令行入口：读取文书模型 JSON，输出检查报告。

用法：
    python3 -m sigcheck.cli path/to/document.json [-o report.json]

退出码：
    0  检查完成且未发现越界
    1  检查完成但发现越界修订
    2  输入模型不合法
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .inspector import inspect_document
from .model import DocumentModelError, load_document


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="文书签章修订检查器：定位最早越界修订及引用链"
    )
    parser.add_argument("model", help="文书模型 JSON 文件路径")
    parser.add_argument(
        "-o",
        "--output",
        help="报告输出路径（缺省输出到标准输出）",
    )
    args = parser.parse_args(argv)

    try:
        with open(args.model, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        document = load_document(payload)
        report = inspect_document(document)
    except DocumentModelError as exc:
        print(f"输入模型不合法: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"无法读取模型文件: {exc}", file=sys.stderr)
        return 2

    rendered = report.to_json()
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.write("\n")
    else:
        print(rendered)

    return 1 if report.violated else 0


if __name__ == "__main__":
    raise SystemExit(main())
