"""Wasm plugin host-capability analysis.

Reads a builder-exported JSON summary of a WebAssembly plugin and
computes, per exported entry, which host capabilities may be exercised,
with evidence paths and exclusion explanations.
"""
from .model import Summary, SummaryError, load_summary, parse_summary
from .analyze import analyze, analyze_entry, type_compatible

__version__ = "0.1.0"

__all__ = [
    "Summary",
    "SummaryError",
    "load_summary",
    "parse_summary",
    "analyze",
    "analyze_entry",
    "type_compatible",
    "__version__",
]
