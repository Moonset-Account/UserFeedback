"""让 tests 目录在 `python3 -m unittest discover -s tests` 下可导入工作区包。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
