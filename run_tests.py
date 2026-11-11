#!/usr/bin/env python3
"""统一测试入口：先后端单元/集成测试，再（可选）Playwright 端到端验证。

用法::

    python3 run_tests.py            # 仅后端测试
    python3 run_tests.py --e2e      # 后端 + 浏览器端到端
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import unittest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--e2e", action="store_true",
                        help="额外运行 Playwright 浏览器端到端验证")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args()

    loader = unittest.TestLoader()
    suite = loader.discover("tests", pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1

    if args.e2e:
        code = subprocess.run(
            [sys.executable, "tests/run_e2e.py", "--port", str(args.port)],
        ).returncode
        if code != 0:
            return code
    print("\n全部测试通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
