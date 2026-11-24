"""让测试在 `python3 -I -m unittest discover` 下也能导入工作区根目录的 safedrain。"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
