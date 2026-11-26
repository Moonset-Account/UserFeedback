"""Convenience module for environments that expect a top-level ``solution``.

The implementation lives in the dependency-free :mod:`tenant_guard` package.
"""

from tenant_guard import (
    AnalysisReport,
    Program,
    ProgramValidationError,
    analyze_file,
    analyze_function,
    analyze_json,
    analyze_program,
    call,
    current_tenant,
    lit,
    tup,
    var,
)

check_program = analyze_program
check_function = analyze_function
audit = analyze_program
analyze = analyze_program

__all__ = [
    "AnalysisReport",
    "Program",
    "ProgramValidationError",
    "analyze",
    "analyze_file",
    "analyze_function",
    "analyze_json",
    "analyze_program",
    "audit",
    "call",
    "check_program",
    "current_tenant",
    "lit",
    "tup",
    "var",
]
