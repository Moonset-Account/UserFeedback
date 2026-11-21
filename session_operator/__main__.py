"""命令行演示：``python -m session_operator``

只使用本地合成数据，按题面顺序演示合并后再拆分、以及另一台设备不受影响。
"""

from __future__ import annotations

import argparse
from typing import List

from .core import SessionOperator
from .model import Change


def run_demo(verbose: bool = False) -> List[Change]:
    operator = SessionOperator()
    steps = [
        ("insert", "b1", "B", 5.0, 100.0),
        ("insert", "e1", "A", 0.0, 10.0),
        ("insert", "e2", "A", 18.0, 20.0),
        ("insert", "e3", "A", 9.0, 5.0),
        ("retract", "e3"),
    ]
    all_changes: List[Change] = []
    for step in steps:
        changes = operator.apply(step)
        all_changes.extend(changes)
        if verbose:
            label = f"{step[0]} {step[1]}"
            print(f"-> {label}")
            for change in changes:
                print(f"     {change}")
    if verbose:
        print("最终结果：")
        for result in operator.all_published_results():
            print(f"   {result}")
    return all_changes


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按设备聚合会话的增量算子演示")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="只输出汇总行，不打印逐条变更",
    )
    args = parser.parse_args(argv)
    changes = run_demo(verbose=not args.quiet)
    retracts = sum(1 for change in changes if change.is_retract)
    inserts = len(changes) - retracts
    print(f"共产生 {len(changes)} 条变更（撤回 {retracts}，插入 {inserts}）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
