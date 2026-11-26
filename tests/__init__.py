"""测试包引导：把仓库根目录加入 sys.path。

常规运行 ``python3 -m unittest discover -s tests`` 时 cwd 已在路径中；
在 ``python3 -I`` 隔离模式下 cwd 不会自动加入，需要显式引导，
以便测试能 import 工作区内的 phrase_code 包。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
