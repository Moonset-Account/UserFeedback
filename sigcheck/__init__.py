"""文书签章修订检查器（签名增量保存越界检查）。

公开 API：
    DocumentModelError, Revision, ObjectVersion, ReferenceEdge, Signature,
    Document, load_document, inspect_document, InspectionReport,
    SignatureAssessment, Finding, Hop
"""

from .model import (
    Document,
    DocumentModelError,
    ObjectVersion,
    ReferenceEdge,
    Revision,
    Signature,
    load_document,
)
from .inspector import (
    Finding,
    Hop,
    InspectionReport,
    SignatureAssessment,
    inspect_document,
)

__all__ = [
    "Document",
    "DocumentModelError",
    "Revision",
    "ObjectVersion",
    "ReferenceEdge",
    "Signature",
    "load_document",
    "inspect_document",
    "InspectionReport",
    "SignatureAssessment",
    "Finding",
    "Hop",
]
