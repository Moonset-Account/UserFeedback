"""测试公共引导：把工作区根目录加入 sys.path，兼容 `python3 -I` 运行。"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
