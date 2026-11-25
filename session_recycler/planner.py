"""Produce deterministic restoration plans from tracked session state."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import ActionKind, SuppressedAction, UNSET
from .state import SessionState, TemporaryObject


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_value(value: Any) -> str:
    if value is UNSET or value is None:
        return "DEFAULT"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    upper = text.upper()
    if upper in {"ON", "OFF", "TRUE", "FALSE", "DEFAULT"}:
        return upper
    return "'" + text.replace("'", "''") + "'"


@dataclass
class PlannedAction:
    sequence: int
    kind: ActionKind
    key: str
    target: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class RecoveryPlan:
    actions: list[PlannedAction]
    suppressed: list[SuppressedAction]

    @property
    def is_empty(self) -> bool:
        return not self.actions

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [
                {
                    "sequence": a.sequence,
                    "kind": a.kind.value,
                    "key": a.key,
                    "target": a.target,
                    "arguments": a.arguments,
                }
                for a in self.actions
            ],
            "suppressed": [s.to_dict() for s in self.suppressed],
        }


class RecoveryPlanner:
    """Build the required reset plan.

    Ordering is intentional:

    1. end an open/uncommitted transaction with ``ROLLBACK``;
    2. restore session parameters that still differ from baseline;
    3. drop temporary objects that are still present.

    Transaction-local parameters are not emitted as reset actions when the
    rollback action is present: a successful rollback discards them.  They are
    exposed in ``suppressed`` for auditability.
    """

    def plan(self, state: SessionState) -> RecoveryPlan:
        actions: list[PlannedAction] = []
        suppressed: list[SuppressedAction] = []
        sequence = 1

        if state.has_open_transaction:
            tx_id = state.current_transaction_id
            actions.append(
                PlannedAction(
                    sequence=sequence,
                    kind=ActionKind.ROLLBACK_TRANSACTION,
                    key="rollback",
                    target=tx_id,
                    arguments={
                        "sql": "ROLLBACK",
                        "open_transactions": list(state.open_transactions),
                    },
                )
            )
            sequence += 1

            for name in sorted(state.transaction_parameters):
                suppressed.append(
                    SuppressedAction(
                        kind=ActionKind.RESET_TRANSACTION_PARAMETER,
                        target=name,
                        reason="rollback_discards_transaction_local_parameter",
                        details={"value": state.transaction_parameters[name]},
                    )
                )
        elif state.transaction_parameters:
            # Defensive edge case: a parser may report local parameters without
            # a recognizable begin/end pair.  They cannot safely be assumed to
            # have been discarded, so make the cleanup explicit.  Normal event
            # streams keep this map empty after commit/rollback.
            for name in sorted(state.transaction_parameters):
                actions.append(
                    PlannedAction(
                        sequence=sequence,
                        kind=ActionKind.RESET_TRANSACTION_PARAMETER,
                        key=f"tx_param:{name}",
                        target=name,
                        arguments={
                            "sql": f"RESET {quote_identifier(name)}",
                            "value": state.transaction_parameters[name],
                        },
                    )
                )
                sequence += 1

        changed = state.changed_session_parameters()
        for name in sorted(changed):
            current = changed[name]
            baseline = state.baseline_parameters.get(name, UNSET)
            if baseline is UNSET:
                sql = f"RESET {quote_identifier(name)}"
                baseline_value = None
                baseline_is_default = True
            else:
                sql = f"SET {quote_identifier(name)} = {quote_value(baseline)}"
                baseline_value = baseline
                baseline_is_default = False

            actions.append(
                PlannedAction(
                    sequence=sequence,
                    kind=ActionKind.RESTORE_SESSION_PARAMETER,
                    key=f"param:{name}",
                    target=name,
                    arguments={
                        "sql": sql,
                        "parameter": name,
                        "current_value": current,
                        "baseline_value": baseline_value,
                        "baseline_is_default": baseline_is_default,
                    },
                )
            )
            sequence += 1

        for obj in state.leftover_temporary_objects():
            actions.append(
                PlannedAction(
                    sequence=sequence,
                    kind=ActionKind.DROP_TEMPORARY_OBJECT,
                    key=f"temp:{obj.key}",
                    target=obj.key,
                    arguments={
                        "sql": self._drop_sql(obj),
                        "object_name": obj.name,
                        "object_key": obj.key,
                        "object_type": obj.object_type,
                        "schema": obj.schema,
                        "temporary": True,
                    },
                )
            )
            sequence += 1

        return RecoveryPlan(actions=actions, suppressed=suppressed)

    @staticmethod
    def _drop_sql(obj: TemporaryObject) -> str:
        if obj.schema:
            name = f"{quote_identifier(obj.schema)}.{quote_identifier(obj.name)}"
        else:
            name = quote_identifier(obj.name)
        object_type = "TABLE" if obj.object_type == "table" else obj.object_type.upper()
        return f"DROP {object_type} {name}"


def plan_recovery(state: SessionState) -> RecoveryPlan:
    return RecoveryPlanner().plan(state)
