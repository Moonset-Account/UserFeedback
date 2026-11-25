"""Normalisation for already-parsed database/proxy state events.

The recycler does not parse SQL or connect to a database.  Inputs are parsed
state events, supplied as :class:`StateEvent` objects or ordinary mappings.
The aliases make the small event vocabulary tolerant of common naming styles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .model import UNSET


class EventType(str, Enum):
    BASELINE_RECEIVED = "baseline_received"
    SESSION_STARTED = "session_started"

    TRANSACTION_STARTED = "transaction_started"
    TRANSACTION_COMMITTED = "transaction_committed"
    TRANSACTION_ROLLED_BACK = "transaction_rolled_back"

    PARAMETER_SET = "parameter_set"
    SESSION_PARAMETER_SET = "session_parameter_set"
    TRANSACTION_PARAMETER_SET = "transaction_parameter_set"
    PARAMETER_RESET = "parameter_reset"

    TEMPORARY_OBJECT_CREATED = "temporary_object_created"
    TEMPORARY_OBJECT_DROPPED = "temporary_object_dropped"


def normalize_token(value: Any) -> str:
    text = str(value).strip().lower()
    return re.sub(r"[\s.\-/]+", "_", text)


_EVENT_ALIASES: dict[str, EventType] = {
    "baseline": EventType.BASELINE_RECEIVED,
    "baseline_snapshot": EventType.BASELINE_RECEIVED,
    "initial_state": EventType.BASELINE_RECEIVED,
    "session_baseline": EventType.BASELINE_RECEIVED,
    "baseline_received": EventType.BASELINE_RECEIVED,
    "connected": EventType.SESSION_STARTED,
    "connection_opened": EventType.SESSION_STARTED,
    "session_start": EventType.SESSION_STARTED,
    "session_started": EventType.SESSION_STARTED,

    "begin": EventType.TRANSACTION_STARTED,
    "start_transaction": EventType.TRANSACTION_STARTED,
    "transaction_begin": EventType.TRANSACTION_STARTED,
    "transaction_start": EventType.TRANSACTION_STARTED,
    "transaction_started": EventType.TRANSACTION_STARTED,
    "tx_started": EventType.TRANSACTION_STARTED,
    "xact_started": EventType.TRANSACTION_STARTED,

    "commit": EventType.TRANSACTION_COMMITTED,
    "transaction_commit": EventType.TRANSACTION_COMMITTED,
    "transaction_committed": EventType.TRANSACTION_COMMITTED,
    "tx_committed": EventType.TRANSACTION_COMMITTED,
    "xact_committed": EventType.TRANSACTION_COMMITTED,

    "rollback": EventType.TRANSACTION_ROLLED_BACK,
    "abort": EventType.TRANSACTION_ROLLED_BACK,
    "transaction_rollback": EventType.TRANSACTION_ROLLED_BACK,
    "transaction_rolled_back": EventType.TRANSACTION_ROLLED_BACK,
    "tx_rolled_back": EventType.TRANSACTION_ROLLED_BACK,
    "xact_rolled_back": EventType.TRANSACTION_ROLLED_BACK,

    "set": EventType.PARAMETER_SET,
    "parameter_set": EventType.PARAMETER_SET,
    "param_set": EventType.PARAMETER_SET,
    "setting_changed": EventType.PARAMETER_SET,
    "session_set": EventType.SESSION_PARAMETER_SET,
    "set_session": EventType.SESSION_PARAMETER_SET,
    "session_parameter_set": EventType.SESSION_PARAMETER_SET,
    "session_param_set": EventType.SESSION_PARAMETER_SET,

    "set_local": EventType.TRANSACTION_PARAMETER_SET,
    "local_set": EventType.TRANSACTION_PARAMETER_SET,
    "local_parameter_set": EventType.TRANSACTION_PARAMETER_SET,
    "transaction_parameter_set": EventType.TRANSACTION_PARAMETER_SET,
    "transaction_param_set": EventType.TRANSACTION_PARAMETER_SET,
    "tx_parameter_set": EventType.TRANSACTION_PARAMETER_SET,

    "reset": EventType.PARAMETER_RESET,
    "parameter_reset": EventType.PARAMETER_RESET,
    "param_reset": EventType.PARAMETER_RESET,
    "reset_parameter": EventType.PARAMETER_RESET,

    "temp_table_created": EventType.TEMPORARY_OBJECT_CREATED,
    "temporary_table_created": EventType.TEMPORARY_OBJECT_CREATED,
    "create_temp_table": EventType.TEMPORARY_OBJECT_CREATED,
    "create_temporary_table": EventType.TEMPORARY_OBJECT_CREATED,
    "temp_object_created": EventType.TEMPORARY_OBJECT_CREATED,
    "temporary_object_created": EventType.TEMPORARY_OBJECT_CREATED,
    "temp_created": EventType.TEMPORARY_OBJECT_CREATED,

    "temp_table_dropped": EventType.TEMPORARY_OBJECT_DROPPED,
    "temporary_table_dropped": EventType.TEMPORARY_OBJECT_DROPPED,
    "drop_temp_table": EventType.TEMPORARY_OBJECT_DROPPED,
    "drop_temporary_table": EventType.TEMPORARY_OBJECT_DROPPED,
    "temp_object_dropped": EventType.TEMPORARY_OBJECT_DROPPED,
    "temporary_object_dropped": EventType.TEMPORARY_OBJECT_DROPPED,
    "temp_dropped": EventType.TEMPORARY_OBJECT_DROPPED,
}

_NAME_KEYS = (
    "name",
    "parameter",
    "parameter_name",
    "param",
    "key",
    "table",
    "table_name",
    "object_name",
    "relation",
)
_VALUE_KEYS = ("value", "new_value", "setting", "val", "to")
_SCOPE_KEYS = ("scope", "parameter_scope", "param_scope", "level")
_TX_KEYS = ("transaction_id", "tx_id", "txid", "xid", "xact_id")
_OBJECT_TYPE_KEYS = ("object_type", "object_kind", "relation_type")
_SCHEMA_KEYS = ("schema", "schema_name", "namespace")


@dataclass
class StateEvent:
    """Normalised parsed state event."""

    type: EventType | str
    connection_id: str | None = None
    name: str | None = None
    value: Any = UNSET
    scope: str | None = None
    transaction_id: str | None = None
    object_type: str | None = None
    schema: str | None = None
    parameters: dict[str, Any] | None = None
    baseline_parameters: dict[str, Any] | None = None
    temporary_objects: list[Any] | dict[str, Any] | None = None
    all_parameters: bool = False
    event_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type, EventType):
            normalized = _EVENT_ALIASES.get(normalize_token(self.type))
            if normalized is not None:
                self.type = normalized
        if self.scope is not None:
            self.scope = normalize_token(self.scope)
        if self.object_type is not None:
            self.object_type = normalize_token(self.object_type)

    @property
    def event_type(self) -> str:
        return self.type.value if isinstance(self.type, EventType) else str(self.type)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "StateEvent":
        raw = dict(data)
        event_type_value = (
            data.get("type")
            or data.get("event_type")
            or data.get("event")
            or data.get("kind")
            or data.get("name_event")
        )
        if event_type_value is None:
            raise ValueError("state event is missing type/event_type/event/kind")

        event_type = _EVENT_ALIASES.get(normalize_token(event_type_value))
        if event_type is None:
            # Preserve unknown types so callers can handle parser extensions.
            event_type = normalize_token(event_type_value)

        name = _first_present(data, _NAME_KEYS)
        if name is not None:
            name = str(name)

        value = UNSET
        for key in _VALUE_KEYS:
            if key in data:
                value = data[key]
                break

        scope = _first_present(data, _SCOPE_KEYS)
        transaction_id = _first_present(data, _TX_KEYS)
        object_type = _first_present(data, _OBJECT_TYPE_KEYS)
        schema = _first_present(data, _SCHEMA_KEYS)

        parameters = data.get("parameters") or data.get("settings") or data.get("vars")
        baseline_parameters = (
            data.get("baseline_parameters")
            or data.get("baseline")
            or data.get("initial_parameters")
        )
        temporary_objects = (
            data.get("temporary_objects")
            or data.get("temp_tables")
            or data.get("temporary_tables")
            or data.get("objects")
        )

        connection_ref = data.get("connection_id") or data.get("conn_id")
        return cls(
            type=event_type,
            connection_id=_optional_str(connection_ref),
            name=name,
            value=value,
            scope=_optional_str(scope),
            transaction_id=_optional_str(transaction_id),
            object_type=_optional_str(object_type),
            schema=_optional_str(schema),
            parameters=dict(parameters) if isinstance(parameters, Mapping) else parameters,  # type: ignore[arg-type]
            baseline_parameters=(
                dict(baseline_parameters)
                if isinstance(baseline_parameters, Mapping)
                else baseline_parameters
            ),
            temporary_objects=temporary_objects,  # type: ignore[arg-type]
            all_parameters=bool(data.get("all") or data.get("reset_all") or False),
            event_id=_optional_str(
                data.get("event_id") or data.get("sequence") or data.get("lsn") or data.get("id")
            ),
            raw=raw,
        )


def _first_present(data: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def coerce_event(event: StateEvent | Mapping[str, Any] | str | Any) -> StateEvent:
    if isinstance(event, StateEvent):
        return event
    if isinstance(event, Mapping):
        return StateEvent.from_mapping(event)
    if isinstance(event, str):
        return StateEvent(type=event)

    # Tolerate ordinary parsed event objects such as dataclasses, attrs
    # instances, or named tuples without taking a dependency on them.
    if hasattr(event, "_asdict"):
        return StateEvent.from_mapping(event._asdict())
    if hasattr(event, "__dict__"):
        data = {
            key: value
            for key, value in vars(event).items()
            if not key.startswith("_") and not callable(value)
        }
        if data:
            return StateEvent.from_mapping(data)

    raise TypeError(f"unsupported state event type: {type(event)!r}")
