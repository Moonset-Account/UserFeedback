"""Unified-identity logout completion decider.

The public entry points are:

* :class:`LogoutDecider` for stateful, out-of-order event ingestion;
* :func:`decide_logout_completion` for one-shot dictionary/object input;
* the data models in :mod:`identity_logout.model`.
"""

from .decider import (
    CompletionNotReady,
    LogoutDecider,
    decide_logout_completion,
)
from .errors import (
    BarrierConfigurationError,
    EventConflictError,
    InvalidEventError,
    LogoutDecisionError,
)
from .model import (
    CompletionReceipt,
    Event,
    EventKind,
    LogoutDecision,
    MissingEvidence,
    RevocationBarrier,
    Watermark,
)

__all__ = [
    "LogoutDecider",
    "decide_logout_completion",
    "CompletionNotReady",
    "LogoutDecisionError",
    "InvalidEventError",
    "EventConflictError",
    "BarrierConfigurationError",
    "Event",
    "EventKind",
    "Watermark",
    "RevocationBarrier",
    "MissingEvidence",
    "CompletionReceipt",
    "LogoutDecision",
]

__version__ = "1.0.0"
