#!/usr/bin/env python3
"""字段血缘分级与降级审批 —— CLI 主入口。

子命令：
  classify <manifest.json> [--out FILE]  按清单计算每个字段的分级与解释
  revoke   <manifest.json> <rule_id>     撤销某条降级批准后重新载入清单，
             [--out FILE]                列出因此受影响的下游字段
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lineage import (  # noqa: E402
    ManifestError,
    classify,
    diff_reports,
    load_manifest,
    revoke_rule,
)


def _emit(obj, out_path=None):
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)


def cmd_classify(args):
    manifest = load_manifest(args.manifest)
    report = classify(manifest)
    _emit(report, args.out)
    return 0


def cmd_revoke(args):
    manifest = load_manifest(args.manifest)
    before = classify(manifest)
    updated = revoke_rule(manifest, args.rule_id)
    after = classify(updated)
    affected = diff_reports(before, after)
    _emit(
        {
            "revoked_rule": args.rule_id,
            "affected_count": len(affected),
            "affected_fields": affected,
        },
        args.out,
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="app.py", description="字段血缘分级与降级审批（本地清单）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("classify", help="按清单计算每个字段的分级与一条解释来源")
    c.add_argument("manifest", help="清单 JSON 路径")
    c.add_argument("--out", help="同时将报告写入该文件")
    c.set_defaults(func=cmd_classify)

    r = sub.add_parser(
        "revoke", help="撤销某条降级批准后重新载入清单，列出受影响的下游字段"
    )
    r.add_argument("manifest", help="清单 JSON 路径")
    r.add_argument("rule_id", help="要撤销的降级规则 id")
    r.add_argument("--out", help="同时将结果写入该文件")
    r.set_defaults(func=cmd_revoke)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ManifestError as exc:
        print("清单错误: %s" % exc, file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print("文件不存在: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
