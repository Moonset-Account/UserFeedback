"""Configured resource boundaries for the local importer.

All values are inclusive upper bounds: a value equal to the configured limit is
accepted, and only a value strictly greater than it is rejected.  Measurements
are taken from the actually-read bytes and the actually-parsed JSON, never from
metadata declared inside a record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .errors import LimitsConfigError

#: Defaults chosen to be small enough for synthetic local fixtures.
DEFAULT_LIMIT_VALUES: dict[str, int] = {
    "max_file_bytes": 1_048_576,
    "max_record_bytes": 8_192,
    "max_object_depth": 6,
    "max_list_items": 100,
}

KNOWN_LIMIT_KEYS = frozenset(DEFAULT_LIMIT_VALUES)

MAX_LIMITS_CONFIG_BYTES = 65_536


@dataclass(frozen=True)
class Limits:
    """Inclusive resource boundaries applied to input files and records."""

    max_file_bytes: int = DEFAULT_LIMIT_VALUES["max_file_bytes"]
    max_record_bytes: int = DEFAULT_LIMIT_VALUES["max_record_bytes"]
    max_object_depth: int = DEFAULT_LIMIT_VALUES["max_object_depth"]
    max_list_items: int = DEFAULT_LIMIT_VALUES["max_list_items"]

    def to_dict(self) -> dict[str, int]:
        return {
            "max_file_bytes": self.max_file_bytes,
            "max_record_bytes": self.max_record_bytes,
            "max_object_depth": self.max_object_depth,
            "max_list_items": self.max_list_items,
        }


def load_limits(path: str | None = None) -> Limits:
    """Load limits from an optional JSON object file.

    ``None`` returns the built-in defaults.  A non-object document, unknown
    keys, and non-positive integer values are rejected; booleans are not
    accepted as integers.
    """

    if path is None:
        return Limits()

    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise LimitsConfigError(
            f"cannot read limits file {path!r}: {exc.strerror or exc}"
        ) from exc

    if len(raw) > MAX_LIMITS_CONFIG_BYTES:
        raise LimitsConfigError(
            f"limits file exceeds {MAX_LIMITS_CONFIG_BYTES} bytes"
        )

    try:
        text = raw.decode("utf-8-sig")
        payload = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LimitsConfigError(f"limits file is not valid UTF-8 JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise LimitsConfigError("limits file must contain a JSON object")

    unknown = sorted(set(payload) - KNOWN_LIMIT_KEYS)
    if unknown:
        raise LimitsConfigError(
            "unknown limits key(s): " + ", ".join(repr(key) for key in unknown)
        )

    for key, value in payload.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise LimitsConfigError(
                f"limits key {key!r} must be a positive integer, got {value!r}"
            )

    return Limits(**payload)
