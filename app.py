#!/usr/bin/env python3
"""容器发布准入检查 CLI。

用法:
    python3 app.py --candidate C.json --baseline B.json --exceptions E.json \
        [--deployed D.json] [--now ISO8601] [--pretty]

退出码: 0=放行(allow) 1=拦截(block) 2=输入/用法错误。
结果 JSON 输出到 stdout，供流水线消费。
"""

import argparse
import json
import sys

from admission import InputError, check_release


def _load_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise InputError("无法读取%s文件 %s: %s" % (what, path, exc))
    except json.JSONDecodeError as exc:
        raise InputError("%s文件 %s 不是合法 JSON: %s" % (what, path, exc))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="容器发布准入检查：特权模式 / root 运行 / 宿主机目录挂载，"
                    "支持审批例外与已上线版本比对。")
    parser.add_argument("--candidate", required=True, help="候选工作负载 JSON（必填）")
    parser.add_argument("--deployed", help="已上线版本工作负载 JSON（可选，提供时输出新增违规）")
    parser.add_argument("--baseline", help="团队基线 JSON（可选，缺省全部规则强制）")
    parser.add_argument("--exceptions", help="审批例外 JSON（可选）")
    parser.add_argument("--now", help="评估时刻 ISO 8601（可选，用于确定性地判断例外到期）")
    parser.add_argument("--pretty", action="store_true", help="缩进输出 JSON")
    args = parser.parse_args(argv)

    try:
        candidate = _load_json(args.candidate, "候选工作负载")
        deployed = _load_json(args.deployed, "已上线版本") if args.deployed else None
        baseline = _load_json(args.baseline, "团队基线") if args.baseline else None
        exceptions = _load_json(args.exceptions, "审批例外") if args.exceptions else None
        result = check_release(candidate, baseline=baseline, exceptions=exceptions,
                               deployed=deployed, now=args.now)
    except InputError as exc:
        print(json.dumps({"decision": "error", "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2

    indent = 2 if args.pretty else None
    json.dump(result, sys.stdout, ensure_ascii=False, indent=indent)
    sys.stdout.write("\n")
    return 0 if result["decision"] == "allow" else 1


if __name__ == "__main__":
    sys.exit(main())
