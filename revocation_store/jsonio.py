"""JSON (de)serialisation for tables, used by the CLI and fixtures."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from .model import Table, TableEntry, TableValidationError, isoformat_utc, parse_reason


def _parse_time(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        raise TableValidationError(f"{field_name} must be an ISO-8601 string")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TableValidationError(
            f"{field_name} is not a valid ISO-8601 timestamp: {value!r}"
        ) from exc
    return parsed


def _parse_entry(raw: Any, index: int) -> TableEntry:
    if not isinstance(raw, Mapping):
        raise TableValidationError(f"entries[{index}] must be an object")
    if "serial" not in raw:
        raise TableValidationError(f"entries[{index}] is missing 'serial'")
    reason = raw.get("reason", "unspecified")
    revocation_raw = raw.get("revocation_time")
    revocation_time = (
        _parse_time(revocation_raw, f"entries[{index}].revocation_time")
        if revocation_raw is not None
        else None
    )
    return TableEntry(
        serial=str(raw["serial"]),
        reason=parse_reason(reason),
        revocation_time=revocation_time,
    )


def table_from_dict(data: Mapping[str, Any]) -> Table:
    if not isinstance(data, Mapping):
        raise TableValidationError("a table must be a JSON object")

    kind = data.get("kind")
    if kind is None:
        raise TableValidationError("table is missing 'kind' (FULL or DELTA)")

    required = ("issuer", "scope", "number", "this_update", "next_update")
    missing = [name for name in required if name not in data]
    if missing:
        raise TableValidationError(f"table is missing required field(s): {', '.join(missing)}")

    entries_raw = data.get("entries", [])
    if not isinstance(entries_raw, Sequence) or isinstance(entries_raw, (str, bytes)):
        raise TableValidationError("'entries' must be a list")

    kind_text = str(kind).upper()

    common = dict(
        issuer=str(data["issuer"]),
        scope=str(data["scope"]),
        number=int(data["number"]),
        this_update=_parse_time(data["this_update"], "this_update"),
        next_update=_parse_time(data["next_update"], "next_update"),
        entries=tuple(_parse_entry(item, index) for index, item in enumerate(entries_raw)),
    )
    if kind_text == "FULL":
        # A full table is its own baseline; a stray base_number is ignored.
        return Table.full(**common)
    if kind_text == "DELTA":
        if "base_number" not in data:
            raise TableValidationError("a DELTA table must name its 'base_number'")
        return Table.delta(base_number=int(data["base_number"]), **common)
    raise TableValidationError(f"unknown table kind: {kind!r}")


def table_to_dict(table: Table) -> dict:
    return {
        "kind": table.kind.value,
        "issuer": table.issuer,
        "scope": table.scope,
        "base_number": table.base_number,
        "number": table.number,
        "this_update": isoformat_utc(table.this_update),
        "next_update": isoformat_utc(table.next_update),
        "entries": [
            {
                "serial": entry.serial,
                "reason": entry.reason.value,
                "revocation_time": (
                    isoformat_utc(entry.revocation_time)
                    if entry.revocation_time is not None
                    else None
                ),
            }
            for entry in table.entries
        ],
    }


def tables_from_json(payload: Any) -> list[Table]:
    if isinstance(payload, Mapping):
        return [table_from_dict(payload)]
    if isinstance(payload, list):
        return [table_from_dict(item) for item in payload]
    raise TableValidationError("expected a table object or an array of tables")
