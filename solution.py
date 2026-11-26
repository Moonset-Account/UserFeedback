"""Convenience top-level entry point for the dictionary reduction task.

The implementation lives in the :mod:`dictionary_reduction` package.  This
module re-exports its public API so callers can use either
``from dictionary_reduction import ...`` or ``from solution import ...``.
"""

from dictionary_reduction import *  # noqa: F403
from dictionary_reduction import __version__  # noqa: F401
