#!/usr/bin/env python3
"""GraphQL 解析器授权覆盖 CI 校验器入口。

用法:
    python3 app.py <schema.json> <resolvers.json> <operations.json>

退出码:
    0  分析完成, 无 missing (缺少授权检查) 结论
    1  存在 missing 结论 (CI 应失败)
    2  输入不合法或 IO 错误

报告以 JSON 输出到 stdout, 含 missing / covered / manual_review 三组结论,
每条结论附根操作到字段的路径与命中契约; 片段循环与缺失的解析器元数据在
diagnostics 中分别报告。
"""
import json
import sys

from checker import Checker, InputError, MISSING


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv):
    if len(argv) != 4:
        sys.stderr.write(__doc__)
        return 2
    try:
        schema = _load(argv[1])
        resolvers = _load(argv[2])
        document = _load(argv[3])
    except (OSError, ValueError) as exc:
        sys.stderr.write("input error: %s\n" % exc)
        return 2
    try:
        report = Checker(schema, resolvers, document).run()
    except InputError as exc:
        sys.stderr.write("input error: %s\n" % exc)
        return 2
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return 1 if report["summary"][MISSING] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
