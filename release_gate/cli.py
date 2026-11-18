"""命令行入口。

用法：

    python -m release_gate evaluate <bundle.json> [--store decisions.json]
    python -m release_gate review <corrected-bundle.json> --store decisions.json

``evaluate`` 退出码：0 表示全部申请放行；1 表示存在被阻止的申请；
2 表示输入文件或参数错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import evaluate_bundle
from .loader import load_bundle_file
from .review import DecisionRepository


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_gate",
        description="临时豁免累计开放额度的发布判定模块",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    eval_cmd = sub.add_parser("evaluate", help="读取整包输入并执行发布判定")
    eval_cmd.add_argument("bundle", help="判定输入 JSON 文件路径")
    eval_cmd.add_argument(
        "--store",
        help="可选：把决定连同输入快照落盘到该 JSON 文件，供事后复核",
    )

    review_cmd = sub.add_parser("review", help="用更正后的输入复核历史决定")
    review_cmd.add_argument("corrected_bundle", help="更正后的判定输入 JSON 文件")
    review_cmd.add_argument(
        "--store",
        required=True,
        help="evaluate 阶段写入的决定记录 JSON 文件",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "evaluate":
            return _do_evaluate(args)
        return _do_review(args)
    except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2


def _do_evaluate(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle)
    bundle = load_bundle_file(bundle_path)
    evaluation = evaluate_bundle(bundle)
    payload = evaluation.to_dict()

    if args.store:
        with open(bundle_path, "r", encoding="utf-8") as handle:
            snapshot = json.load(handle)
        stored = DecisionRepository(args.store).save(evaluation, snapshot)
        payload["stored_record_ids"] = [record.record_id for record in stored]

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if evaluation.all_allowed else 1


def _do_review(args: argparse.Namespace) -> int:
    corrected = load_bundle_file(args.corrected_bundle)
    report = DecisionRepository(args.store).review(corrected)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if not report.has_changes else 1


if __name__ == "__main__":
    raise SystemExit(main())
