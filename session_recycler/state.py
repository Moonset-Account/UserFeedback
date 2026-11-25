"""Event-sourced state for one database connection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .events import EventType, StateEvent, coerce_event
from .model import UNSET, _Unset


@dataclass(frozen=True)
class TemporaryObject:
    name: str
    object_type: str = "table"
    schema: str | None = None

    @property
    def key(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    @property
    def qualified_name(self) -> str:
        return self.key

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "object_type": self.object_type, "schema": self.schema}


@dataclass
class TransactionInfo:
    id: str
    status: str = "open"
    local_parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class SessionState:
    """Track parsed session state.

    The four tracked categories from the task are available as:

    * :attr:`temporary_objects` / :attr:`temporary_tables`
    * :attr:`session_parameters`
    * :attr:`transaction_parameters`
    * :attr:`open_transactions`
    """

    def __init__(
        self,
        connection_id: str,
        baseline_parameters: Mapping[str, Any] | None = None,
        baseline_temporary_objects: Iterable[Any] | None = None,
    ) -> None:
        self.connection_id = connection_id
        self.baseline_parameters: dict[str, Any] = {}
        self.session_parameters: dict[str, Any] = {}
        self.transaction_parameters: dict[str, Any] = {}
        self.transactions: dict[str, TransactionInfo] = {}
        self.transaction_order: list[str] = []
        self.temporary_objects: dict[str, TemporaryObject] = {}
        self.baseline_temporary_objects: set[str] = set()
        self.applied_event_ids: set[str] = set()
        self.event_count = 0
        self._transaction_counter = 0

        self.set_baseline_parameters(baseline_parameters or {})
        for obj in baseline_temporary_objects or []:
            temporary = self._coerce_object(obj)
            self.baseline_temporary_objects.add(temporary.key)
            self.temporary_objects[temporary.key] = temporary

    # ------------------------------------------------------------------
    # Baseline and parameter helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_baseline_value(value: Any) -> Any:
        return UNSET if value is None or isinstance(value, _Unset) else value

    def set_baseline_parameters(self, parameters: Mapping[str, Any] | None) -> None:
        normalized: dict[str, Any] = {}
        for key, value in (parameters or {}).items():
            normalized[str(key)] = self._normalize_baseline_value(value)
        self.baseline_parameters = normalized
        # A fresh known session starts at its baseline.  Preserve explicit
        # current overrides if this is called after activity by only filling
        # missing keys?  Baseline events are expected first; resetting is the
        # least surprising semantics for an explicit baseline snapshot.
        self.session_parameters = {
            key: value for key, value in normalized.items() if value is not UNSET
        }

    def capture_baseline(self, parameters: Mapping[str, Any] | None = None) -> None:
        self.set_baseline_parameters(parameters if parameters is not None else self.session_parameters)

    def changed_session_parameters(self) -> dict[str, Any]:
        """Return current session values that differ from the baseline."""

        names = set(self.baseline_parameters) | set(self.session_parameters)
        changed: dict[str, Any] = {}
        for name in names:
            baseline = self.baseline_parameters.get(name, UNSET)
            current = self.session_parameters.get(name, UNSET)
            if current != baseline:
                changed[name] = current
        return dict(sorted(changed.items()))

    def set_session_parameter(self, name: str, value: Any) -> None:
        self.session_parameters[name] = value

    def restore_session_parameter(self, name: str, baseline_value: Any = UNSET) -> None:
        if isinstance(baseline_value, _Unset):
            baseline_value = self.baseline_parameters.get(name, UNSET)
        if baseline_value is UNSET:
            self.session_parameters.pop(name, None)
        else:
            self.session_parameters[name] = baseline_value

    def reset_session_parameter(self, name: str | None = None) -> None:
        if name is None or name.upper() == "ALL":
            self.session_parameters.clear()
        else:
            self.session_parameters.pop(name, None)

    def set_transaction_parameter(
        self, name: str, value: Any, transaction_id: str | None = None
    ) -> None:
        tx_id = transaction_id or self.current_transaction_id
        if tx_id is None:
            tx_id = self.begin_transaction()
        self.transaction_parameters[name] = value
        tx = self.transactions.get(tx_id)
        if tx is not None and tx.is_open:
            tx.local_parameters[name] = value

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------
    def begin_transaction(self, transaction_id: str | None = None) -> str:
        if transaction_id is None:
            self._transaction_counter += 1
            transaction_id = f"txn-{self._transaction_counter}"
        if transaction_id not in self.transactions:
            self.transactions[transaction_id] = TransactionInfo(id=transaction_id)
            self.transaction_order.append(transaction_id)
        else:
            self.transactions[transaction_id].status = "open"
        return transaction_id

    def end_transaction(
        self,
        status: str,
        transaction_id: str | None = None,
        *,
        clear_local_parameters: bool = True,
    ) -> str | None:
        tx_id = transaction_id or self.current_transaction_id
        if tx_id is None:
            # Parsed end event can arrive without a separately observed begin.
            self._transaction_counter += 1
            tx_id = f"txn-{self._transaction_counter}"
            self.transactions[tx_id] = TransactionInfo(id=tx_id)
            self.transaction_order.append(tx_id)

        tx = self.transactions.get(tx_id)
        if tx is not None:
            tx.status = status
            if clear_local_parameters:
                for name in tx.local_parameters:
                    self.transaction_parameters.pop(name, None)
                tx.local_parameters.clear()

        if clear_local_parameters and status in {"committed", "rolled_back"}:
            # Defensive invariant: ended transactions do not leave LOCAL vars.
            open_ids = set(self.open_transactions)
            for name in list(self.transaction_parameters):
                owner_open = any(
                    name in self.transactions[txid].local_parameters for txid in open_ids
                )
                if not owner_open:
                    self.transaction_parameters.pop(name, None)
        return tx_id

    def commit_transaction(self, transaction_id: str | None = None) -> str | None:
        return self.end_transaction("committed", transaction_id)

    def rollback_transaction(self, transaction_id: str | None = None) -> str | None:
        return self.end_transaction("rolled_back", transaction_id)

    def mark_rollback_completed(self, transaction_id: str | None = None) -> None:
        """Apply a successful proxy ROLLBACK receipt to tracked state."""

        if transaction_id is None:
            for tx_id in list(self.open_transactions):
                self.rollback_transaction(tx_id)
            if self.has_open_transaction:
                self.rollback_transaction()
        else:
            self.rollback_transaction(transaction_id)
            # The generated connection-level rollback ends any other open tx
            # in the unusual multiple-id tracking case as well.
            if self.has_open_transaction:
                for tx_id in list(self.open_transactions):
                    self.rollback_transaction(tx_id)
        self.transaction_parameters.clear()

    @property
    def open_transactions(self) -> list[str]:
        return [
            tx_id
            for tx_id in self.transaction_order
            if tx_id in self.transactions and self.transactions[tx_id].is_open
        ]

    @property
    def has_open_transaction(self) -> bool:
        return bool(self.open_transactions)

    @property
    def current_transaction_id(self) -> str | None:
        open_ids = self.open_transactions
        return open_ids[-1] if open_ids else None

    @property
    def open_transaction_count(self) -> int:
        return len(self.open_transactions)

    # ------------------------------------------------------------------
    # Temporary objects
    # ------------------------------------------------------------------
    @staticmethod
    def _coerce_object(obj: Any) -> TemporaryObject:
        if isinstance(obj, TemporaryObject):
            return obj
        if isinstance(obj, Mapping):
            name = obj.get("name") or obj.get("table") or obj.get("table_name") or obj.get("object_name")
            if name is None:
                raise ValueError(f"temporary object event has no name: {obj!r}")
            object_type = (
                obj.get("object_type")
                or obj.get("type")
                or obj.get("kind")
                or "table"
            )
            if str(object_type) in {"temp_table", "temporary_table"}:
                object_type = "table"
            return TemporaryObject(
                name=str(name),
                object_type=str(object_type),
                schema=None if obj.get("schema") is None else str(obj.get("schema")),
            )
        if isinstance(obj, str):
            return TemporaryObject(name=obj)
        raise TypeError(f"unsupported temporary object value: {obj!r}")

    def create_temporary_object(
        self,
        name: Any = None,
        *,
        object_type: str = "table",
        schema: str | None = None,
    ) -> TemporaryObject:
        if isinstance(name, TemporaryObject):
            obj = name
        elif isinstance(name, Mapping):
            obj = self._coerce_object(name)
        elif name is not None:
            obj = TemporaryObject(name=str(name), object_type=object_type, schema=schema)
        else:
            raise ValueError("temporary object name is required")
        self.temporary_objects[obj.key] = obj
        return obj

    def drop_temporary_object(self, name: Any, *, schema: str | None = None) -> TemporaryObject | None:
        if isinstance(name, TemporaryObject):
            key = name.key
        elif isinstance(name, Mapping):
            key = self._coerce_object(name).key
        elif schema:
            key = f"{schema}.{name}"
        else:
            key = str(name)
        removed = self.temporary_objects.pop(key, None)
        if removed is None and "." not in key and schema is None:
            # Events may provide a qualified key while callers use bare name.
            for candidate_key, obj in list(self.temporary_objects.items()):
                if obj.name == key:
                    removed = self.temporary_objects.pop(candidate_key)
                    break
        return removed

    @property
    def temporary_tables(self) -> dict[str, TemporaryObject]:
        return {
            key: obj
            for key, obj in self.temporary_objects.items()
            if obj.object_type == "table"
        }

    # Concise vocabulary aliases used by some proxy codebases.
    @property
    def temp_tables(self) -> dict[str, TemporaryObject]:
        return self.temporary_tables

    @property
    def session_params(self) -> dict[str, Any]:
        return self.session_parameters

    @property
    def transaction_params(self) -> dict[str, Any]:
        return self.transaction_parameters

    @property
    def in_transaction(self) -> bool:
        return self.has_open_transaction

    def leftover_temporary_objects(self) -> list[TemporaryObject]:
        return [
            self.temporary_objects[key]
            for key in sorted(self.temporary_objects)
            if key not in self.baseline_temporary_objects
        ]

    # ------------------------------------------------------------------
    # Event application
    # ------------------------------------------------------------------
    def apply(self, event: StateEvent | Mapping[str, Any] | str) -> "SessionState":
        parsed = coerce_event(event)
        if parsed.event_id is not None:
            if parsed.event_id in self.applied_event_ids:
                return self
            self.applied_event_ids.add(parsed.event_id)
        self.event_count += 1

        event_type = parsed.type
        if event_type == EventType.BASELINE_RECEIVED:
            baseline = parsed.baseline_parameters or parsed.parameters
            if baseline is not None:
                self.set_baseline_parameters(baseline)
            if parsed.temporary_objects is not None:
                objects = (
                    parsed.temporary_objects.values()
                    if isinstance(parsed.temporary_objects, Mapping)
                    else parsed.temporary_objects
                )
                for obj in objects or []:
                    temporary = self._coerce_object(obj)
                    self.baseline_temporary_objects.add(temporary.key)
                    self.temporary_objects[temporary.key] = temporary
            return self

        if event_type == EventType.SESSION_STARTED:
            if parsed.parameters:
                self.set_baseline_parameters(parsed.parameters)
            return self

        if event_type == EventType.TRANSACTION_STARTED:
            self.begin_transaction(parsed.transaction_id)
            return self

        if event_type == EventType.TRANSACTION_COMMITTED:
            self.commit_transaction(parsed.transaction_id)
            return self

        if event_type == EventType.TRANSACTION_ROLLED_BACK:
            self.rollback_transaction(parsed.transaction_id)
            return self

        if event_type in {EventType.PARAMETER_SET, EventType.SESSION_PARAMETER_SET, EventType.TRANSACTION_PARAMETER_SET}:
            if parsed.name is None:
                if parsed.parameters:
                    for key, value in parsed.parameters.items():
                        self._set_named_parameter(event_type, str(key), value, parsed.scope, parsed.transaction_id)
                return self
            self._set_named_parameter(
                event_type, parsed.name, parsed.value, parsed.scope, parsed.transaction_id
            )
            return self

        if event_type == EventType.PARAMETER_RESET:
            scope = parsed.scope or "session"
            if scope in {"transaction", "local", "tx"}:
                if parsed.all_parameters or parsed.name is None:
                    self.transaction_parameters.clear()
                else:
                    self.transaction_parameters.pop(parsed.name, None)
            elif parsed.all_parameters or parsed.name is None:
                self.reset_session_parameter(None)
            else:
                self.reset_session_parameter(parsed.name)
            return self

        if event_type == EventType.TEMPORARY_OBJECT_CREATED:
            object_type = parsed.object_type or "table"
            self.create_temporary_object(
                parsed.name, object_type=object_type, schema=parsed.schema
            )
            return self

        if event_type == EventType.TEMPORARY_OBJECT_DROPPED:
            self.drop_temporary_object(parsed.name, schema=parsed.schema)
            return self

        # Unknown parsed events are ignored rather than guessing state effects.
        return self

    def _set_named_parameter(
        self,
        event_type: EventType | str,
        name: str,
        value: Any,
        scope: str | None,
        transaction_id: str | None,
    ) -> None:
        is_transaction = (
            event_type == EventType.TRANSACTION_PARAMETER_SET
            or scope in {"transaction", "local", "tx", "transaction_local"}
        )
        if is_transaction:
            if value is UNSET:
                # A SET event without a value is not enough to assign state.
                return
            self.set_transaction_parameter(name, value, transaction_id)
        else:
            if value is UNSET:
                return
            self.set_session_parameter(name, value)

    def handle(self, event: StateEvent | Mapping[str, Any] | str) -> "SessionState":
        return self.apply(event)

    process_event = apply
    ingest_event = apply
    reduce_event = apply
    record_event = apply

    # ------------------------------------------------------------------
    # Diff / serialisation
    # ------------------------------------------------------------------
    def is_at_baseline(self) -> bool:
        return (
            not self.changed_session_parameters()
            and not self.transaction_parameters
            and not self.has_open_transaction
            and not self.leftover_temporary_objects()
        )

    def snapshot(self) -> "SessionState":
        import copy

        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "baseline_parameters": {
                k: ("__UNSET__" if v is UNSET else v)
                for k, v in self.baseline_parameters.items()
            },
            "session_parameters": dict(self.session_parameters),
            "transaction_parameters": dict(self.transaction_parameters),
            "open_transactions": list(self.open_transactions),
            "temporary_objects": {
                key: obj.to_dict() for key, obj in self.temporary_objects.items()
            },
            "leftover_temporary_objects": [
                obj.key for obj in self.leftover_temporary_objects()
            ],
            "changed_session_parameters": self.changed_session_parameters(),
        }
