#!/usr/bin/env python3
"""裁决器启动脚本：以 -I 隔离模式等价的干净环境运行 CLI。

直接执行本脚本即可，脚本所在目录会被加入 sys.path 以定位 adjudicator 包；
推荐配合解释器 -I 使用：  python3 -I run_adjudicator.py ...
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from adjudicator.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
