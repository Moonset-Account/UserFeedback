#!/usr/bin/env python3
"""Test entry point that works under ``python -I`` (isolated mode).

Under ``-I`` Python does not add the current working directory to
``sys.path``, so the ``predrewrite`` package would not be importable when
unittest loads the test modules. This script inserts the workspace root
explicitly and then runs standard unittest discovery on ``tests/``.

Usage (from the workspace root)::

    python3 -I run_tests.py            # full suite
    python3 -I run_tests.py -v         # verbose
    python3 -I run_tests.py tests.test_expressions
"""

from __future__ import annotations

import glob
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(ROOT, "tests")
for path in (ROOT, TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load_file_based_suite() -> unittest.TestSuite:
    """Discover tests without relying on package-style import semantics."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for path in sorted(glob.glob(os.path.join(TESTS_DIR, "test_*.py"))):
        module_name = os.path.splitext(os.path.basename(path))[0]
        spec = importlib.util.spec_from_file_location(module_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite


def main() -> int:
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        # Allow explicit module names like test_expressions.
        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        for name in sys.argv[1:]:
            suite.addTests(loader.loadTestsFromName(name))
    else:
        suite = _load_file_based_suite()
    runner = unittest.TextTestRunner(verbosity=2 if "-v" in sys.argv else 1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
