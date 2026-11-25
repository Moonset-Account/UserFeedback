"""Connection pool/recycler with return-scoped receipts and generations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .events import coerce_event
from .model import (
    AckResult,
    ConnectionStatus,
    OperationStatus,
    RecoveryAction,
    RecoveryOperation,
)
from .planner import RecoveryPlanner
from .state import SessionState


class RecyclerError(RuntimeError):
    """Base class for recycler state errors."""


class ConnectionNotFoundError(KeyError, RecyclerError):
    """Raised when an unknown connection id is used."""


class ConnectionNotAvailableError(RecyclerError):
    """Raised when checkout is requested for a non-borrowable connection."""


class InvalidReturnStateError(RecyclerError):
    """Raised on a conflicting concurrent/double return."""


@dataclass
class ManagedConnection:
    id: str
    state: SessionState
    status: ConnectionStatus = ConnectionStatus.AVAILABLE
    return_number: int = 0
    latest_operation_id: str | None = None
    active_operation_id: str | None = None
    last_succeeded_operation_id: str | None = None
    quarantined_operation_id: str | None = None
    quarantine_reason: str | None = None
    operation_history: list[str] = field(default_factory=list)


class SessionRecycler:
    """Track sessions, plan resets, correlate receipts, and maintain a pool.

    Parameters
    ----------
    planner:
        Optional custom planner.
    default_baseline_parameters:
        Baseline used when registering a connection without an explicit one.
    """

    def __init__(
        self,
        planner: RecoveryPlanner | None = None,
        default_baseline_parameters: Mapping[str, Any] | None = None,
    ) -> None:
        self.planner = planner or RecoveryPlanner()
        self.default_baseline_parameters = dict(default_baseline_parameters or {})
        self.connections: dict[str, ManagedConnection] = {}
        self.states: dict[str, SessionState] = {}
        self.operations: dict[str, RecoveryOperation] = {}
        self.action_index: dict[str, str] = {}

        #: The authoritative borrowable set.  ``available`` is an alias.
        self.borrowable: set[str] = set()
        self.available: set[str] = self.borrowable

        self._operation_counter = 0

    # ------------------------------------------------------------------
    # Registration and lookup
    # ------------------------------------------------------------------
    def register(
        self,
        connection_id: str,
        baseline_parameters: Mapping[str, Any] | None = None,
        baseline_temporary_objects: list[Any] | None = None,
        *,
        available: bool = True,
        overwrite: bool = False,
    ) -> SessionState:
        if connection_id in self.connections and not overwrite:
            raise ValueError(f"connection {connection_id!r} is already registered")

        baseline = (
            baseline_parameters
            if baseline_parameters is not None
            else self.default_baseline_parameters
        )
        state = SessionState(
            connection_id,
            baseline_parameters=baseline,
            baseline_temporary_objects=baseline_temporary_objects,
        )
        status = ConnectionStatus.AVAILABLE if available else ConnectionStatus.BORROWED
        managed = ManagedConnection(id=connection_id, state=state, status=status)
        self.connections[connection_id] = managed
        self.states[connection_id] = state
        if available:
            self.borrowable.add(connection_id)
        else:
            self.borrowable.discard(connection_id)
        return state

    # Friendly aliases for host applications.
    add_connection = register

    def add(
        self,
        connection_id: str,
        baseline_parameters: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> SessionState:
        return self.register(connection_id, baseline_parameters, **kwargs)

    def require_connection(self, connection_id: str) -> ManagedConnection:
        try:
            return self.connections[connection_id]
        except KeyError as exc:
            raise ConnectionNotFoundError(connection_id) from exc

    def get_state(self, connection_id: str) -> SessionState:
        return self.require_connection(connection_id).state

    def state_for(self, connection_id: str) -> SessionState:
        return self.get_state(connection_id)

    def is_borrowable(self, connection_id: str) -> bool:
        return (
            connection_id in self.borrowable
            and connection_id in self.connections
            and self.connections[connection_id].status == ConnectionStatus.AVAILABLE
        )

    def available_ids(self) -> list[str]:
        return sorted(self.borrowable)

    def borrowable_ids(self) -> list[str]:
        return sorted(self.borrowable)

    # ------------------------------------------------------------------
    # Event intake
    # ------------------------------------------------------------------
    def handle_event(
        self,
        connection_id_or_event: str | Mapping[str, Any] | Any | None = None,
        event: Any = None,
        *,
        connection_id: str | None = None,
    ) -> SessionState:
        """Apply a parsed state event.

        Supports both ``handle_event("c1", event)`` and
        ``handle_event(event_mapping_with_connection_id)``.
        """

        if event is None and not isinstance(connection_id_or_event, str):
            candidate = connection_id_or_event
            parsed = coerce_event(candidate)
            event = candidate
            connection_id = connection_id or parsed.connection_id
        elif event is None and isinstance(connection_id_or_event, str):
            # handle_event("c1", {"type": ...}) normally has event supplied.
            # A bare string event is also accepted.
            if connection_id is None:
                raise TypeError("connection_id is required when event is a string")
        else:
            connection_id = connection_id or (
                connection_id_or_event if isinstance(connection_id_or_event, str) else None
            )

        if connection_id is None:
            if isinstance(connection_id_or_event, str) and event is not None:
                connection_id = connection_id_or_event
            else:
                raise ValueError("connection_id is required")

        if event is None and isinstance(connection_id_or_event, str):
            event = connection_id_or_event
        if event is None:
            raise ValueError("event is required")

        if connection_id not in self.connections:
            # Events observed while the application holds the connection.
            self.register(connection_id, available=False)

        state = self.get_state(connection_id)
        state.apply(event)
        return state

    track_event = handle_event
    record_event = handle_event
    apply_event = handle_event

    # ------------------------------------------------------------------
    # Checkout / return / planning
    # ------------------------------------------------------------------
    def checkout(self, connection_id: str | None = None) -> str:
        if connection_id is None:
            if not self.borrowable:
                raise ConnectionNotAvailableError("no borrowable connections")
            connection_id = sorted(self.borrowable)[0]

        managed = self.require_connection(connection_id)
        if not self.is_borrowable(connection_id):
            raise ConnectionNotAvailableError(
                f"connection {connection_id!r} is not borrowable "
                f"(status={managed.status.value})"
            )
        self.borrowable.remove(connection_id)
        managed.status = ConnectionStatus.BORROWED
        return connection_id

    borrow = checkout
    acquire = checkout

    def return_connection(
        self,
        connection_id: str,
        *,
        events: list[Any] | tuple[Any, ...] | None = None,
        force_new_generation: bool = False,
    ) -> RecoveryOperation:
        managed = self.require_connection(connection_id)

        if events:
            for event in events:
                managed.state.apply(event)

        if (
            managed.status == ConnectionStatus.RECYCLING
            and managed.active_operation_id is not None
        ):
            if not force_new_generation:
                raise InvalidReturnStateError(
                    f"connection {connection_id!r} already has an active return "
                    f"operation {managed.active_operation_id}"
                )
            old = self.operations[managed.active_operation_id]
            old.status = OperationStatus.SUPERSEDED
            managed.active_operation_id = None

        managed.return_number += 1
        self._operation_counter += 1
        operation_id = f"op-{self._operation_counter}"

        plan = self.planner.plan(managed.state)
        actions: list[RecoveryAction] = []
        for planned in plan.actions:
            action_id = f"{operation_id}:{planned.sequence}"
            action = RecoveryAction(
                id=action_id,
                operation_id=operation_id,
                sequence=planned.sequence,
                kind=planned.kind,
                key=planned.key,
                target=planned.target,
                arguments=dict(planned.arguments),
            )
            actions.append(action)
            self.action_index[action.id] = operation_id

        state_at_return = managed.state.snapshot()
        operation = RecoveryOperation(
            id=operation_id,
            connection_id=connection_id,
            return_number=managed.return_number,
            actions=actions,
            suppressed=list(plan.suppressed),
            status=OperationStatus.PENDING,
            state_at_return=state_at_return,
            state_for_receipts=managed.state.snapshot(),
        )
        self.operations[operation_id] = operation
        managed.operation_history.append(operation_id)
        managed.latest_operation_id = operation_id
        managed.active_operation_id = operation_id
        managed.status = ConnectionStatus.RECYCLING
        self.borrowable.discard(connection_id)

        if not actions:
            operation.status = OperationStatus.SUCCEEDED
            managed.active_operation_id = None
            managed.last_succeeded_operation_id = operation_id
            managed.status = ConnectionStatus.AVAILABLE
            self.borrowable.add(connection_id)

        return operation

    checkin = return_connection
    recycle = return_connection
    recycle_connection = return_connection

    def reintroduce(
        self,
        connection_id: str,
        baseline_parameters: Mapping[str, Any] | None = None,
        baseline_temporary_objects: list[Any] | None = None,
        *,
        reset_state: bool = True,
    ) -> SessionState:
        """Administratively return a quarantined connection to the pool.

        This is intentionally not invoked by a late receipt.  After a failed
        restoration, an operator/health check must verify the connection.  When
        ``reset_state`` is true, a fresh tracker is created from the supplied
        baseline (or the recycler default baseline).
        """

        managed = self.require_connection(connection_id)
        if reset_state:
            baseline = (
                baseline_parameters
                if baseline_parameters is not None
                else self.default_baseline_parameters
            )
            state = SessionState(
                connection_id,
                baseline_parameters=baseline,
                baseline_temporary_objects=baseline_temporary_objects,
            )
            managed.state = state
            self.states[connection_id] = state
        managed.status = ConnectionStatus.AVAILABLE
        managed.active_operation_id = None
        managed.quarantine_reason = None
        managed.quarantined_operation_id = None
        self.borrowable.add(connection_id)
        return managed.state

    readd_to_pool = reintroduce
    mark_healthy = reintroduce
    restore_to_pool = reintroduce

    # ------------------------------------------------------------------
    # Receipts / acknowledgements
    # ------------------------------------------------------------------
    def acknowledge(
        self,
        first: Any = None,
        second: Any = None,
        third: Any = None,
        /,
        *,
        success: bool | None = None,
        ok: bool | None = None,
        operation_id: str | None = None,
        return_id: str | None = None,
        op_id: str | None = None,
        action_id: str | None = None,
        action: str | int | None = None,
        receipt: Mapping[str, Any] | None = None,
        error: str | None = None,
        connection_id: str | None = None,
        **extra: Any,
    ) -> AckResult:
        """Apply a success/failure receipt.

        Supported calling styles include::

            recycler.acknowledge(action.id, True)
            recycler.acknowledge(operation.id, "rollback", True)
            recycler.acknowledge(connection_id, "rollback", True)
            recycler.acknowledge({"operation_id": ..., "action_id": ..., "ok": True})
            recycler.acknowledge(receipt=action.receipt())

        Connection-plus-key fallback is convenient for current receipts, but
        asynchronous/late receipts should carry the unique action id or
        operation/return id so generations cannot be confused.
        """

        # Positional object/mapping receipt.
        if receipt is None and isinstance(first, Mapping):
            receipt = first
            first = None
        if receipt is not None:
            parsed_op, parsed_action, parsed_success, parsed_error, parsed_conn = (
                self._parse_receipt_mapping(receipt)
            )
            operation_id = operation_id or parsed_op
            action_id = action_id or parsed_action
            if success is None and ok is None:
                success = parsed_success
            error = error or parsed_error
            connection_id = connection_id or parsed_conn

        # RecoveryAction object as first argument.
        if isinstance(first, RecoveryAction):
            operation_id = operation_id or first.operation_id
            action_id = action_id or first.id
            if isinstance(second, bool) and success is None:
                success = second
            elif isinstance(second, str) and error is None:
                error = second
            if isinstance(third, bool) and success is None:
                success = third
            first = None
            second = None

        # Positional string forms.
        if first is not None:
            first_text = str(first)
            if second is None:
                if operation_id is None and action_id is None:
                    if first_text in self.action_index:
                        action_id = first_text
                    elif first_text in self.operations:
                        operation_id = first_text
                    else:
                        action_id = first_text  # preserve for unknown result
                elif action_id is None:
                    action_id = first_text
            elif isinstance(second, bool):
                action_id = action_id or first_text
                if success is None:
                    success = second
                if isinstance(third, str):
                    error = error or third
            else:
                operation_id = operation_id or first_text
                action_id = action_id or (str(second) if second is not None else None)
                if isinstance(third, bool) and success is None:
                    success = third
                elif isinstance(third, str) and error is None:
                    error = third

        operation_id = operation_id or return_id or op_id

        # Allow the natural form acknowledge(connection_id, action_ref, ok).
        # Unique action ids/op ids still take precedence below.
        if (
            operation_id is not None
            and operation_id not in self.operations
            and operation_id in self.connections
            and connection_id is None
        ):
            connection_id = operation_id
            operation_id = None

        action_ref = action_id if action_id is not None else action
        if ok is not None and success is None:
            success = ok
        if success is None:
            success = True

        if operation_id is None and action_ref is not None:
            ref_text = str(action_ref)
            operation_id = self.action_index.get(ref_text)
            if operation_id is None and ":" in ref_text:
                maybe_op, maybe_sequence = ref_text.split(":", 1)
                if maybe_op in self.operations:
                    operation_id = maybe_op
                elif maybe_sequence.isdigit() and maybe_op.startswith("op-"):
                    operation_id = maybe_op

        if operation_id is None and connection_id is not None:
            managed_for_receipt = self.connections.get(str(connection_id))
            if managed_for_receipt is not None:
                # Prefer an in-flight generation.  If none is active, attach a
                # late/duplicate keyed receipt to the latest return; globally
                # unique action ids are always resolved before this fallback.
                operation_id = (
                    managed_for_receipt.active_operation_id
                    or managed_for_receipt.latest_operation_id
                )

        if operation_id is None:
            if connection_id is not None and connection_id not in self.connections:
                outcome = "unknown_connection"
            else:
                outcome = "missing_operation"
            return AckResult(
                accepted=False,
                outcome=outcome,
                connection_id=str(connection_id) if connection_id is not None else None,
                action_id=str(action_ref) if action_ref is not None else None,
            )

        operation = self.operations.get(str(operation_id))
        if operation is None:
            return AckResult(
                accepted=False,
                outcome="unknown_operation",
                operation_id=str(operation_id),
                action_id=str(action_ref) if action_ref is not None else None,
            )

        if connection_id is not None and operation.connection_id != str(connection_id):
            return AckResult(
                accepted=False,
                outcome="connection_mismatch",
                operation_id=operation.id,
                connection_id=operation.connection_id,
                action_id=str(action_ref) if action_ref is not None else None,
                operation=operation,
            )

        resolved_action = operation.find_action(action_ref)
        if resolved_action is None:
            return AckResult(
                accepted=False,
                outcome="unknown_action",
                operation_id=operation.id,
                connection_id=operation.connection_id,
                action_id=str(action_ref) if action_ref is not None else None,
                operation=operation,
            )

        managed = self.connections[operation.connection_id]
        is_latest_generation = managed.latest_operation_id == operation.id
        is_active_generation = managed.active_operation_id == operation.id
        receipt_record = {
            "operation_id": operation.id,
            "action_id": resolved_action.id,
            "success": bool(success),
            "error": error,
        }
        operation.receipts.append(receipt_record)
        resolved_action.receipt_history.append(dict(receipt_record))

        # Idempotent / conflicting duplicate handling.
        if resolved_action.status != "pending":
            same_outcome = (
                (success and resolved_action.status.value == "succeeded")
                or (not success and resolved_action.status.value == "failed")
            )
            outcome = "duplicate" if same_outcome else "conflicting_terminal_receipt"
            return AckResult(
                accepted=same_outcome,
                outcome=outcome,
                operation_id=operation.id,
                action_id=resolved_action.id,
                connection_id=operation.connection_id,
                stale=not is_latest_generation,
                applied_to_connection=False,
                returned_to_pool=False,
                quarantined=managed.status == ConnectionStatus.QUARANTINED
                and managed.quarantined_operation_id == operation.id,
                operation=operation,
                action=resolved_action,
            )

        if success:
            resolved_action.status = type(resolved_action.status).SUCCEEDED
            resolved_action.error = None
            self._apply_action_to_operation_snapshot(resolved_action, operation)

            applied_live = False
            returned = False
            if is_active_generation and operation.status == OperationStatus.PENDING:
                self._apply_action_to_state(resolved_action, managed.state)
                applied_live = True

            if operation.status == OperationStatus.PENDING and operation.all_succeeded:
                operation.status = OperationStatus.SUCCEEDED
                if is_active_generation:
                    self._mark_available(managed, operation)
                    returned = True

            if not is_latest_generation:
                outcome = "applied_stale"
            elif not is_active_generation:
                outcome = "applied_late"
            elif returned:
                outcome = "operation_succeeded"
            else:
                outcome = "applied"

            return AckResult(
                accepted=True,
                outcome=outcome,
                operation_id=operation.id,
                action_id=resolved_action.id,
                connection_id=operation.connection_id,
                stale=not is_latest_generation,
                applied_to_connection=applied_live,
                returned_to_pool=returned,
                quarantined=(
                    managed.status == ConnectionStatus.QUARANTINED
                    and managed.quarantined_operation_id == operation.id
                ),
                operation=operation,
                action=resolved_action,
            )

        # Failure receipt.
        resolved_action.status = type(resolved_action.status).FAILED
        resolved_action.error = error or "restoration action failed"
        operation.status = OperationStatus.FAILED
        operation.error = resolved_action.error

        quarantined = False
        applied_live = False
        already_quarantined = (
            managed.status == ConnectionStatus.QUARANTINED
            and managed.quarantined_operation_id == operation.id
        )
        if is_active_generation:
            self._mark_quarantined(managed, operation, resolved_action.error)
            applied_live = True
            quarantined = True
        elif already_quarantined:
            quarantined = True

        if not is_latest_generation:
            outcome = "failure_applied_stale"
        elif not is_active_generation:
            outcome = "failure_applied_late"
        else:
            outcome = "failure_quarantined"

        return AckResult(
            accepted=True,
            outcome=outcome,
            operation_id=operation.id,
            action_id=resolved_action.id,
            connection_id=operation.connection_id,
            stale=not is_latest_generation,
            applied_to_connection=applied_live,
            quarantined=quarantined,
            operation=operation,
            action=resolved_action,
        )

    ack = acknowledge
    receive_receipt = acknowledge
    handle_receipt = acknowledge
    process_receipt = acknowledge
    complete_action = acknowledge
    report_receipt = acknowledge

    @staticmethod
    def _parse_receipt_mapping(receipt: Mapping[str, Any]) -> tuple[
        str | None, str | int | None, bool | None, str | None, str | None
    ]:
        op = (
            receipt.get("operation_id")
            or receipt.get("return_id")
            or receipt.get("op_id")
            or receipt.get("return")
        )
        action = (
            receipt.get("action_id")
            or receipt.get("action")
            or receipt.get("receipt_id")
            or receipt.get("id")
        )
        success: bool | None = None
        for key in ("success", "succeeded", "ok"):
            if key in receipt:
                success = bool(receipt[key])
                break
        error = receipt.get("error") or receipt.get("reason")
        connection_id = receipt.get("connection_id") or receipt.get("conn_id")
        return (
            None if op is None else str(op),
            action,
            success,
            None if error is None else str(error),
            None if connection_id is None else str(connection_id),
        )

    # ------------------------------------------------------------------
    # State transitions driven by successful receipts
    # ------------------------------------------------------------------
    def _apply_action_to_operation_snapshot(
        self, action: RecoveryAction, operation: RecoveryOperation
    ) -> None:
        snapshot = operation.state_for_receipts
        if snapshot is not None:
            self._apply_action_to_state(action, snapshot)

    @staticmethod
    def _apply_action_to_state(action: RecoveryAction, state: SessionState) -> None:
        if action.kind.value == "rollback_transaction":
            state.mark_rollback_completed(action.target)
        elif action.kind.value == "restore_session_parameter":
            baseline_is_default = bool(action.arguments.get("baseline_is_default"))
            if baseline_is_default:
                state.restore_session_parameter(action.target)
            else:
                state.restore_session_parameter(
                    action.target, action.arguments.get("baseline_value")
                )
        elif action.kind.value == "drop_temporary_object":
            key = action.arguments.get("object_key") or action.target
            state.drop_temporary_object(key)
        elif action.kind.value == "reset_transaction_parameter":
            state.transaction_parameters.pop(action.target, None)

    def _mark_available(
        self, managed: ManagedConnection, operation: RecoveryOperation
    ) -> None:
        managed.status = ConnectionStatus.AVAILABLE
        managed.active_operation_id = None
        managed.last_succeeded_operation_id = operation.id
        managed.quarantine_reason = None
        self.borrowable.add(managed.id)

    def _mark_quarantined(
        self, managed: ManagedConnection, operation: RecoveryOperation, reason: str
    ) -> None:
        managed.status = ConnectionStatus.QUARANTINED
        managed.active_operation_id = None
        managed.quarantined_operation_id = operation.id
        managed.quarantine_reason = reason
        self.borrowable.discard(managed.id)

    # ------------------------------------------------------------------
    # Convenience for tests/diagnostics
    # ------------------------------------------------------------------
    def succeed_all(
        self, operation: RecoveryOperation | str
    ) -> list[AckResult]:
        """Synthetically acknowledge every pending action successfully."""

        op = operation if isinstance(operation, RecoveryOperation) else self.operations[str(operation)]
        results = []
        # Acknowledge in plan order to exercise ordered state transitions.
        for action in list(op.actions):
            if action.pending:
                results.append(self.acknowledge(action.id, True))
        return results

    def summary(self) -> dict[str, Any]:
        return {
            "borrowable": sorted(self.borrowable),
            "connections": {
                cid: {
                    "status": managed.status.value,
                    "return_number": managed.return_number,
                    "latest_operation_id": managed.latest_operation_id,
                    "active_operation_id": managed.active_operation_id,
                    "quarantine_reason": managed.quarantine_reason,
                    "state": managed.state.to_dict(),
                }
                for cid, managed in sorted(self.connections.items())
            },
            "operations": {
                oid: op.to_dict() for oid, op in sorted(self.operations.items())
            },
        }
