"""Retention adjudication module.

Public API:

* Domain objects: :class:`Material`, :class:`PreservationNotice`,
  :class:`DerivedArtifact`, :class:`RetentionStatus`.
* Result objects: :class:`RetentionDecision`, :class:`FieldDecision`,
  :class:`ArtifactDecision`, :class:`ReferenceLink`.
* Engine: :class:`RetentionEngine` and the one-shot helper
  :func:`decide_retention`.
"""

from .engine import RetentionEngine, decide_retention
from .models import (
    ArtifactDecision,
    DerivedArtifact,
    FieldDecision,
    Material,
    PreservationNotice,
    ReferenceLink,
    RetentionDecision,
    RetentionStatus,
)

__all__ = [
    "ArtifactDecision",
    "DerivedArtifact",
    "FieldDecision",
    "Material",
    "PreservationNotice",
    "ReferenceLink",
    "RetentionDecision",
    "RetentionEngine",
    "RetentionStatus",
    "decide_retention",
]
