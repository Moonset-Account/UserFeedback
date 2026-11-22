"""测试包。

本机 Homebrew Python 3.11.6 在 ``-I``（隔离模式）下不会把当前工作目录
加入 ``sys.path``，导致 ``import topk`` 失败。这里显式把工作区根目录
加入搜索路径，使 ``python3.11 -I -m unittest discover -s tests``
与普通模式都能从根目录发现测试。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
