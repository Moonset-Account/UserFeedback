"""隔离模式（python -I）下的测试启动器：显式把工作区加入 sys.path。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
