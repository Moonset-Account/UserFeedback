"""Public surface for handshake UTC time adjudication."""

from .core import (
    AdjudicationResult,
    TimeReceipt,
    UtcInterval,
    Verdict,
    adjudicate,
    covers,
    retained_segments,
)

__all__ = [
    "AdjudicationResult",
    "TimeReceipt",
    "UtcInterval",
    "Verdict",
    "adjudicate",
    "covers",
    "retained_segments",
]
