"""测试发现引导：把工作区根目录加入 sys.path，保证 ``import quantile_merger``
在 ``python -m unittest discover`` 下可用（仅使用标准库）。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
