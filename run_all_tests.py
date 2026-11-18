"""测试入口：发现并运行 tests/ 下全部 test_*.py，真实执行断言并以退出码反映结果。"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=os.path.join(HERE, "tests"),
        pattern="test_*.py",
        top_level_dir=HERE,
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
