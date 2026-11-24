"""Convenience top-level entry point.

The implementation lives in the :mod:`probe_planner` package.  This module
allows ``from solution import plan_placement, solve`` in addition to importing
the package directly.
"""

from probe_planner import *  # noqa: F403
from probe_planner import __all__ as _package_all
from probe_planner import __version__
from probe_planner.api import plan_placement, plan_probes, solve

__all__ = list(
    dict.fromkeys(
        [
            *_package_all,
            "plan_placement",
            "plan_probes",
            "solve",
        ]
    )
)
