#!/usr/bin/env python3
"""本地核验包装器。

1. 用 unittest.defaultTestLoader.discover("tests") 获取测试套件，
   要求 countTestCases() >= 1 并实际运行；
2. 要求实际执行且未跳过的测试数 >= 1；
3. 发现 0 项、全部跳过、异常或任何失败均以非零退出；
4. 随后执行已交付的样例/边界断言（boundary_checks），逐条打印实际结果。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    suite = unittest.defaultTestLoader.discover("tests")
    total = suite.countTestCases()
    print("[verify] 发现测试用例数: %d" % total)
    if total < 1:
        print("[verify] 失败: 未发现任何测试用例")
        return 1

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    skipped = len(result.skipped)
    executed = result.testsRun - skipped
    print("[verify] 执行=%d 跳过=%d 失败=%d 错误=%d"
          % (executed, skipped, len(result.failures), len(result.errors)))
    if executed < 1:
        print("[verify] 失败: 没有实际执行且未跳过的测试")
        return 1
    if result.failures or result.errors:
        print("[verify] 失败: 存在测试失败或错误")
        return 1

    from boundary_checks import run_boundary_checks

    checks = run_boundary_checks()
    bad = 0
    for c in checks:
        status = "OK" if c["ok"] else "FAIL"
        if not c["ok"]:
            bad += 1
        print("[boundary] %s | 实际=%r | 期望=%r | %s"
              % (c["case_id"], c["actual"], c["expected"], status))
    if bad:
        print("[verify] 失败: %d 条样例/边界断言未通过" % bad)
        return 1

    print("[verify] 全部通过（单测 %d 项 + 边界断言 %d 条）" % (executed, len(checks)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
