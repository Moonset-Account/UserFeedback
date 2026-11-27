"""Test package: make the workspace importable under `python -I` as well."""

import os
import sys

_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE not in sys.path:
    sys.path.insert(0, _WORKSPACE)
