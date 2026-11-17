#!/usr/bin/env python3
"""本地核验入口：直接调用本次 capsim 实现与真实断言。

用法：
    python tools/local_verify.py --list
    python tools/local_verify.py --case C01
    python tools/local_verify.py --case C01,C02
    python tools/local_verify.py            # 运行全部用例

退出码：
    0  所选每个用例均有 >=1 条断言且全部通过、无跳过成组；
    1  存在断言失败、入口/用例缺失、未实现、零断言或整组跳过；
    2  参数/调用错误。
输出为 JSON（每组记录 case、断言计数、seed 与期望/观察摘要），
不含凭据、原始敏感样本或私有账户内容。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 以仓库根目录（本文件的上一级）加入 sys.path，保证从任意 CWD 都能导入本次实现
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.verify_cases import CASES, Checker  # noqa: E402


def run_case(case_id: str) -> dict:
    record: dict = {"case": case_id}
    if case_id not in CASES:
        record.update({
            "status": "error",
            "reason": f"未知用例 {case_id}；可用: {', '.join(sorted(CASES))}",
            "assertions": {"passed": 0, "failed": 0, "skipped": 0, "total": 0},
        })
        return record

    title, fn = CASES[case_id]
    chk = Checker()
    record["title"] = title
    try:
        detail = fn(chk)
    except Exception as exc:  # 未实现/运行期错误：非零退出，不伪装成功
        record.update({
            "status": "error",
            "reason": f"用例执行异常: {type(exc).__name__}: {exc}",
            "assertions": {
                "passed": chk.passed,
                "failed": chk.failed + 1,
                "skipped": chk.skipped,
                "total": chk.passed + chk.failed + 1 + chk.skipped,
            },
            "failures": chk.failures,
        })
        return record

    total = chk.passed + chk.failed + chk.skipped
    if total == 0:
        status, reason = "fail", "零断言：该用例没有执行任何真实检查"
    elif chk.failed:
        status, reason = "fail", "存在失败断言"
    elif chk.skipped and chk.passed == 0:
        status, reason = "fail", "整组跳过，未执行有效核验"
    else:
        status, reason = "pass", "全部断言通过"

    record.update({
        "status": status,
        "reason": reason,
        "assertions": {
            "passed": chk.passed,
            "failed": chk.failed,
            "skipped": chk.skipped,
            "total": total,
        },
        "seed": detail.get("seed"),
        "expected": detail.get("expected"),
        "observed": {k: v for k, v in detail.items()
                     if k not in ("seed", "expected")},
    })
    if chk.failures:
        record["failures"] = chk.failures
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="capsim 本地核验（真实断言）")
    parser.add_argument("--list", action="store_true", help="列出可用用例后退出")
    parser.add_argument("--case", help="用例 id，逗号分隔；缺省运行全部")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps(
            {"cases": [{"id": cid, "title": title} for cid, (title, _) in sorted(CASES.items())]},
            ensure_ascii=False, indent=2,
        ))
        return 0

    if args.case:
        ids = [part.strip() for part in args.case.split(",") if part.strip()]
        unknown = [cid for cid in ids if cid not in CASES]
        if unknown:
            print(json.dumps(
                {"status": "error",
                 "reason": f"未知用例 {unknown}；可用: {sorted(CASES)}"},
                ensure_ascii=False, indent=2,
            ))
            return 2
    else:
        ids = sorted(CASES)

    records = [run_case(cid) for cid in ids]
    overall_fail = any(
        r["status"] != "pass" or r["assertions"]["total"] == 0
        or (r["assertions"]["skipped"] and r["assertions"]["passed"] == 0)
        for r in records
    )
    summary = {
        "total_cases": len(records),
        "passed_cases": sum(1 for r in records if r["status"] == "pass"),
        "failed_cases": sum(1 for r in records if r["status"] != "pass"),
        "total_assertions": sum(r["assertions"]["total"] for r in records),
    }
    print(json.dumps(
        {"status": "fail" if overall_fail else "pass", "summary": summary,
         "results": records},
        ensure_ascii=False, indent=2,
    ))
    return 1 if overall_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
