#!/usr/bin/env python3
"""SEC-VULN-006 CLI：签名下载对象规范化一致性检查。

用法：
    python3 app.py [--input records.json] [--rules rules.json] [--output report.json]

输入：
    --input  已解析的合成记录 JSON 数组，每条记录字段：
             record_no(来源编号)、tenant、object_no、version、query。
             version 为数组时表示重复版本字段；为 null/缺省表示版本丢失。
    --rules  两套规则配置 JSON：{"rules_a": {...}, "rules_b": {...}}，
             字段见 core.RuleSet。
    缺省读取 samples/records.json 与 samples/rules.json。

输出：
    JSON 分歧报告，仅含 record_no、rule_versions、divergent_fields。
    写到 --output 指定文件，否则打印到 stdout。

退出码：
    0  检查完成且无分歧
    1  检查完成但存在分歧
    2  输入/配置错误
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import check_records, ruleset_from_dict  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT = os.path.join(BASE_DIR, "samples", "records.json")
DEFAULT_RULES = os.path.join(BASE_DIR, "samples", "rules.json")


def _load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="签名下载对象规范化一致性检查（离线，合成记录）"
    )
    parser.add_argument("--input", default=DEFAULT_INPUT, help="记录 JSON 文件路径")
    parser.add_argument("--rules", default=DEFAULT_RULES, help="规则配置 JSON 文件路径")
    parser.add_argument("--output", default=None, help="报告输出路径（缺省为 stdout）")
    args = parser.parse_args(argv)

    try:
        records = _load_json(args.input)
        rules_conf = _load_json(args.rules)
    except (OSError, json.JSONDecodeError) as exc:
        print("error: cannot load input: %s" % exc, file=sys.stderr)
        return 2

    if not isinstance(records, list):
        print("error: input must be a JSON array of records", file=sys.stderr)
        return 2

    try:
        rules_a = ruleset_from_dict(rules_conf["rules_a"])
        rules_b = ruleset_from_dict(rules_conf["rules_b"])
    except (KeyError, TypeError, ValueError) as exc:
        print("error: invalid rules config: %s" % exc, file=sys.stderr)
        return 2

    outcome = check_records(records, rules_a, rules_b)
    payload = json.dumps(outcome["report"], ensure_ascii=False, indent=2, sort_keys=True)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    else:
        print(payload)

    return 1 if outcome["report"] else 0


if __name__ == "__main__":
    sys.exit(main())
