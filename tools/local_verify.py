#!/usr/bin/env python3
"""本地核验包装器。

1) 用 unittest.defaultTestLoader.discover("tests") 发现测试套件，
   要求 countTestCases() >= 1，随后实际运行；
   要求实际执行且未跳过的测试数 >= 1；
   发现 0 项、全部跳过、异常或任何失败 => 非零退出。
2) 运行已交付的样例/边界断言（app.py + samples/scenario.json），
   逐条打印实际结果与期望值，任何不符 => 非零退出。

用法：/usr/local/bin/python3 tools/local_verify.py
"""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_unit_tests() -> bool:
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("== unit tests ==")
    print("discovered test cases: %d" % count)
    if count < 1:
        print("FAIL: discovered 0 test cases")
        return False
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("testsRun=%d skipped=%d executed_non_skipped=%d"
          % (result.testsRun, skipped, executed))
    if executed < 1:
        print("FAIL: 0 tests actually executed (all skipped?)")
        return False
    if not result.wasSuccessful():
        print("FAIL: unit tests had failures/errors")
        return False
    print("unit tests: OK")
    return True


# 样例断言：对 samples/scenario.json 的每个 paste 步骤给出期望输出。
SAMPLE_CASES = [
    {"case_id": "SAMPLE-01", "step": 3, "action": "paste",
     "reason": "user-confirmed", "delivered": "Q3 roadmap (confidential)"},
    {"case_id": "SAMPLE-02", "step": 6, "action": "cancel",
     "reason": "user-declined", "delivered": None},
    {"case_id": "SAMPLE-03", "step": 9, "action": "paste_text",
     "reason": "internal-text-downgraded", "delivered": "hello"},
    {"case_id": "SAMPLE-04", "step": 12, "action": "paste",
     "reason": "user-confirmed", "delivered": "<png-bytes>"},
    {"case_id": "SAMPLE-05", "step": 14, "action": "block",
     "reason": "secret-blocked", "delivered": None},
]

# 边界断言：审计输出中不得出现的剪贴板内容片段。
FORBIDDEN_IN_AUDIT = ["Q3 roadmap", "<png-bytes>", "root-credentials", "<b>hello</b>"]


def run_sample_assertions() -> bool:
    print("== sample/boundary assertions ==")
    scenario = os.path.join(ROOT, "samples", "scenario.json")
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "app.py"), scenario],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        print("FAIL: app.py exited %d\n%s" % (proc.returncode, proc.stderr))
        return False
    try:
        output = json.loads(proc.stdout)
    except ValueError as exc:
        print("FAIL: app.py output is not JSON: %s" % exc)
        return False

    ok = True
    results_by_step = {r["step"]: r for r in output["results"]}
    for case in SAMPLE_CASES:
        actual = results_by_step.get(case["step"])
        if actual is None:
            print("%s: FAIL (no result for step %d)" % (case["case_id"], case["step"]))
            ok = False
            continue
        a = (actual["action"], actual["reason"], actual["delivered_payload"])
        e = (case["action"], case["reason"], case["delivered"])
        status = "PASS" if a == e else "FAIL"
        if a != e:
            ok = False
        print("%s: %s step=%d actual=%r expected=%r"
              % (case["case_id"], status, case["step"], a, e))

    # 边界断言：审计记录只引用版本与分类，不含剪贴板内容
    audit_dump = json.dumps(output["audit"], ensure_ascii=False)
    for token in FORBIDDEN_IN_AUDIT:
        status = "PASS" if token not in audit_dump else "FAIL"
        if token in audit_dump:
            ok = False
        print("BOUNDARY audit-excludes %r: %s" % (token, status))
    # 边界断言：审计记录确实引用了版本与分类字段
    has_fields = all(
        "classification" in rec and "clipboard_version" in rec
        for rec in output["audit"]) and len(output["audit"]) > 0
    print("BOUNDARY audit-has-version-and-classification: %s"
          % ("PASS" if has_fields else "FAIL"))
    if not has_fields:
        ok = False
    return ok


def main() -> int:
    ok_tests = run_unit_tests()
    ok_samples = run_sample_assertions()
    if ok_tests and ok_samples:
        print("LOCAL VERIFY: PASS")
        return 0
    print("LOCAL VERIFY: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
