"""测试包：在隔离模式（python -I -m unittest）下保证可导入工作区根包。"""

import os
import sys

_WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)
