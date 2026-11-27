"""Electronic-credential presentation planner.

Public API::

    from wallet_planner import plan_presentation, require_plan, plan_from_dict

:func:`plan_presentation` accepts either a validated :class:`WalletSpec` or the
raw JSON-shaped mapping and returns a :class:`Plan`.
"""

from .model import (
    MAX_DISCLOSURE_GROUPS,
    Alternative,
    Condition,
    Credential,
    DisclosureGroup,
    SpecError,
    WalletSpec,
    parse_spec,
)
from .planner import (
    ConditionProvenance,
    GroupSelection,
    Plan,
    UnsatisfiableError,
    natural_key,
    plan_from_dict,
    plan_presentation,
    require_plan,
)

__version__ = "1.0.0"

__all__ = [
    "Alternative",
    "Condition",
    "ConditionProvenance",
    "Credential",
    "DisclosureGroup",
    "GroupSelection",
    "MAX_DISCLOSURE_GROUPS",
    "Plan",
    "SpecError",
    "UnsatisfiableError",
    "WalletSpec",
    "natural_key",
    "parse_spec",
    "plan_from_dict",
    "plan_presentation",
    "require_plan",
]
