"""Loop-free next-hop update planner.

Public API
----------
``solve(old, new)``         -> :class:`~flowupdate.solver.Schedule`
``verify_schedule(old, new, rounds)`` -> :class:`~flowupdate.verify.Verification`
``check_round(base, nodes, new)``     -> :class:`~flowupdate.solver.RoundCheck`

See ``README.md`` for the problem statement, the model, and reproduction steps.
"""

from .model import (
    EGRESS,
    ForwardingTable,
    ModelError,
    changed_nodes,
    find_loops,
    load_problem,
    unreachable_nodes,
    validate_table,
)
from .solver import (
    DEFAULT_MAX_CHANGED,
    DEFAULT_MAX_ROUND_SIZE,
    NoScheduleError,
    RoundCheck,
    Schedule,
    check_round,
    round_is_safe,
    solve,
    table_for_state,
)
from .verify import Verification, verify, verify_schedule

__all__ = [
    "EGRESS",
    "ForwardingTable",
    "ModelError",
    "NoScheduleError",
    "RoundCheck",
    "Schedule",
    "Verification",
    "DEFAULT_MAX_CHANGED",
    "DEFAULT_MAX_ROUND_SIZE",
    "changed_nodes",
    "check_round",
    "find_loops",
    "load_problem",
    "round_is_safe",
    "solve",
    "table_for_state",
    "unreachable_nodes",
    "validate_table",
    "verify",
    "verify_schedule",
]

__version__ = "1.0.0"
