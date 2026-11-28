"""Pseudonym registry package.

Public API::

    from pseudonym_registry import PseudonymRegistry, validate_subject_id

    reg = PseudonymRegistry()
    handle = reg.login(partner, institution, subject_id)
    reg.merge(subject_a, subject_b)
    report = reg.split(subject_a)
    reg.explain(partner, handle)
    reg.export_for_partner(partner)
    reg.replay(partner)
"""

from .core import (
    PseudonymRegistry,
    SplitReport,
    validate_subject_id,
)

__all__ = ["PseudonymRegistry", "SplitReport", "validate_subject_id"]
