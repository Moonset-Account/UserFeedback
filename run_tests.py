#!/usr/bin/env python3
"""Canonical test entry point.

Runs the ``unittest`` suite in ``tests/`` with the repository root on
``sys.path``, so the command works regardless of the working directory and
regardless of whether ``-I``/``-P`` would otherwise hide the package.

    .venv/bin/python run_tests.py           # run everything
    .venv/bin/python run_tests.py -v        # verbose
    .venv/bin/python run_tests.py test_reject  # one module

Exit status is ``0`` only when every assertion passes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TESTS = ROOT / "tests"


def main(argv: list[str]) -> int:
    for entry in (str(ROOT), str(TESTS)):
        if entry not in sys.path:
            sys.path.insert(0, entry)

    verbosity = 2 if "-v" in argv else 1
    patterns = [item for item in argv if not item.startswith("-")]
    if patterns:
        loader = unittest.TestLoader()
        suite = unittest.TestSuite(
            loader.loadTestsFromName(f"tests.{name}") for name in patterns
        )
    else:
        suite = unittest.TestLoader().discover(
            start_dir=str(TESTS), top_level_dir=str(TESTS)
        )

    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
