#!/usr/bin/env python3
"""SEC-VULN-006 本地核验入口（离线，仅标准库）。

执行内容：
  1. 用 unittest.defaultTestLoader.discover("tests") 发现测试套件，
     要求 countTestCases() >= 1，随后实际运行；
     要求实际执行且未跳过的测试数 >= 1，且零失败零错误。
  2. 执行交付样例与边界断言（通过 CLI 子进程与核心函数真实运行），
     逐条打印 断言内容 / 期望 / 实际 / 结论。

发现 0 项测试、全部跳过、运行异常、任何失败或断言不通过，均以非零退出。
"""

import json
import os
import subprocess
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

ASSERTION_RESULTS = []


def record_assertion(name, expected, actual, ok):
    ASSERTION_RESULTS.append(ok)
    status = "PASS" if ok else "FAIL"
    print("[assert] %s\n         expected: %r\n         actual:   %r\n         result:   %s"
          % (name, expected, actual, status))


def run_unittest_suite():
    os.chdir(BASE_DIR)
    suite = unittest.defaultTestLoader.discover("tests")
    discovered = suite.countTestCases()
    print("[unittest] discovered test cases: %d" % discovered)
    if discovered < 1:
        print("[unittest] FAIL: discovered 0 test cases")
        return False
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("[unittest] testsRun=%d skipped=%d executed=%d failures=%d errors=%d"
          % (result.testsRun, skipped, executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("[unittest] FAIL: 0 tests actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("[unittest] FAIL: failures or errors present")
        return False
    return True


def run_cli(input_path, rules_path):
    proc = subprocess.run(
        [sys.executable, os.path.join(BASE_DIR, "app.py"),
         "--input", input_path, "--rules", rules_path],
        capture_output=True, text=True, cwd=BASE_DIR, timeout=60,
    )
    return proc


def run_sample_assertions():
    records_path = os.path.join(BASE_DIR, "samples", "records.json")
    rules_path = os.path.join(BASE_DIR, "samples", "rules.json")

    expected_report = [
        {"record_no": "r-002", "rule_versions": ["sig-1.2", "loc-2.0"],
         "divergent_fields": ["version"]},
        {"record_no": "r-005", "rule_versions": ["sig-1.2", "loc-2.0"],
         "divergent_fields": ["version"]},
        {"record_no": "r-007", "rule_versions": ["sig-1.2", "loc-2.0"],
         "divergent_fields": ["tenant"]},
    ]

    proc = run_cli(records_path, rules_path)
    actual_report = None
    parse_error = None
    try:
        actual_report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        parse_error = str(exc)
    record_assertion(
        "CLI 对交付样例输出分歧报告（记录编号/规则版本/分歧字段）",
        expected_report,
        actual_report if parse_error is None else "stdout 非 JSON: %s" % parse_error,
        parse_error is None and actual_report == expected_report,
    )
    record_assertion(
        "CLI 在存在分歧时退出码为 1",
        1, proc.returncode, proc.returncode == 1,
    )

    # 边界断言 1：全部通过的输入 -> 空报告且退出码 0（交付夹具）。
    from core import check_records, ruleset_from_dict
    with open(records_path, encoding="utf-8") as fh:
        all_records = json.load(fh)
    with open(rules_path, encoding="utf-8") as fh:
        conf = json.load(fh)
    rules_a = ruleset_from_dict(conf["rules_a"])
    rules_b = ruleset_from_dict(conf["rules_b"])
    passing_path = os.path.join(BASE_DIR, "samples", "records_passing.json")
    proc_ok = run_cli(passing_path, rules_path)
    ok_stdout = None
    ok_parse_error = None
    try:
        ok_stdout = json.loads(proc_ok.stdout)
    except json.JSONDecodeError as exc:
        ok_parse_error = "stdout 非 JSON: %s; stderr=%s" % (exc, proc_ok.stderr.strip())
    record_assertion(
        "CLI 对无分歧输入输出空报告且退出码为 0",
        (0, []),
        (proc_ok.returncode, ok_stdout if ok_parse_error is None else ok_parse_error),
        ok_parse_error is None and proc_ok.returncode == 0 and ok_stdout == [],
    )

    # 边界断言 2：输入顺序变化不改变报告。
    shuffled = list(reversed(all_records))
    report_forward = check_records(all_records, rules_a, rules_b)["report"]
    report_shuffled = check_records(shuffled, rules_a, rules_b)["report"]
    record_assertion(
        "输入顺序变化后报告保持一致（按记录编号排序）",
        report_forward, report_shuffled, report_forward == report_shuffled,
    )

    # 边界断言 3：重复版本字段取值相同，即使 first/last 策略不同也不分歧。
    dup_same = [{"record_no": "b-1", "tenant": "tenant-a", "object_no": "OBJ-9",
                 "version": ["v1", "v1"], "query": {}}]
    report_dup = check_records(dup_same, rules_a, rules_b)["report"]
    record_assertion(
        "重复版本字段值相同（first/last 策略下）不产生分歧",
        [], report_dup, report_dup == [],
    )

    # 边界断言 4：可忽略追踪字段（trace_id / utm_*）不参与比对。
    tracked = [{"record_no": "b-2", "tenant": "tenant-a", "object_no": "OBJ-9",
                "version": "v1",
                "query": {"trace_id": "t-1", "utm_source": "mail", "keep": "x"}}]
    report_tracked = check_records(tracked, rules_a, rules_b)["report"]
    record_assertion(
        "仅含可忽略追踪字段差异的记录通过一致性检查",
        [], report_tracked, report_tracked == [],
    )

    # 边界断言 5：同编号跨租户记录各自独立判定且均通过。
    cross = [
        {"record_no": "b-3a", "tenant": "tenant-a", "object_no": "OBJ-X",
         "version": "v1", "query": {}},
        {"record_no": "b-3b", "tenant": "tenant-b", "object_no": "OBJ-X",
         "version": "v1", "query": {}},
    ]
    report_cross = check_records(cross, rules_a, rules_b)["report"]
    record_assertion(
        "同编号跨租户记录独立判定且通过",
        [], report_cross, report_cross == [],
    )

    return all(ASSERTION_RESULTS)


def main():
    print("=== SEC-VULN-006 local_verify ===")
    ok_tests = run_unittest_suite()
    ok_assertions = run_sample_assertions()
    if ok_tests and ok_assertions:
        print("=== RESULT: PASS ===")
        return 0
    print("=== RESULT: FAIL (tests_ok=%s assertions_ok=%s) ==="
          % (ok_tests, ok_assertions))
    return 1


if __name__ == "__main__":
    sys.exit(main())
