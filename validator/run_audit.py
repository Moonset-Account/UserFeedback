#!/usr/bin/env python3
"""Isolated-mode entry point.

Run as:
    python3 -I run_audit.py examples/01_parallel_isolation.json

The script lives next to the ``handle_audit`` package so the interpreter
adds its directory to ``sys.path`` even under ``-I`` (PYTHONPATH and cwd are
ignored in isolated mode). Exit codes mirror handle_audit.cli:
    0 PASS, 1 VIOLATION, 2 UNDECIDABLE, 3 input error.
"""

import sys
from pathlib import Path

# Under -I the interpreter does not seed sys.path from the current working
# directory; add this script's own directory explicitly and defensively.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from handle_audit.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
