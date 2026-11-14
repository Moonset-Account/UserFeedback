"""Secret reference resolver for one deployment.

The package only ever handles reference identifiers, alias chains and
versions.  It never reads, stores or emits secret material.
"""

from .errors import InputError
from .resolver import build_plan, plan_exit_code

__all__ = ["InputError", "build_plan", "plan_exit_code"]
