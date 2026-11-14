#!/usr/bin/env python3
"""CLI 主入口:分支机构域名解析分流。

用法:
  python3 app.py generate --config CONFIG.json
      按分支生成生效的域名分流规则(JSON 输出)。

  python3 app.py query --config CONFIG.json --branch BRANCH_ID --name NAME
      试算一次查询的去向(JSON 输出)。

退出码: 0 成功;2 配置或参数错误;3 引用了未知分支。
"""

import argparse
import json
import sys

from splitdns.config import ConfigError, load_config
from splitdns.engine import Engine, UnknownBranchError


def _cmd_generate(args):
    config = load_config(args.config)
    engine = Engine(config)
    out = engine.generate()
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _cmd_query(args):
    config = load_config(args.config)
    engine = Engine(config)
    try:
        result = engine.query(args.branch, args.name)
    except UnknownBranchError as exc:
        print("error: unknown branch %r" % exc.args[0], file=sys.stderr)
        return 3
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="分支机构域名解析分流:规则生成与查询试算")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="按分支生成生效分流规则")
    p_gen.add_argument("--config", required=True, help="配置 JSON 文件路径")
    p_gen.set_defaults(func=_cmd_generate)

    p_qry = sub.add_parser("query", help="试算一次域名查询的去向")
    p_qry.add_argument("--config", required=True, help="配置 JSON 文件路径")
    p_qry.add_argument("--branch", required=True, help="分支 id")
    p_qry.add_argument("--name", required=True, help="查询域名")
    p_qry.set_defaults(func=_cmd_query)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print("error: invalid config: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print("error: cannot load config: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
