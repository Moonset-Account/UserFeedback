#!/usr/bin/env python3
"""本地核验包装器（仅使用 Python 3 标准库）。

职责：
1. 通过 ``unittest.defaultTestLoader.discover("tests")`` 发现测试套件，
   要求 ``countTestCases() >= 1``，否则按失败处理（防止零测试静默通过）。
2. 实际运行套件，统计真实执行且未跳过的测试数；发现 0 项、全部跳过、
   异常或任何失败均以非零退出。
3. 执行本题自带的样例/边界断言（独立于 unittest 再跑一遍），逐条保留
   稳定 case_id、输入、实际输出与期望值。
4. 将证据写入 verify_report.json，并在标准输出打印摘要。

用法（在工作区根目录）::

    /usr/local/bin/python3 tools/local_verify.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import unittest

# 让脚本在 tools/ 下运行时也能导入工作区根目录的模块。
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, WORKSPACE_ROOT)

from header_precheck import (  # noqa: E402
    BUDGET_EXCEEDED,
    CONTENT_TRUNCATED,
    FIELD_OUT_OF_RANGE,
    OK,
    TOTAL_LENGTH_UNREPRESENTABLE,
    TRAILING_EXCESS,
    UINT32_MAX,
    precheck_header,
)

REPORT_PATH = os.path.join(WORKSPACE_ROOT, "verify_report.json")

# 独立样例/边界断言（与 tests/ 中的回归用例同源，但此处逐条记录输入、
# 实际输出、期望值，作为可重跑的显式证据）。
SAMPLE_ASSERTIONS = [
    {
        "case_id": "sample-ok-small",
        "description": "正常小样例：16 字节头 + 10 条 * 32 字节 = 336",
        "input": dict(record_count=10, record_length=32, header_length=16,
                      actual_file_size=336, resource_budget=4096),
        "expected_code": OK,
        "expected_total": 336,
    },
    {
        "case_id": "sample-zero-batch",
        "description": "零批次：条数 0 且记录长度 0，仅头部 24 字节",
        "input": dict(record_count=0, record_length=0, header_length=24,
                      actual_file_size=24, resource_budget=64),
        "expected_code": OK,
        "expected_total": 24,
    },
    {
        "case_id": "sample-zero-batch-nonzero-record-length",
        "description": "零批次但记录长度非零：字段越界",
        "input": dict(record_count=0, record_length=8, header_length=24,
                      actual_file_size=24, resource_budget=64),
        "expected_code": FIELD_OUT_OF_RANGE,
        "expected_total": None,
    },
    {
        "case_id": "boundary-total-eq-uint32-max",
        "description": "声明总长恰好等于 2^32-1：可表示，通过",
        "input": dict(record_count=2, record_length=2_000_000_000,
                      header_length=294_967_295,
                      actual_file_size=UINT32_MAX,
                      resource_budget=UINT32_MAX),
        "expected_code": OK,
        "expected_total": UINT32_MAX,
    },
    {
        "case_id": "boundary-total-uint32-max-plus-one",
        "description": "声明总长等于 2^32：无法表示",
        "input": dict(record_count=2, record_length=2_000_000_000,
                      header_length=294_967_296,
                      actual_file_size=UINT32_MAX,
                      resource_budget=UINT32_MAX + 1),
        "expected_code": TOTAL_LENGTH_UNREPRESENTABLE,
        "expected_total": 2**32,
    },
    {
        "case_id": "sample-field-overflow",
        "description": "条数字段超过 uint32 上限：字段越界",
        "input": dict(record_count=UINT32_MAX + 1, record_length=1,
                      header_length=0, actual_file_size=0,
                      resource_budget=UINT32_MAX),
        "expected_code": FIELD_OUT_OF_RANGE,
        "expected_total": None,
    },
    {
        "case_id": "sample-truncated",
        "description": "实际文件比声明总长短 1 字节：内容截断",
        "input": dict(record_count=4, record_length=10, header_length=8,
                      actual_file_size=47, resource_budget=4096),
        "expected_code": CONTENT_TRUNCATED,
        "expected_total": 48,
    },
    {
        "case_id": "sample-trailing-excess",
        "description": "实际文件比声明总长长 1 字节：尾部多余",
        "input": dict(record_count=4, record_length=10, header_length=8,
                      actual_file_size=49, resource_budget=4096),
        "expected_code": TRAILING_EXCESS,
        "expected_total": 48,
    },
    {
        "case_id": "sample-budget-exceeded",
        "description": "声明总长 48 超过预算 47：预算不足",
        "input": dict(record_count=4, record_length=10, header_length=8,
                      actual_file_size=48, resource_budget=47),
        "expected_code": BUDGET_EXCEEDED,
        "expected_total": 48,
    },
]


def run_unittest_suite() -> dict:
    """发现并运行 tests/ 下的 unittest 套件，返回统计证据。"""
    loader = unittest.defaultTestLoader
    suite = loader.discover(start_dir="tests", top_level_dir=WORKSPACE_ROOT)

    discovered = suite.countTestCases()
    if discovered < 1:
        return {
            "status": "failed",
            "reason": "测试发现数为 0，拒绝把零测试当通过",
            "discovered": 0,
            "executed": 0,
            "skipped": 0,
            "failures": 0,
            "errors": 0,
            "output": "",
        }

    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2)
    result = runner.run(suite)

    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    failures = len(result.failures)
    errors = len(result.errors)

    status = "passed"
    reason = None
    if executed < 1:
        status = "failed"
        reason = "没有实际执行且未跳过的测试"
    elif failures or errors:
        status = "failed"
        reason = f"{failures} 个失败，{errors} 个错误"

    return {
        "status": status,
        "reason": reason,
        "discovered": discovered,
        "tests_run": result.testsRun,
        "executed": executed,
        "skipped": skipped,
        "failures": failures,
        "errors": errors,
        "output": stream.getvalue(),
    }


def run_sample_assertions() -> dict:
    """逐条执行样例/边界断言并保留实际结果。"""
    records = []
    all_passed = True
    for item in SAMPLE_ASSERTIONS:
        result = precheck_header(**item["input"])
        actual = {
            "code": result.code,
            "ok": result.ok,
            "declared_total_length": result.declared_total_length,
            "reason": result.reason,
        }
        passed = (
            result.code == item["expected_code"]
            and result.declared_total_length == item["expected_total"]
        )
        all_passed = all_passed and passed
        records.append(
            {
                "case_id": item["case_id"],
                "description": item["description"],
                "input": item["input"],
                "expected": {
                    "code": item["expected_code"],
                    "declared_total_length": item["expected_total"],
                },
                "actual": actual,
                "passed": passed,
            }
        )
    return {
        "status": "passed" if all_passed else "failed",
        "total": len(records),
        "passed": sum(1 for r in records if r["passed"]),
        "failed": sum(1 for r in records if not r["passed"]),
        "records": records,
    }


def main() -> int:
    os.chdir(WORKSPACE_ROOT)

    unit_evidence = run_unittest_suite()
    sample_evidence = run_sample_assertions()

    overall_ok = (
        unit_evidence["status"] == "passed"
        and sample_evidence["status"] == "passed"
    )
    report = {
        "overall_status": "passed" if overall_ok else "failed",
        "python": sys.version,
        "workspace": WORKSPACE_ROOT,
        "unittest": unit_evidence,
        "sample_assertions": sample_evidence,
    }

    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    print("=== unittest 套件 ===")
    print(unit_evidence["output"])
    print(
        f"发现 {unit_evidence.get('discovered', 0)} 项，"
        f"实际执行 {unit_evidence.get('executed', 0)} 项，"
        f"跳过 {unit_evidence.get('skipped', 0)} 项，"
        f"失败 {unit_evidence.get('failures', 0)}，"
        f"错误 {unit_evidence.get('errors', 0)}"
    )
    print("=== 样例/边界断言 ===")
    for record in sample_evidence["records"]:
        mark = "PASS" if record["passed"] else "FAIL"
        print(
            f"[{mark}] {record['case_id']}: "
            f"期望 {record['expected']['code']} / "
            f"实际 {record['actual']['code']}"
        )
    print(
        f"样例断言 {sample_evidence['passed']}/{sample_evidence['total']} 通过"
    )
    print(f"总体结论: {report['overall_status'].upper()}")
    print(f"报告已写入: {REPORT_PATH}")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
