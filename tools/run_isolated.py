"""隔离模式运行入口：python3 -I tools/run_isolated.py [tests|demo]。

`-I` 会忽略 PYTHONPATH 与 cwd，因此在脚本内显式把工作区根目录
加入 sys.path，保证不依赖任何环境变量或站点包。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "tests"
    if target == "tests":
        import unittest
        loader = unittest.TestLoader()
        suite = loader.discover(os.path.join(ROOT, "tests"))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        sys.exit(0 if result.wasSuccessful() else 1)
    elif target == "demo":
        from demo.run_demo import main
        main()
    else:
        print(f"未知目标: {target}（可选 tests / demo）")
        sys.exit(2)
