"""测试引导：在 ``python3 -I`` 隔离模式下也能从工作区根目录导入 planner。

unittest discover 会把 tests/ 目录放入 sys.path，但 -I（隔离模式）
不会把当前工作目录加入路径，因此这里显式补上工作区根目录。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
