"""header_precheck.precheck_header 的回归测试。

每个用例带稳定 case_id，断言结果代码、ok 标志、声明总长度与说明文本，
覆盖需求中的全部五类失败原因、零批次约定、uint32 边界以及“不按声明
长度创建大对象”的约束。
"""

from __future__ import annotations

import io
import json
import time
import unittest
from contextlib import redirect_stdout

import app as cli
from header_precheck import (
    BUDGET_EXCEEDED,
    CONTENT_TRUNCATED,
    FIELD_OUT_OF_RANGE,
    OK,
    TOTAL_LENGTH_UNREPRESENTABLE,
    TRAILING_EXCESS,
    UINT32_MAX,
    precheck_header,
)


def _run(case):
    """按用例字典调用预检并返回结果。"""
    return precheck_header(
        record_count=case["record_count"],
        record_length=case["record_length"],
        header_length=case["header_length"],
        actual_file_size=case["actual_file_size"],
        resource_budget=case["resource_budget"],
    )


# 表驱动用例：case_id 为稳定标识，重跑不应改名。
# 字段顺序：record_count, record_length, header_length,
#           actual_file_size, resource_budget
CASES = [
    # ---- 正常小样例 -----------------------------------------------------
    {
        "case_id": "ok-small-sample",
        "record_count": 10,
        "record_length": 32,
        "header_length": 16,
        "actual_file_size": 336,  # 16 + 10*32
        "resource_budget": 4096,
        "expected_code": OK,
        "expected_total": 336,
    },
    {
        "case_id": "ok-single-record-exact-budget",
        "record_count": 1,
        "record_length": 1,
        "header_length": 0,
        "actual_file_size": 1,
        "resource_budget": 1,  # 预算恰好等于声明总长，允许
        "expected_code": OK,
        "expected_total": 1,
    },
    # ---- 零批次约定 -----------------------------------------------------
    {
        "case_id": "ok-zero-count-zero-payload",
        "record_count": 0,
        "record_length": 0,
        "header_length": 24,
        "actual_file_size": 24,  # 只有头，没有载荷
        "resource_budget": 24,
        "expected_code": OK,
        "expected_total": 24,
    },
    {
        "case_id": "zero-count-nonzero-record-length-rejected",
        "record_count": 0,
        "record_length": 8,  # 条数为零却声明非零记录长度，违反零载荷约定
        "header_length": 24,
        "actual_file_size": 24,
        "resource_budget": 4096,
        "expected_code": FIELD_OUT_OF_RANGE,
        "expected_total": None,
    },
    {
        "case_id": "zero-count-header-only-trailing-excess",
        "record_count": 0,
        "record_length": 0,
        "header_length": 24,
        "actual_file_size": 25,  # 零载荷却多出 1 字节
        "resource_budget": 4096,
        "expected_code": TRAILING_EXCESS,
        "expected_total": 24,
    },
    # ---- uint32 边界 ----------------------------------------------------
    {
        "case_id": "boundary-declared-total-equals-uint32-max",
        "record_count": 2,
        "record_length": 2_000_000_000,
        "header_length": 294_967_295,  # 294967295 + 2*2000000000 = 4294967295
        "actual_file_size": UINT32_MAX,
        "resource_budget": UINT32_MAX,
        "expected_code": OK,
        "expected_total": UINT32_MAX,
    },
    {
        "case_id": "boundary-declared-total-over-uint32-max-by-one",
        "record_count": 2,
        "record_length": 2_000_000_000,
        "header_length": 294_967_296,  # 比上例多 1，总长 = 2**32
        "actual_file_size": UINT32_MAX,
        "resource_budget": UINT32_MAX + 1,
        "expected_code": TOTAL_LENGTH_UNREPRESENTABLE,
        "expected_total": 2**32,
    },
    {
        "case_id": "boundary-record-count-equals-uint32-max",
        "record_count": UINT32_MAX,  # 头字段本身在范围内
        "record_length": 1,
        "header_length": 0,
        "actual_file_size": UINT32_MAX,
        "resource_budget": UINT32_MAX,
        "expected_code": OK,
        "expected_total": UINT32_MAX,
    },
    {
        "case_id": "boundary-record-count-over-uint32-max",
        "record_count": UINT32_MAX + 1,
        "record_length": 1,
        "header_length": 0,
        "actual_file_size": 0,
        "resource_budget": UINT32_MAX,
        "expected_code": FIELD_OUT_OF_RANGE,
        "expected_total": None,
    },
    {
        "case_id": "boundary-record-length-over-uint32-max",
        "record_count": 1,
        "record_length": UINT32_MAX + 1,
        "header_length": 0,
        "actual_file_size": 0,
        "resource_budget": UINT32_MAX,
        "expected_code": FIELD_OUT_OF_RANGE,
        "expected_total": None,
    },
    {
        "case_id": "boundary-header-length-over-uint32-max",
        "record_count": 0,
        "record_length": 0,
        "header_length": UINT32_MAX + 1,
        "actual_file_size": 0,
        "resource_budget": UINT32_MAX,
        "expected_code": FIELD_OUT_OF_RANGE,
        "expected_total": None,
    },
    # ---- 乘加溢出（各字段在范围内，乘积超出） ---------------------------
    {
        "case_id": "multiply-overflow-unrepresentable",
        "record_count": 0xFFFFFFFE,
        "record_length": 0x10,
        "header_length": 0,
        "actual_file_size": 0,
        "resource_budget": 2**64,
        "expected_code": TOTAL_LENGTH_UNREPRESENTABLE,
        "expected_total": 0xFFFFFFFE * 0x10,
    },
    # ---- 截断 / 尾部多余 / 预算 ----------------------------------------
    {
        "case_id": "content-truncated-one-byte",
        "record_count": 4,
        "record_length": 10,
        "header_length": 8,  # 声明总长 48
        "actual_file_size": 47,
        "resource_budget": 4096,
        "expected_code": CONTENT_TRUNCATED,
        "expected_total": 48,
    },
    {
        "case_id": "trailing-excess-one-byte",
        "record_count": 4,
        "record_length": 10,
        "header_length": 8,
        "actual_file_size": 49,
        "resource_budget": 4096,
        "expected_code": TRAILING_EXCESS,
        "expected_total": 48,
    },
    {
        "case_id": "budget-exceeded-by-one",
        "record_count": 4,
        "record_length": 10,
        "header_length": 8,  # 声明总长 48
        "actual_file_size": 48,
        "resource_budget": 47,
        "expected_code": BUDGET_EXCEEDED,
        "expected_total": 48,
    },
    # ---- 检查顺序优先级：返回首个失败原因 -------------------------------
    {
        "case_id": "order-field-before-unrepresentable",
        "record_count": UINT32_MAX + 1,  # 字段越界优先于乘加溢出
        "record_length": UINT32_MAX,
        "header_length": UINT32_MAX,
        "actual_file_size": 0,
        "resource_budget": 0,
        "expected_code": FIELD_OUT_OF_RANGE,
        "expected_total": None,
    },
    {
        "case_id": "order-unrepresentable-before-truncation",
        "record_count": UINT32_MAX,
        "record_length": 2,  # 乘加后超 uint32；实际文件同时也不够
        "header_length": 0,
        "actual_file_size": 0,
        "resource_budget": 2**64,
        "expected_code": TOTAL_LENGTH_UNREPRESENTABLE,
        "expected_total": UINT32_MAX * 2,
    },
    {
        "case_id": "order-truncation-before-budget",
        "record_count": 10,
        "record_length": 10,
        "header_length": 0,  # 总长 100
        "actual_file_size": 50,  # 截断
        "resource_budget": 10,  # 同时预算也不足，截断优先
        "expected_code": CONTENT_TRUNCATED,
        "expected_total": 100,
    },
    {
        "case_id": "order-trailing-before-budget",
        "record_count": 10,
        "record_length": 10,
        "header_length": 0,  # 总长 100
        "actual_file_size": 150,  # 尾部多余
        "resource_budget": 10,  # 同时预算不足，尾部多余优先
        "expected_code": TRAILING_EXCESS,
        "expected_total": 100,
    },
]


class TableDrivenPrecheckTests(unittest.TestCase):
    """对稳定 case_id 逐条断言实际结果与期望。"""

    def test_all_cases(self):
        for case in CASES:
            with self.subTest(case_id=case["case_id"]):
                result = _run(case)
                self.assertEqual(
                    result.code,
                    case["expected_code"],
                    msg=(
                        f"case {case['case_id']}: 期望 {case['expected_code']}，"
                        f"实际 {result.code}（{result.reason}）"
                    ),
                )
                self.assertEqual(result.ok, case["expected_code"] == OK)
                self.assertEqual(
                    result.declared_total_length, case["expected_total"]
                )
                # 说明文本必须非空且包含结果语义。
                self.assertTrue(result.reason)

    def test_failure_codes_are_explicit_and_distinct(self):
        """五类失败原因都至少被一个用例覆盖，且代码互不相同。"""
        failure_codes = {
            FIELD_OUT_OF_RANGE,
            TOTAL_LENGTH_UNREPRESENTABLE,
            CONTENT_TRUNCATED,
            TRAILING_EXCESS,
            BUDGET_EXCEEDED,
        }
        self.assertEqual(len(failure_codes), 5)
        seen = {
            case["expected_code"]
            for case in CASES
            if case["expected_code"] != OK
        }
        self.assertEqual(seen, failure_codes)

    def test_case_ids_are_stable_and_unique(self):
        ids = [case["case_id"] for case in CASES]
        self.assertEqual(len(ids), len(set(ids)))


class InvalidArgumentTests(unittest.TestCase):
    """非整数 / 负数 / bool 等非法类型一律判字段越界。"""

    def test_negative_values_rejected(self):
        for field in (
            "record_count",
            "record_length",
            "header_length",
            "actual_file_size",
            "resource_budget",
        ):
            with self.subTest(field=field):
                kwargs = dict(
                    record_count=1,
                    record_length=1,
                    header_length=1,
                    actual_file_size=2,
                    resource_budget=10,
                )
                kwargs[field] = -1
                result = precheck_header(**kwargs)
                self.assertEqual(result.code, FIELD_OUT_OF_RANGE)
                self.assertIsNone(result.declared_total_length)

    def test_non_integer_types_rejected(self):
        bad_values = ["10", 1.5, None, b"10", object()]
        for bad in bad_values:
            with self.subTest(bad=bad):
                result = precheck_header(
                    record_count=bad,
                    record_length=0,
                    header_length=0,
                    actual_file_size=0,
                    resource_budget=0,
                )
                self.assertEqual(result.code, FIELD_OUT_OF_RANGE)

    def test_bool_is_rejected_even_though_bool_subclasses_int(self):
        # True 表面上 == 1，但作为类型化字段不接受布尔值，避免静默误配。
        result = precheck_header(
            record_count=True,
            record_length=0,
            header_length=0,
            actual_file_size=0,
            resource_budget=0,
        )
        self.assertEqual(result.code, FIELD_OUT_OF_RANGE)


class NoLargeAllocationTests(unittest.TestCase):
    """验证检查过程中不按声明长度创建大对象。"""

    def test_astronomical_declared_length_returns_instantly_without_buffer(self):
        # 两个头字段本身都在 uint32 范围内，但乘积约 1.8e19，
        # 远超任何可分配缓冲区与 uint32 上限。预检必须只做整数运算、
        # 不按声明长度分配内存，并快速返回“无法表示”。
        big = 2_147_483_647  # 2^31-1，单字段合法
        start = time.monotonic()
        result = precheck_header(
            record_count=big,
            record_length=big,
            header_length=0,
            actual_file_size=0,
            resource_budget=10**80,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(result.code, TOTAL_LENGTH_UNREPRESENTABLE)
        self.assertEqual(result.declared_total_length, big * big)
        self.assertLess(elapsed, 1.0)

    def test_big_field_overflow_does_not_compute_payload_buffer(self):
        # 字段本身越界时应在乘加之前短路，declared_total 保持 None。
        result = precheck_header(
            record_count=10**40,
            record_length=1,
            header_length=0,
            actual_file_size=0,
            resource_budget=0,
        )
        self.assertEqual(result.code, FIELD_OUT_OF_RANGE)
        self.assertIsNone(result.declared_total_length)


class CliTests(unittest.TestCase):
    """app.py 命令行入口的退出码与 JSON 输出。"""

    def test_cli_success_exit_zero_and_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(
                [
                    "--record-count", "2",
                    "--record-length", "4",
                    "--header-length", "8",
                    "--actual-file-size", "16",
                    "--resource-budget", "32",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["code"], OK)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["declared_total_length"], 16)
        self.assertEqual(payload["input"]["record_count"], 2)

    def test_cli_rejection_exit_one(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(
                [
                    "--record-count", "2",
                    "--record-length", "4",
                    "--header-length", "8",  # 总长 16
                    "--actual-file-size", "16",
                    "--resource-budget", "15",  # 预算不足
                ]
            )
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["code"], BUDGET_EXCEEDED)
        self.assertFalse(payload["ok"])

    def test_cli_bad_usage_exit_two(self):
        for bad_args in (
            ["--record-count", "-1", "--record-length", "0",
             "--header-length", "0", "--actual-file-size", "0",
             "--resource-budget", "0"],
            ["--record-count", "x", "--record-length", "0",
             "--header-length", "0", "--actual-file-size", "0",
             "--resource-budget", "0"],
        ):
            with self.subTest(bad_args=bad_args):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(bad_args)
                self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
