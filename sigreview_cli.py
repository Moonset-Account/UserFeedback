#!/usr/bin/env python3
"""隔离模式友好的 CLI 入口包装。

用法：python -I sigreview_cli.py <cfg.json>
显式把脚本所在目录加入 sys.path，避免 -I 下 cwd 不在导入路径的问题。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sigreview.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
