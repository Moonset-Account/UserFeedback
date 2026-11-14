#!/usr/bin/env python3
"""本地核验包装器（仅使用 Python 3 标准库）。

职责：
1. 通过 unittest.defaultTestLoader.discover("tests") 发现测试套件，
   并检查 countTestCases() >= 1；
2. 实际运行套件，统计真实执行且未跳过的测试数，全部跳过/异常/失败均非零退出；
3. 逐条执行本题交付的样例/边界断言，保留每条 case_id 的输入摘要、实际输出与期望值。

任何一步失败、零测试、全部跳过或异常，进程均以非零码退出。
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vuln_ledger.engine import evaluate  # noqa: E402
from vuln_ledger.models import InputError  # noqa: E402
from vuln_ledger.parser import parse_input  # noqa: E402
from tests.scenarios import ERROR_SCENARIOS, SCENARIOS  # noqa: E402


def run_unittest_suite() -> tuple[bool, dict]:
    loader = unittest.defaultTestLoader
    suite = loader.discover("tests")
    discovered = suite.countTestCases()
    print(f"[1/2] unittest 发现测试用例数: {discovered}")
    if discovered < 1:
        print("FAIL: 未发现任何测试用例", file=sys.stderr)
        return False, {"discovered": 0}

    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(suite)
    print(stream.getvalue())

    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    summary = {
        "discovered": discovered,
        "tests_run": result.testsRun,
        "skipped": skipped,
        "executed_non_skipped": executed,
        "failures": len(result.failures),
        "errors": len(result.errors),
    }
    print(f"[1/2] unittest 统计: {json.dumps(summary, ensure_ascii=False)}")

    ok = True
    if executed < 1:
        print("FAIL: 没有实际执行且未跳过的测试", file=sys.stderr)
        ok = False
    if result.failures or result.errors:
        ok = False
    return ok, summary


def run_sample_assertions() -> tuple[bool, list[dict]]:
    print("[2/2] 样例/边界断言逐条核验:")
    records: list[dict] = []
    all_ok = True

    for scenario in SCENARIOS:
        record: dict = {
            "case_id": scenario["case_id"],
            "description": scenario["description"],
            "checks": [],
        }
        try:
            ledger = parse_input(scenario["input"])
            result = evaluate(ledger)
            deps = {d["build_id"]: d for d in result["deployments"]}
            for build_id, expected in scenario["expected"].items():
                dep = deps[build_id]
                actual_status = dep["status"]
                actual_codes = sorted(m["code"] for m in dep["missing_records"])
                actual_fix = {fid: info["status"] for fid, info in dep["fix_statuses"].items()}
                check = {
                    "build_id": build_id,
                    "input_line": dep["line"],
                    "input_source_commit": dep["source_commit"],
                    "actual": {
                        "status": actual_status,
                        "fix_statuses": actual_fix,
                        "missing_codes": actual_codes,
                    },
                    "expected": expected,
                    "passed": (
                        actual_status == expected["status"]
                        and actual_codes == sorted(expected["missing_codes"])
                        and actual_fix == expected["fix"]
                    ),
                }
                record["checks"].append(check)
                all_ok = all_ok and check["passed"]
        except Exception as exc:  # noqa: BLE001 - 核验器必须捕获并如实记录
            record["fatal"] = f"{type(exc).__name__}: {exc}"
            all_ok = False
        records.append(record)
        status_text = "PASS" if all(c.get("passed") for c in record["checks"]) and "fatal" not in record else "FAIL"
        print(f"  {status_text} {record['case_id']} ({len(record['checks'])} 条断言)")
        for check in record["checks"]:
            print(
                f"    - {check['build_id']}: actual={json.dumps(check['actual'], ensure_ascii=False)}"
            )
            print(
                f"      expected={json.dumps(check['expected'], ensure_ascii=False)}"
            )

    for scenario in ERROR_SCENARIOS:
        record = {
            "case_id": scenario["case_id"],
            "description": scenario["description"],
            "checks": [],
        }
        try:
            parse_input(scenario["input"])
            passed = False
            actual = "未抛出 InputError"
        except InputError as exc:
            passed = scenario["error_contains"] in str(exc)
            actual = f"InputError: {exc}"
        check = {
            "build_id": None,
            "actual": actual,
            "expected": f"InputError 包含 '{scenario['error_contains']}'",
            "passed": passed,
        }
        record["checks"].append(check)
        all_ok = all_ok and passed
        records.append(record)
        print(f"  {'PASS' if passed else 'FAIL'} {record['case_id']}: {actual}")

    return all_ok, records


def main() -> int:
    print(f"工作目录: {ROOT}")
    suite_ok, suite_summary = run_unittest_suite()
    samples_ok, records = run_sample_assertions()

    total_checks = sum(len(r.get("checks", [])) for r in records)
    passed_checks = sum(
        1 for r in records for c in r.get("checks", []) if c.get("passed")
    )
    print(
        f"样例断言汇总: {passed_checks}/{total_checks} 通过，"
        f"场景数 {len(records)}"
    )

    if suite_ok and samples_ok:
        print("LOCAL_VERIFY: PASS")
        return 0
    print("LOCAL_VERIFY: FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
