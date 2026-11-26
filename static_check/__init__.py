"""Static disclosure checking for restricted job-agent error flows.

The canonical entry point is :func:`audit_flow`.  It accepts a JSON-like graph,
declared diagnostic/code grants, and a caller identity, then reports every
reachable response exit and a safe projection of its response.
"""

from .analysis import FlowAnalyzer
from .api import (
    analyze_flow,
    audit_error_flow,
    audit_flow,
    check,
    check_error_flow,
    check_flow,
    filter_diagnostics_for_identity,
    graph_from_dict,
    reverse_identity_filter,
    reverse_scan,
    run_check,
)
from .model import (
    AuditReport,
    DiagnosticReport,
    DiagnosticUse,
    ErrorReport,
    ErrorValue,
    ExitReport,
    FieldReport,
    Finding,
    ResponseScenario,
    ReverseSlice,
)
from .policy import CallerIdentity, DisclosurePolicy, Grant

__all__ = [
    "FlowAnalyzer",
    "DisclosurePolicy",
    "CallerIdentity",
    "Grant",
    "audit_flow",
    "check",
    "check_flow",
    "analyze_flow",
    "run_check",
    "audit_error_flow",
    "check_error_flow",
    "reverse_scan",
    "reverse_identity_filter",
    "filter_diagnostics_for_identity",
    "graph_from_dict",
    "AuditReport",
    "ExitReport",
    "ResponseScenario",
    "FieldReport",
    "ErrorReport",
    "ErrorValue",
    "DiagnosticReport",
    "DiagnosticUse",
    "Finding",
    "ReverseSlice",
]

__version__ = "1.0.0"
