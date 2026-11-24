"""Compatibility spelling for the probe-placement component."""

from probe_planner import *  # noqa: F403
from probe_planner import __all__ as _package_all
from probe_planner.api import (
    ProbePlacementPlanner,
    plan_placement,
    plan_probes,
    solve,
)

Planner = ProbePlacementPlanner

__all__ = list(
    dict.fromkeys(
        [
            *_package_all,
            "Planner",
            "ProbePlacementPlanner",
            "plan_placement",
            "plan_probes",
            "solve",
        ]
    )
)
