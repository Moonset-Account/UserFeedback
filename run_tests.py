"""验收测试入口。

用法（在工作区根目录）：
    python3 -I run_tests.py

以隔离模式运行，显式把工作区根目录加入 sys.path 后执行 tests/ 下的
unittest 用例，确保测试入口执行真实断言。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
