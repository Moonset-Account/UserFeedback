"""Isolated-mode runner for the build semantics CLI.

``python -I`` omits the working directory from ``sys.path``, so the package
cannot be started with ``-m`` under isolation.  This runner inserts the
workspace root itself and then delegates to :mod:`build_semantics.cli`.

Usage:
    python3 -I scripts/run_cli.py analyze examples/compile_race.json
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from build_semantics.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
