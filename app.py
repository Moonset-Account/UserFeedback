#!/usr/bin/env python3
"""错误响应映射层 CLI 主入口。

用法：
    python3 app.py --demo
    python3 app.py --interface payments.charge --request-id req-123 \
        --registry examples/registry.json --error error.json
    cat error.json | python3 app.py --interface payments.charge \
        --request-id req-123 --error -

输出：stdout 打印 JSON ``{"client": ..., "internal": ...}``。
客户端视图只含短文案与链路编号；内部诊断视图含脱敏后的完整错误树。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from errmap import ErrorRegistry, map_error  # noqa: E402

DEFAULT_REGISTRY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "examples", "registry.json")


def _load_error(path):
    if path == "-":
        raw = sys.stdin.read()
    else:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("error input must be a JSON object")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="公开错误映射与内部诊断字段脱敏")
    parser.add_argument("--interface", default="unknown.interface",
                        help="接口名，记入内部诊断输出")
    parser.add_argument("--request-id", default="req-unknown",
                        help="请求标识 / 链路编号，两类输出保持一致")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY,
                        help="公开错误码登记配置 JSON 路径")
    parser.add_argument("--error", help="结构化错误对象 JSON 文件，'-' 表示 stdin")
    parser.add_argument("--demo", action="store_true",
                        help="运行内置两模块嵌套错误示例")
    args = parser.parse_args(argv)

    registry = ErrorRegistry.load(args.registry)

    if args.demo:
        from examples.modules import module_a_charge
        error_obj = module_a_charge()
        args.interface = "payments.charge"
        args.request_id = "req-demo-0001"
    elif args.error:
        error_obj = _load_error(args.error)
    else:
        parser.error("either --demo or --error is required")

    result = map_error(error_obj, args.interface, args.request_id, registry)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
