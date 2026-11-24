"""Discovery bootstrap.

``python -I`` (isolated mode) does not put the working directory on
``sys.path``.  Every test module imports this package first so that the
workspace root, where the ``build_semantics`` package lives, is importable
both under ``python -I -m unittest`` and a plain ``python -m unittest`` run.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
