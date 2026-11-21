"""Left-deep equi-join planner over byte-sized relations.

Public entry points::

    from joinplan import load_query, plan_query, PlannerError

    plan = plan_query(load_query("query.json"))
    print(plan.total_cost)
"""

from __future__ import annotations

from .model import InputError, JoinEdge, Plan, PlanStep, Query, Table
from .parse import MAX_TABLES, loads_query, load_query, parse_query
from .planner import plan_query

#: Errors raised for unusable input (structural or cost-model violations).
PlannerError = InputError

__all__ = [
    "MAX_TABLES",
    "InputError",
    "JoinEdge",
    "Plan",
    "PlanStep",
    "PlannerError",
    "Query",
    "Table",
    "load_query",
    "loads_query",
    "parse_query",
    "plan_query",
]

__version__ = "1.0.0"
