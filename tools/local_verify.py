#!/usr/bin/env python3
"""本地核验入口：运行全部单元测试并执行已交付的样例/边界断言。

用法：
    /usr/local/bin/python3 tools/local_verify.py

行为：
1. 用 unittest.defaultTestLoader.discover("tests") 获取测试套件，
   要求 countTestCases() >= 1，随后实际运行；
2. 要求实际执行且未跳过的测试数 >= 1；
3. 发现 0 项、全部跳过、加载异常或任何失败/错误均以非零退出；
4. 运行 examples/run_demo.py，逐条保留样例/边界断言的实际结果，
   任一 FAIL 或非零退出均判失败。
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def run_unit_tests():
    loader = unittest.defaultTestLoader
    try:
        suite = loader.discover(os.path.join(ROOT, "tests"),
                                top_level_dir=ROOT)
    except Exception as exc:  # 加载异常不能当通过
        print("VERIFY FAIL: test discovery raised: %r" % (exc,))
        return False
    total = suite.countTestCases()
    print("discovered test cases: %d" % total)
    if total < 1:
        print("VERIFY FAIL: zero tests discovered")
        return False

    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    executed = result.testsRun - len(result.skipped)
    print("tests run: %d, skipped: %d, executed: %d, failures: %d, errors: %d"
          % (result.testsRun, len(result.skipped), executed,
             len(result.failures), len(result.errors)))
    if executed < 1:
        print("VERIFY FAIL: zero tests actually executed (all skipped?)")
        return False
    if result.failures or result.errors:
        print("VERIFY FAIL: unit tests failed")
        return False
    return True


def run_demo_assertions():
    demo = os.path.join(ROOT, "examples", "run_demo.py")
    proc = subprocess.run([PY, demo], capture_output=True, text=True,
                          cwd=ROOT, timeout=60)
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stdout.write(proc.stderr)
    case_lines = [line for line in proc.stdout.splitlines()
                  if line.startswith("CASE ")]
    if not case_lines:
        print("VERIFY FAIL: demo produced no CASE assertion lines")
        return False
    failed = [line for line in case_lines if " FAIL " in line]
    for line in case_lines:
        print("demo-assert: %s" % line)
    if proc.returncode != 0 or failed:
        print("VERIFY FAIL: demo assertions failed (%d of %d)"
              % (len(failed), len(case_lines)))
        return False
    print("demo assertions: %d/%d PASS" % (len(case_lines), len(case_lines)))
    return True


def main():
    ok_tests = run_unit_tests()
    ok_demo = run_demo_assertions()
    if ok_tests and ok_demo:
        print("LOCAL VERIFY: PASS")
        return 0
    print("LOCAL VERIFY: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
