#!/usr/bin/env python3
"""本地核验入口（SEC-20261114-048）。

流程：
1. 用 unittest.defaultTestLoader.discover("tests") 获取测试套件，
   要求 countTestCases() >= 1，随后实际运行；
2. 要求实际执行且未跳过的测试数 >= 1；发现 0 项、全部跳过、异常或
   任何失败均以非零退出；
3. 通过 CLI（app.py）逐条执行已交付样例/边界断言，保留每条实际结果。

退出码：0 = 单测与样例断言全部通过；1 = 存在失败/异常/无有效测试。
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = "/usr/local/bin/python3"
APP = os.path.join(ROOT, "app.py")
SAMPLES = os.path.join(ROOT, "samples")
CONFIG = os.path.join(SAMPLES, "import_config.json")


def run_unit_tests():
    print("== 单元测试 ==")
    os.chdir(ROOT)
    loader = unittest.defaultTestLoader
    suite = loader.discover("tests")
    discovered = suite.countTestCases()
    print("发现测试用例数: %d" % discovered)
    if discovered < 1:
        print("FAIL: 未发现任何测试用例")
        return False
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("执行: %d, 跳过: %d, 失败: %d, 异常: %d"
          % (executed, skipped, len(result.failures), len(result.errors)))
    if executed < 1:
        print("FAIL: 没有实际执行且未跳过的测试")
        return False
    if result.failures or result.errors:
        print("FAIL: 存在失败或异常")
        return False
    return True


def run_cli(xml_name):
    cmd = [PYTHON, APP, os.path.join(SAMPLES, xml_name), "--config", CONFIG]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = None
    return proc.returncode, payload, proc.stdout.strip(), proc.stderr.strip()


def check_case(case_id, description, actual_ok, actual, expected):
    status = "PASS" if actual_ok else "FAIL"
    print("[%s] case_id=%s" % (status, case_id))
    print("  断言: %s" % description)
    print("  期望: %s" % expected)
    print("  实际: %s" % actual)
    return actual_ok


def run_sample_assertions():
    print("== 样例/边界断言（经 app.py CLI 实际执行） ==")
    results = []

    rc_normal, normal, raw_normal, err_normal = run_cli("catalog_normal.xml")
    ok = (rc_normal == 0 and normal is not None
          and normal.get("status") == "ok" and normal.get("record_count") == 2)
    results.append(check_case(
        "SAMPLE-01", "普通目录导入成功并输出 2 条标准化记录",
        ok,
        {"exit": rc_normal, "stdout": raw_normal, "stderr": err_normal},
        "exit=0, status=ok, record_count=2"))

    rc_rep, replaced, raw_rep, err_rep = run_cli("catalog_ns_prefix_replaced.xml")
    same = (normal or {}).get("records") == (replaced or {}).get("records")
    ok = rc_rep == 0 and replaced is not None and same
    results.append(check_case(
        "SAMPLE-02", "替换命名空间前缀的等价目录输出与普通目录完全一致（按 URI 比对）",
        ok,
        {"exit": rc_rep, "records_equal_to_normal": same, "stdout": raw_rep,
         "stderr": err_rep},
        "exit=0, records 与 SAMPLE-01 相同"))

    rc_over, over, raw_over, err_over = run_cli("catalog_over_limit.xml")
    ok = (rc_over == 1 and over is not None
          and over.get("status") == "error"
          and over.get("error", {}).get("category") == "field_length_exceeded"
          and over["error"].get("line") is not None)
    results.append(check_case(
        "SAMPLE-03", "超限目录整份拒绝：分类 field_length_exceeded 且带位置",
        ok,
        {"exit": rc_over, "stdout": raw_over, "stderr": err_over},
        "exit=1, status=error, category=field_length_exceeded, line 非空"))

    return all(results)


def main():
    tests_ok = run_unit_tests()
    samples_ok = run_sample_assertions()
    if tests_ok and samples_ok:
        print("RESULT: PASS（单测与样例断言全部通过）")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
