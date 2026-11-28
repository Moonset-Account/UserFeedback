"""Two-part paper ticket consistency checker."""

from .core import (
    AnalysisResult,
    CertificateItem,
    Check,
    Contradiction,
    EffectiveObservation,
    Erratum,
    ErratumRecord,
    FoldScheme,
    Observation,
    TicketError,
    analyze_ticket,
    count_solutions_by_enumeration,
    enumerate_solutions,
)

__all__ = [
    "AnalysisResult",
    "CertificateItem",
    "Check",
    "Contradiction",
    "EffectiveObservation",
    "Erratum",
    "ErratumRecord",
    "FoldScheme",
    "Observation",
    "TicketError",
    "analyze_ticket",
    "count_solutions_by_enumeration",
    "enumerate_solutions",
]
