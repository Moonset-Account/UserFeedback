"""Per-record JSON decoding with duplicate-key and structural diagnostics.

Measurements come from the bytes that were actually read and the structure that
was actually parsed.  Nothing declared inside the record can raise a limit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import paths
from .errors import (
    DEPTH_LIMIT,
    DUPLICATE_FIELD,
    INVALID_ENCODING,
    INVALID_JSON,
    LIST_ITEM_LIMIT,
    RECORD_SHAPE,
    RECORD_SIZE_LIMIT,
    UNSUPPORTED_VALUE,
    ImportFailure,
)
from .limits import Limits


class PairsObject(dict):
    """A parsed JSON object that remembers duplicated member names."""

    __slots__ = ("duplicate_keys",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.duplicate_keys: list[str] = []


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> PairsObject:
    obj = PairsObject()
    for key, value in pairs:
        if key in obj:
            obj.duplicate_keys.append(key)
        obj[key] = value
    return obj


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def parse_record_object(text: str, line: int) -> dict[str, Any]:
    """Parse one record's text into a top-level JSON object."""

    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_pairs_hook,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ImportFailure(
            INVALID_JSON,
            "$",
            f"invalid JSON: {exc.msg}",
            line=line,
            column=exc.colno,
        ) from exc
    except ValueError as exc:  # NaN / Infinity / -Infinity
        raise ImportFailure(UNSUPPORTED_VALUE, "$", str(exc), line=line) from exc
    except RecursionError as exc:
        # The standard parser gives up before this importer can measure depth;
        # the record is still refused and other records keep going.
        raise ImportFailure(
            DEPTH_LIMIT,
            "$",
            "record nesting exceeded the JSON parser's recursion capacity",
            line=line,
        ) from exc

    if not isinstance(value, dict):
        raise ImportFailure(
            RECORD_SHAPE,
            "$",
            f"record must be a JSON object, got {type(value).__name__}",
            line=line,
        )
    return value


def scan_structure(record: dict[str, Any], limits: Limits) -> None:
    """Enforce container depth and per-list item count.

    Root object is depth 1; every nested object or list adds one.  Lists are
    checked individually: a list with more than ``max_list_items`` items fails,
    and several lists each within the bound are all accepted.  The traversal is
    iterative so an offending record cannot crash the process through nesting.
    """

    stack: list[tuple[Any, str, int]] = [(record, "$", 1)]

    while stack:
        node, path, depth = stack.pop()

        if isinstance(node, PairsObject) and node.duplicate_keys:
            key = node.duplicate_keys[0]
            raise ImportFailure(
                DUPLICATE_FIELD,
                paths.child(path, key),
                f"duplicate field {key!r}; the last value silently won",
            )

        if isinstance(node, dict):
            if depth > limits.max_object_depth:
                raise ImportFailure(
                    DEPTH_LIMIT,
                    path,
                    f"object depth {depth} exceeds max_object_depth "
                    f"{limits.max_object_depth}",
                )
            children = [(v, paths.child(path, k), depth + 1) for k, v in node.items()]
            stack.extend(reversed(children))
        elif isinstance(node, list):
            if depth > limits.max_object_depth:
                raise ImportFailure(
                    DEPTH_LIMIT,
                    path,
                    f"object depth {depth} exceeds max_object_depth "
                    f"{limits.max_object_depth}",
                )
            if len(node) > limits.max_list_items:
                raise ImportFailure(
                    LIST_ITEM_LIMIT,
                    path,
                    f"list has {len(node)} items, exceeds max_list_items "
                    f"{limits.max_list_items}",
                )
            children = [
                (item, paths.index(path, position), depth + 1)
                for position, item in enumerate(node)
            ]
            stack.extend(reversed(children))


@dataclass
class ParsedRecord:
    """A record that parsed and stayed within structural bounds."""

    line: int
    byte_length: int
    payload: dict[str, Any] = field(repr=False)
    record_id: str | None = None


def parse_and_scan(
    raw: bytes, line: int, limits: Limits
) -> ParsedRecord:
    """Decode one record's raw bytes, parse it, and enforce structural bounds."""

    if len(raw) > limits.max_record_bytes:
        raise ImportFailure(
            RECORD_SIZE_LIMIT,
            "$",
            f"record is {len(raw)} bytes, exceeds max_record_bytes "
            f"{limits.max_record_bytes}",
            line=line,
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImportFailure(
            INVALID_ENCODING,
            "$",
            f"record is not valid UTF-8 (byte offset {exc.start})",
            line=line,
            column=exc.start + 1,
        ) from exc

    payload = parse_record_object(text, line)
    scan_structure(payload, limits)

    record_id = None
    for key in ("record_id", "task_id"):
        candidate = payload.get(key)
        if isinstance(candidate, str) and candidate:
            record_id = candidate
            break

    return ParsedRecord(line=line, byte_length=len(raw), payload=payload, record_id=record_id)
