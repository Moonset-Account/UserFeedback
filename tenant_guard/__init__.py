"""Tenant-dimension provenance checker for shared caches."""

from .api import (
    AnalysisReport,
    analyze_dict,
    analyze_file,
    analyze_function,
    analyze_json,
    analyze_program,
    audit,
    check_function,
    check_program,
)
from .program import (
    Program,
    ProgramValidationError,
    alias,
    call,
    current_tenant,
    lit,
    load_program,
    member,
    tup,
    unknown,
    var,
)

__all__ = [
    "AnalysisReport",
    "Program",
    "ProgramValidationError",
    "analyze_dict",
    "analyze_file",
    "analyze_function",
    "analyze_json",
    "analyze_program",
    "audit",
    "check_function",
    "check_program",
    "alias",
    "call",
    "current_tenant",
    "lit",
    "load_program",
    "member",
    "tup",
    "unknown",
    "var",
]

__version__ = "0.1.0"
