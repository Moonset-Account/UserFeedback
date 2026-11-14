#!/usr/bin/env python3
"""证据汇总器 CLI 入口。

用法：
  python3 app.py --input bundle.json [--pretty]
  python3 app.py --fix fix.json --deployments deployments.json \
      --regression regression.json --machines machines.json [--pretty]

输出：JSON 报告，按 (组件, 环境) 给出 待验证/待部署/可关闭 结论与证据链。
退出码：0 = 正常生成报告；2 = 输入缺失或校验失败。
"""

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aggregator.loader import LoaderError, load_bundle, load_parts
from aggregator.evaluate import evaluate


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="漏洞修复工单关闭证据汇总器：按组件+环境输出 待验证/待部署/可关闭 结论")
    parser.add_argument("--input", help="单个 bundle JSON 文件（含 fix/deployments/regression/machines 四键）")
    parser.add_argument("--fix", help="修复清单 JSON（组件与修复产物身份）")
    parser.add_argument("--deployments", help="部署实例清单 JSON")
    parser.add_argument("--regression", help="回归检查结果 JSON")
    parser.add_argument("--machines", help="机器清单 JSON")
    parser.add_argument("--pretty", action="store_true", help="缩进格式化输出")
    args = parser.parse_args(argv)

    try:
        if args.input:
            data = load_bundle(args.input)
        elif all([args.fix, args.deployments, args.regression, args.machines]):
            data = load_parts(args.fix, args.deployments, args.regression, args.machines)
        else:
            parser.error("必须提供 --input，或同时提供 --fix/--deployments/--regression/--machines")
    except LoaderError as exc:
        print("输入错误: %s" % exc, file=sys.stderr)
        return 2

    report = evaluate(data)
    json.dump(report, sys.stdout, ensure_ascii=False,
              indent=2 if args.pretty else None, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
