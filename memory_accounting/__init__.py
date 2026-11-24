"""Replay and account physical memory observations with frame generations."""

from .core import (
    AccountingError,
    AccountingReport,
    FrameRef,
    FrameShare,
    MappingError,
    MappingRange,
    MemoryAccount,
    ProcessReport,
    account_observations,
    fraction_to_dict,
)
from .replay import (
    GapError,
    GapInfo,
    PositionStatus,
    Receipt,
    ReplayResult,
    ReplaySession,
    account_receipts,
    apply_record,
    apply_receipt,
    replay_receipts,
)

__all__ = [
    "AccountingError",
    "AccountingReport",
    "FrameRef",
    "FrameShare",
    "GapError",
    "GapInfo",
    "MappingError",
    "MappingRange",
    "MemoryAccount",
    "PositionStatus",
    "ProcessReport",
    "Receipt",
    "ReplayResult",
    "ReplaySession",
    "account_observations",
    "account_receipts",
    "apply_record",
    "apply_receipt",
    "fraction_to_dict",
    "replay_receipts",
]

__version__ = "1.0.0"

# Descriptive aliases for callers that prefer "accountant/replayer" nouns.
MemoryAccountant = MemoryAccount
MemoryReplayer = ReplaySession
calculate_memory = replay_receipts
account_positions = replay_receipts

__all__ += [
    "MemoryAccountant",
    "MemoryReplayer",
    "calculate_memory",
    "account_positions",
]
