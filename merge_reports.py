#!/usr/bin/env python3
"""根目录启动器，保证可以用 `python3 -I merge_reports.py ...` 安全运行。

使用 -I（隔离模式）时 PYTHONPATH 不生效，因此在脚本内显式加入 src/。
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC))

from crash_merge.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
