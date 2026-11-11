#!/usr/bin/env python3
"""Thin launcher so the checker can run under the isolated interpreter:

    python3 -I precheck.py --graph ... --old-rules ... --new-rules ...

Running with -I keeps PYTHONPATH / cwd out of the import system, which matters
when the graph and rule documents come from untrusted download directories.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sanitizer_check.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
