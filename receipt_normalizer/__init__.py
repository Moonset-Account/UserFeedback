"""Receipt normalization for ABI-aware audit action decoding.

Public API overview::

    from receipt_normalizer import (
        ActionRecord, ImageSwitch, TableVersion, ReceiptNormalizer,
        normalize_receipt, normalize_receipts,
    )

See README.md for the input schema and resolution rules.
"""

from .model import (
    ABI_ANY,
    ABI_COMPAT,
    ABI_NATIVE,
    STATUS_COMPLETE,
    STATUS_EVIDENCE_GAP,
    ActionRecord,
    EvidenceGap,
    GapCode,
    ImageSwitch,
    NormalizedReceipt,
    ProcessInfo,
    Target,
)
from .normalizer import (
    ReceiptNormalizer,
    normalize_receipt,
    normalize_receipts,
)
from .tables import (
    ActionMapping,
    DecodeTable,
    MultiplexSpec,
    ObjectSpec,
    SubOperation,
    TableSchedule,
    TableVersion,
)
from .timeline import ImageTimeline

__all__ = [
    "ABI_ANY",
    "ABI_COMPAT",
    "ABI_NATIVE",
    "STATUS_COMPLETE",
    "STATUS_EVIDENCE_GAP",
    "ActionMapping",
    "ActionRecord",
    "DecodeTable",
    "EvidenceGap",
    "GapCode",
    "ImageSwitch",
    "ImageTimeline",
    "MultiplexSpec",
    "NormalizedReceipt",
    "ObjectSpec",
    "ProcessInfo",
    "ReceiptNormalizer",
    "SubOperation",
    "TableSchedule",
    "TableVersion",
    "Target",
    "normalize_receipt",
    "normalize_receipts",
]

__version__ = "1.0.0"
