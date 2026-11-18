"""测试运行入口。

支持两种等价方式：

    python3 -I run_tests.py            # 隔离模式，不依赖当前工作目录
    python3 -m unittest -v            # 标准 unittest 发现（tests/ 为包目录）
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=os.path.join(ROOT, "tests"),
        pattern="test_*.py",
        top_level_dir=ROOT,
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
