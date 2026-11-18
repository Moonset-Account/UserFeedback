#!/usr/bin/env python3
"""测试运行器。

隔离模式（python -I）不会把当前目录加入 sys.path，
本脚本先注入工作区根路径，再以 unittest 默认 TestRunner 执行全部验收用例。

用法::

    python3 -I run_tests.py            # 隔离模式（推荐，防止夹具目录模块污染）
    python3 run_tests.py               # 普通模式
"""

import os
import sys
import unittest

WORKSPACE_ROOT = os.path.dirname(os.path.abspath(__file__))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=os.path.join(WORKSPACE_ROOT, "tests"),
        top_level_dir=WORKSPACE_ROOT,
    )
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
