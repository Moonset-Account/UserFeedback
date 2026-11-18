#!/usr/bin/env python3
"""基线引擎本地入口（支持 ``python -I run_baseline.py`` 隔离运行）。

等价于 ``python -m baseline_engine.cli``，但显式把脚本所在目录加入
``sys.path``，因此在 ``-I`` 隔离模式下也能定位工程内的包。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from baseline_engine.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
