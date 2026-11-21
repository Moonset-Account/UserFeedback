#!/usr/bin/env python3
"""从工作区根目录运行全部单元测试的便捷入口。

等价于::

    python3 -m unittest discover -s tests -p 'test_*.py'

也兼容隔离模式 ``python3 -I run_tests.py``：本脚本会显式把工作区
根目录加入 ``sys.path``，测试模块自身也有同样的引导。
"""

import sys
import unittest
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent
TESTS_DIR = WORKSPACE_ROOT / "tests"
for path in (str(WORKSPACE_ROOT), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    # tests/ 是无 __init__.py 的顶层测试目录，顶层目录必须与其一致，
    # 否则 discover 会报 “Start directory is not importable”。
    suite = loader.discover(
        start_dir=str(TESTS_DIR),
        pattern="test_*.py",
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
