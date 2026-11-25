"""Core data types for the session recycling module.

The module deliberately uses only the Python standard library.  The types are
small, explicit, and easily serialised by a connection proxy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class _Unset:
    """Sentinel for a parameter/object that is absent at the baseline."""

    _instance: "_Unset | None" = None

    def __new__(cls) -> "_Unset":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET = _Unset()


class ActionKind(str, Enum):
    """Logical restoration actions."""

    ROLLBACK_TRANSACTION = "rollback_transaction"
    RESTORE_SESSION_PARAMETER = "restore_session_parameter"
    DROP_TEMPORARY_OBJECT = "drop_temporary_object"
    RESET_TRANSACTION_PARAMETER = "reset_transaction_parameter"


class ActionStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class OperationStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class ConnectionStatus(str, Enum):
    AVAILABLE = "available"
    BORROWED = "borrowed"
    RECYCLING = "recycling"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class SuppressedAction:
    """A restoration action intentionally not emitted as a required action."""

    kind: ActionKind
    target: str | None
    reason: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "target": self.target,
            "reason": self.reason,
            "details": dict(self.details),
        }


@dataclass
class RecoveryAction:
    """One necessary restoration action.

    ``id`` is globally addressable within a recycler and contains the return
    operation id.  A receipt for this id can therefore never be confused with
    an action from another return/generation.
    """

    id: str
    operation_id: str
    sequence: int
    kind: ActionKind
    key: str
    target: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    status: ActionStatus = ActionStatus.PENDING
    error: str | None = None
    receipt_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def action_id(self) -> str:
        return self.id

    @property
    def uid(self) -> str:
        return self.id

    @property
    def token(self) -> str:
        return self.id

    @property
    def type(self) -> str:
        return self.kind.value

    @property
    def name(self) -> str:
        return self.key

    @property
    def sql(self) -> str | None:
        value = self.arguments.get("sql")
        return value if isinstance(value, str) else None

    @property
    def succeeded(self) -> bool:
        return self.status == ActionStatus.SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.status == ActionStatus.FAILED

    @property
    def pending(self) -> bool:
        return self.status == ActionStatus.PENDING

    def receipt(self, success: bool = True, error: str | None = None) -> dict[str, Any]:
        """Return a synthetic receipt mapping for this action."""

        result: dict[str, Any] = {
            "operation_id": self.operation_id,
            "action_id": self.id,
            "success": success,
        }
        if error is not None:
            result["error"] = error
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "operation_id": self.operation_id,
            "sequence": self.sequence,
            "kind": self.kind.value,
            "key": self.key,
            "target": self.target,
            "arguments": dict(self.arguments),
            "status": self.status.value,
            "error": self.error,
        }


@dataclass
class RecoveryOperation:
    """The plan and receipt state for one application return."""

    id: str
    connection_id: str
    return_number: int
    actions: list[RecoveryAction]
    suppressed: list[SuppressedAction] = field(default_factory=list)
    status: OperationStatus = OperationStatus.PENDING
    error: str | None = None
    state_at_return: Any = None
    state_for_receipts: Any = None
    receipts: list[dict[str, Any]] = field(default_factory=list)

    def action(self, action_ref: str | int) -> RecoveryAction:
        found = self.find_action(action_ref)
        if found is None:
            raise KeyError(f"operation {self.id} has no action {action_ref!r}")
        return found

    def find_action(self, action_ref: str | int | None) -> RecoveryAction | None:
        if action_ref is None:
            if len(self.actions) == 1:
                return self.actions[0]
            return None

        if isinstance(action_ref, int):
            for action in self.actions:
                if action.sequence == action_ref:
                    return action
            return None

        ref = str(action_ref)
        for action in self.actions:
            if ref in {action.id, action.key, action.target, str(action.sequence)}:
                return action
        return None

    @property
    def pending_actions(self) -> list[RecoveryAction]:
        return [a for a in self.actions if a.status == ActionStatus.PENDING]

    @property
    def succeeded_actions(self) -> list[RecoveryAction]:
        return [a for a in self.actions if a.status == ActionStatus.SUCCEEDED]

    @property
    def failed_action(self) -> RecoveryAction | None:
        for action in self.actions:
            if action.status == ActionStatus.FAILED:
                return action
        return None

    @property
    def all_succeeded(self) -> bool:
        return bool(self.actions) and all(a.succeeded for a in self.actions)

    @property
    def is_empty(self) -> bool:
        return not self.actions

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.SUPERSEDED,
        }

    @property
    def can_return_connection(self) -> bool:
        return self.status == OperationStatus.SUCCEEDED and self.all_succeeded

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "connection_id": self.connection_id,
            "return_number": self.return_number,
            "status": self.status.value,
            "error": self.error,
            "actions": [a.to_dict() for a in self.actions],
            "suppressed": [s.to_dict() for s in self.suppressed],
            "all_succeeded": self.all_succeeded,
        }


@dataclass
class AckResult:
    """Result of applying a receipt."""

    accepted: bool
    outcome: str
    operation_id: str | None = None
    action_id: str | None = None
    connection_id: str | None = None
    stale: bool = False
    applied_to_connection: bool = False
    returned_to_pool: bool = False
    quarantined: bool = False
    operation: RecoveryOperation | None = None
    action: RecoveryAction | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "outcome": self.outcome,
            "operation_id": self.operation_id,
            "action_id": self.action_id,
            "connection_id": self.connection_id,
            "stale": self.stale,
            "applied_to_connection": self.applied_to_connection,
            "returned_to_pool": self.returned_to_pool,
            "quarantined": self.quarantined,
        }
