#!/usr/bin/env python3
"""本地核验入口(仅标准库)。

步骤:
1. 用 unittest.defaultTestLoader.discover("tests") 发现测试套件;
   发现 0 项 -> 非零退出。
2. 实际运行套件;实际执行且未跳过的测试数 < 1、有失败/错误 -> 非零退出。
3. 逐条执行 samples/cases.json 中的交付样例断言,保留每条实际输出;
   任一不符 -> 非零退出。

用法: /usr/local/bin/python3 tools/local_verify.py
"""

import json
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(REPO_ROOT, "samples")

# 让 tests 与 splitdns 都可导入(不依赖调用时的 cwd)。
for path in (REPO_ROOT, os.path.join(REPO_ROOT, "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

_IDNA_TOKEN = re.compile(r"\{idna:([^}]*)\}")


def expand_idna_tokens(name):
    """把 {idna:标签} 展开为该标签的 punycode(标准库 idna 编解码器)。"""
    return _IDNA_TOKEN.sub(
        lambda m: m.group(1).encode("idna").decode("ascii"), name)


def run_unit_tests():
    print("== 步骤 1/2: unittest 发现与执行 ==")
    suite = unittest.defaultTestLoader.discover("tests")
    count = suite.countTestCases()
    print("发现测试用例数: %d" % count)
    if count < 1:
        print("FAIL: 未发现任何测试用例")
        return False
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("实际执行(未跳过)测试数: %d" % executed)
    if executed < 1:
        print("FAIL: 没有实际执行的测试(全部跳过或为空)")
        return False
    if not result.wasSuccessful():
        print("FAIL: 存在失败或错误的测试")
        return False
    print("OK: 单元测试全部通过")
    return True


def compare_case(engine, case):
    """执行单条样例,返回 (ok, actual_view, mismatches)。"""
    branch = case["input"]["branch"]
    name = expand_idna_tokens(case["input"]["name"])
    actual = engine.query(branch, name)
    expected = case["expected"]
    actual_view = {
        "status": actual["status"],
        "normalized": actual["normalized"],
        "matched_suffix": actual["matched_suffix"],
        "rule_source": actual["rule_source"],
        "resolver_id": (actual["resolver"] or {}).get("id"),
        "forwarded_to_public": actual["forwarded_to_public"],
    }
    mismatches = []
    for field, want in expected.items():
        got = actual_view.get(field)
        if got != want:
            mismatches.append("%s: expected=%r actual=%r" % (field, want, got))
    return (not mismatches), actual_view, mismatches


def run_sample_cases():
    print("== 步骤 2/2: 交付样例逐条断言 ==")
    from splitdns.config import load_config
    from splitdns.engine import Engine

    config = load_config(os.path.join(SAMPLES, "config.json"))
    engine = Engine(config)
    with open(os.path.join(SAMPLES, "cases.json"), encoding="utf-8") as fh:
        cases = json.load(fh)["cases"]
    if not cases:
        print("FAIL: 样例文件中没有用例")
        return False
    all_ok = True
    for case in cases:
        ok, actual_view, mismatches = compare_case(engine, case)
        status = "PASS" if ok else "FAIL"
        print("[%s] %s (%s)" % (status, case["case_id"],
                                case.get("comment", "")))
        print("  input:    %s" % json.dumps(case["input"], ensure_ascii=False))
        print("  actual:   %s" % json.dumps(actual_view, ensure_ascii=False))
        print("  expected: %s" % json.dumps(case["expected"],
                                            ensure_ascii=False))
        for line in mismatches:
            print("  mismatch: %s" % line)
        if not ok:
            all_ok = False
    return all_ok


def main():
    os.chdir(REPO_ROOT)
    ok_tests = run_unit_tests()
    ok_samples = run_sample_cases()
    if ok_tests and ok_samples:
        print("RESULT: PASS")
        return 0
    print("RESULT: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
