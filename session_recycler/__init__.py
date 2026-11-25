"""Session recycling for a database connection proxy.

The public entry point is :class:`SessionRecycler` (also available as
``ConnectionPool``).  See README.md for the event vocabulary and examples.
"""

from .events import EventType, StateEvent, coerce_event
from .model import (
    UNSET,
    AckResult,
    ActionKind,
    ActionStatus,
    ConnectionStatus,
    OperationStatus,
    RecoveryAction,
    RecoveryOperation,
    SuppressedAction,
)
from .planner import RecoveryPlan, RecoveryPlanner, plan_recovery
from .recycler import (
    ConnectionNotAvailableError,
    ConnectionNotFoundError,
    InvalidReturnStateError,
    ManagedConnection,
    RecyclerError,
    SessionRecycler,
)
from .state import SessionState, TemporaryObject, TransactionInfo

# Pool-oriented aliases.
ConnectionPool = SessionRecycler
SessionRecoveryManager = SessionRecycler
RecyclingManager = SessionRecycler
SessionTracker = SessionState

__all__ = [
    "UNSET",
    "AckResult",
    "ActionKind",
    "ActionStatus",
    "ConnectionNotAvailableError",
    "ConnectionNotFoundError",
    "ConnectionPool",
    "ConnectionStatus",
    "EventType",
    "InvalidReturnStateError",
    "ManagedConnection",
    "OperationStatus",
    "RecoveryAction",
    "RecoveryOperation",
    "RecoveryPlan",
    "RecoveryPlanner",
    "RecyclingManager",
    "SessionRecoveryManager",
    "RecyclerError",
    "SessionRecycler",
    "SessionState",
    "SessionTracker",
    "StateEvent",
    "SuppressedAction",
    "TemporaryObject",
    "TransactionInfo",
    "coerce_event",
    "plan_recovery",
]

__version__ = "1.0.0"
