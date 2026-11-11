"""测试包初始化：把项目根目录加入 sys.path。

保证 `python3 -I -m unittest discover -s tests` 也能导入项目包
（-I 会忽略 PYTHONPATH 等环境变量）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
