#!/usr/bin/env python3
"""隔离模式（python3 -I）下的测试入口。

`-I` 会忽略 PYTHONPATH，因此在这里显式把工作区根目录加入 sys.path，
然后以 unittest discover 方式运行 tests/ 下的全部测试。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
