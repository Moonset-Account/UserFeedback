#!/usr/bin/env python3
"""release_gate 命令行入口（支持解释器隔离模式 ``python3 -I``）。

等价于 ``python -m release_gate``，但在 ``-I`` 模式下也能定位到同目录的
release_gate 包：

    python3 -I release_gate_cli.py evaluate examples/bundle.json --store decisions.json
    python3 -I release_gate_cli.py review  corrected.json --store decisions.json
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release_gate.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
