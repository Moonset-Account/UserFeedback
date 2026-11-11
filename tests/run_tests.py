"""Test entry point that works under Python's isolated mode (``python -I``).

Isolated mode drops the script directory and all ``PYTHON*`` variables from
``sys.path``; insert the repository root explicitly before importing the
package, then hand control to unittest.
"""

from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=os.path.join(REPO_ROOT, "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
