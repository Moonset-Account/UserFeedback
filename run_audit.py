#!/usr/bin/env python3
"""CLI 运行器。

隔离模式（python -I）不会把当前目录加入 sys.path，
因此在仓库根目录通过本脚本注入路径后再调用 compat_audit.cli.main。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from compat_audit.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
