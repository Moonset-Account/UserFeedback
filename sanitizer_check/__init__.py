"""Pre-release checker for sanitizer applicability rule updates.

The package compares two versions of a static-analysis rule table against the
same local data-flow graph and classifies every watched source -> sink path.
"""

from .errors import AnalysisError, LoaderError
from .model import Graph, PathState, RuleTable
from .analysis import Engine
from .verdict import classify, evaluate_sink
from .report import build_report

__all__ = [
    "AnalysisError",
    "LoaderError",
    "Graph",
    "PathState",
    "RuleTable",
    "Engine",
    "classify",
    "evaluate_sink",
    "build_report",
]
