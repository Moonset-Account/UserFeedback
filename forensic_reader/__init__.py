"""Forensic reader for equal-length chip dumps and finite candidate wirings."""

from .errors import (
    AmbiguousReadError,
    ChipValidationError,
    EvidenceFormatError,
    ForensicReaderError,
    LogicalRangeError,
    NoConsistentWiringError,
    WiringConfigurationError,
)
from .evidence import EvidenceEvaluation, KnownByte, PageChecksum, coerce_evidence
from .history import HistoryEntry, Rejection
from .reader import ForensicReader, compare_reports
from .report import (
    AMBIGUOUS,
    CONFIRMED,
    NO_WIRING,
    ByteState,
    ConfirmedRange,
    Interpretation,
    ReadReport,
    ReportDiff,
    Segment,
    TraceReport,
    VersionJudgment,
)
from .wiring import Wiring, generate_wirings

__version__ = "1.0.0"

# Descriptive aliases for callers that prefer the domain vocabulary.
Reader = ForensicReader
ForensicImageReader = ForensicReader
ChipForensicReader = ForensicReader
create_reader = ForensicReader

__all__ = [
    "ForensicReader",
    "Reader",
    "ForensicImageReader",
    "ChipForensicReader",
    "create_reader",
    "Wiring",
    "KnownByte",
    "PageChecksum",
    "coerce_evidence",
    "EvidenceEvaluation",
    "generate_wirings",
    "compare_reports",
    "ReadReport",
    "Segment",
    "Interpretation",
    "ByteState",
    "ReportDiff",
    "ConfirmedRange",
    "TraceReport",
    "VersionJudgment",
    "HistoryEntry",
    "Rejection",
    "ForensicReaderError",
    "ChipValidationError",
    "WiringConfigurationError",
    "EvidenceFormatError",
    "LogicalRangeError",
    "NoConsistentWiringError",
    "AmbiguousReadError",
    "CONFIRMED",
    "AMBIGUOUS",
    "NO_WIRING",
    "__version__",
]
