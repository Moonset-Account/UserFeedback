#!/usr/bin/env python3
"""本地核验入口：/usr/local/bin/python3 tools/local_verify.py

步骤：
1. 用 unittest.defaultTestLoader.discover("tests") 发现测试套件，
   要求 countTestCases() >= 1，随后实际运行；
2. 要求实际执行且未跳过的测试数 >= 1；发现 0 项、全部跳过、异常或失败均以非零退出；
3. 逐条执行 samples/ 下交付样例的边界断言，打印每条的实际结果与期望值；
4. 仅当以上全部通过时退出 0。
"""

import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)


def run_unittest_suite():
    print("== 步骤 1/2: unittest 套件 ==")
    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print("发现测试用例数: %d" % total)
    if total < 1:
        print("失败: 未发现任何测试用例")
        return False
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    skipped = len(result.skipped)
    actually_ran = result.testsRun - skipped
    print("执行: %d, 跳过: %d, 实际执行未跳过: %d, 失败: %d, 异常: %d"
          % (result.testsRun, skipped, actually_ran,
             len(result.failures), len(result.errors)))
    if actually_ran < 1:
        print("失败: 实际执行且未跳过的测试数为 0")
        return False
    if not result.wasSuccessful():
        print("失败: 存在测试失败或异常")
        return False
    return True


def run_sample_assertions():
    print("== 步骤 2/2: 样例/边界断言 ==")
    from aggregator.loader import load_bundle
    from aggregator.evaluate import evaluate

    samples_dir = os.path.join(REPO_ROOT, "samples")
    case_dirs = sorted(
        name for name in os.listdir(samples_dir)
        if os.path.isdir(os.path.join(samples_dir, name)))
    if not case_dirs:
        print("失败: samples/ 下没有任何样例用例")
        return False

    all_ok = True
    for name in case_dirs:
        case_dir = os.path.join(samples_dir, name)
        with open(os.path.join(case_dir, "expected.json"), encoding="utf-8") as fh:
            expected = json.load(fh)
        report = evaluate(load_bundle(os.path.join(case_dir, "input.json")))
        actual = {(d["component"], d["environment"]): d for d in report["decisions"]}
        for exp in expected["decisions"]:
            key = (exp["component"], exp["environment"])
            decision = actual.get(key)
            if decision is None:
                print("[FAIL] %s %s/%s: 缺少判定输出" % (name, key[0], key[1]))
                all_ok = False
                continue
            checks = [
                ("status", decision["status"], exp["status"]),
                ("missing_count", len(decision["missing"]), exp["missing_count"]),
                ("decommissioned_count",
                 decision["summary"]["decommissioned_instances"],
                 exp["decommissioned_count"]),
                ("new_instance_count",
                 decision["summary"]["new_instances"], exp["new_instance_count"]),
            ]
            for field, got, want in checks:
                ok = got == want
                all_ok = all_ok and ok
                print("[%s] %s %s/%s %s: 实际=%r 期望=%r"
                      % ("PASS" if ok else "FAIL", name, key[0], key[1],
                         field, got, want))
    return all_ok


def main():
    ok_suite = run_unittest_suite()
    ok_samples = run_sample_assertions()
    if ok_suite and ok_samples:
        print("核验通过: 单测套件与样例断言全部成功")
        return 0
    print("核验失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
