"""提取任务的命令行入口，供 Web 后端以 ``python -I`` 子进程方式调用。

Web 侧把不可信上传文件交给独立解释器进程处理，避免 cwd / sys.path 注入。
结果以 JSON 打到 stdout，错误以非零退出码 + stderr 返回。
"""
from __future__ import annotations

import argparse
import json
import sys

from .pipeline import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从本地 WARC 归档提取研究资料字段")
    parser.add_argument("warc_path", help="WARC 文件路径（.warc 或 .warc.gz）")
    parser.add_argument("--pretty", action="store_true", help="格式化输出 JSON")
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(args.warc_path)
    except FileNotFoundError:
        print(json.dumps({"fatal": f"文件不存在：{args.warc_path}"}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    except Exception as exc:  # 提取器自身缺陷也要结构化上报
        print(json.dumps({"fatal": f"提取失败：{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
