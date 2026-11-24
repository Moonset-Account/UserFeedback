"""Disclosure tracing and approval review for cited investigative text.

The public entry points are:

* :class:`disclosure_review.ReviewEngine` and :func:`review_case` for typed use;
* :func:`disclosure_review.review_dict` for JSON/dictionary input;
* the model/dataclass exports for constructing synthetic registries.
"""

from .api import case_from_dict, review_dict, review_payload
from .engine import ReviewEngine, review_case
from .hashing import fingerprint_paragraphs, fingerprint_text
from .models import (
    Approval,
    CarryForward,
    Citation,
    NodeRef,
    RecordRef,
    ReviewCase,
    ReviewPolicy,
    Submission,
    VersionedRecord,
)
from .registry import MaterialRegistry
from .reporting import (
    Finding,
    ParagraphResult,
    Remediation,
    ReviewReport,
    Segment,
)
from .ranges import Range, normalize_ranges

__all__ = [
    "Approval",
    "CarryForward",
    "Citation",
    "Finding",
    "MaterialRegistry",
    "NodeRef",
    "ParagraphResult",
    "Range",
    "RecordRef",
    "Remediation",
    "ReviewCase",
    "ReviewEngine",
    "ReviewPolicy",
    "ReviewReport",
    "Segment",
    "Submission",
    "VersionedRecord",
    "case_from_dict",
    "fingerprint_paragraphs",
    "fingerprint_text",
    "normalize_ranges",
    "review_case",
    "review_dict",
    "review_payload",
]

__version__ = "1.0.0"
