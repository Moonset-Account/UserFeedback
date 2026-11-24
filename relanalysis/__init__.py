"""Relational schema analysis: lossless-join decision with checkable evidence.

Public API::

    from relanalysis import analyze, verify_evidence

    result = analyze(
        ["A", "B", "C"],
        [("R1", ["A", "B"]), ("R2", ["B", "C"])],
        [("MVD", ("A",), ("B",))],
    )
    result.lossless          # bool
    result.explain()         # multi-line, human-reviewable derivation
    verify_evidence(result.to_dict()).accepted   # independent validation

See ``README.md`` for the model, the chase, and the guarantees.
"""

from .analysis import (
    EVIDENCE_FORMAT,
    Analysis,
    analyze,
    analyze_problem,
    explain_evidence,
)
from .chase import ChaseRun, chase_tableau, content_key
from .errors import (
    DataError,
    DependencyError,
    EvidenceError,
    RelAnalysisError,
    SchemaError,
)
from .evidence import VerificationResult, verify_evidence
from .model import (
    FD,
    MAX_COLUMNS,
    MVD,
    Dependency,
    Instance,
    Schema,
    SubTable,
    dependency_from,
    make_dependencies,
    make_subtables,
)
from .semantics import (
    dependency_violations,
    join_extra,
    join_relation,
    project_tuples,
    sources_for_extra_row,
)
from .tableau import build_seeds, distinguished, is_distinguished

__all__ = [
    "Analysis",
    "ChaseRun",
    "DataError",
    "Dependency",
    "DependencyError",
    "EVIDENCE_FORMAT",
    "EvidenceError",
    "FD",
    "Instance",
    "MAX_COLUMNS",
    "MVD",
    "RelAnalysisError",
    "Schema",
    "SchemaError",
    "SubTable",
    "VerificationResult",
    "analyze",
    "analyze_problem",
    "build_seeds",
    "chase_tableau",
    "content_key",
    "dependency_from",
    "dependency_violations",
    "distinguished",
    "explain_evidence",
    "is_distinguished",
    "join_extra",
    "join_relation",
    "make_dependencies",
    "make_subtables",
    "project_tuples",
    "sources_for_extra_row",
    "verify_evidence",
]

__version__ = "1.0.0"
