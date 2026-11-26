"""Safe dump planner for shared compute-node crash pages.

The public entry point is :func:`plan_safe_dump`; :func:`materialize_dump`
copies and redacts the selected synthetic bytes.
"""

from .dump import DumpSegment, SafeDumpArtifact, materialize_dump
from .errors import DumpPlannerError, UnsafeOutputError, ValidationError
from .geometry import ByteRange
from .model import (
    Crop,
    DiagnosticTarget,
    DomainPolicy,
    DumpRequest,
    FieldSpec,
    FragmentNeed,
    PageSpec,
    PointerRef,
    PolicyRule,
    RecordSpec,
    VirtualAlias,
)
from .planner import Planner, plan_dump, plan_safe_dump
from .report import (
    DumpPlan,
    Fragment,
    PagePlan,
    PointerDecision,
    Reason,
    TargetDecision,
)

__all__ = [
    "ByteRange",
    "Crop",
    "DiagnosticTarget",
    "DomainPolicy",
    "DumpPlan",
    "DumpPlannerError",
    "DumpRequest",
    "DumpSegment",
    "FieldSpec",
    "Fragment",
    "FragmentNeed",
    "PagePlan",
    "PageSpec",
    "Planner",
    "PointerDecision",
    "PointerRef",
    "PolicyRule",
    "Reason",
    "RecordSpec",
    "SafeDumpArtifact",
    "TargetDecision",
    "UnsafeOutputError",
    "ValidationError",
    "VirtualAlias",
    "materialize_dump",
    "plan_dump",
    "plan_safe_dump",
]

__version__ = "1.0.0"
