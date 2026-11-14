"""Import orchestration and the JSON report format."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    INPUT_SIZE_LIMIT,
    INPUT_UNREADABLE,
    INVALID_ENCODING,
    RECORD_DECODE_ERROR,
    ImportFailure,
)
from .limits import Limits
from .registry import _decode_record, resolve_task_spec
from .split import split_records
from .structure import parse_and_scan

REPORT_TOOL = "task-whitelist-importer"
REPORT_SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_RECORD_FAILURES = 1
EXIT_INPUT_ERROR = 2

#: File-level failures leave no imported records at all.
FILE_LEVEL_CLASSIFICATIONS = frozenset(
    {INPUT_UNREADABLE, INPUT_SIZE_LIMIT, INVALID_ENCODING}
)


@dataclass
class Acceptance:
    line: int
    record_id: str | None
    version: str
    declared_task_type: str
    canonical_task_type: str
    data: dict[str, Any]
    extensions: dict[str, Any]
    byte_length: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "accepted",
            "line": self.line,
            "record_id": self.record_id,
            "version": self.version,
            "declared_task_type": self.declared_task_type,
            "canonical_task_type": self.canonical_task_type,
            "byte_length": self.byte_length,
            "data": self.data,
            "extensions": self.extensions,
        }


def import_bytes(
    raw: bytes, limits: Limits = Limits(), source: str = "<memory>"
) -> dict[str, Any]:
    """Import all records in ``raw`` and return the report dictionary."""

    if len(raw) > limits.max_file_bytes:
        failure = ImportFailure(
            INPUT_SIZE_LIMIT,
            "$",
            f"input is {len(raw)} bytes, exceeds max_file_bytes "
            f"{limits.max_file_bytes}",
        )
        return _report(source, limits, [], [failure], input_bytes=len(raw))

    try:
        split = split_records(raw, limits)
    except ImportFailure as exc:
        return _report(source, limits, [], [exc], input_bytes=len(raw))

    accepted: list[Acceptance] = []
    rejected: list[ImportFailure] = list(split.failures)

    for chunk in split.chunks:
        try:
            parsed = parse_and_scan(chunk.raw, chunk.line, limits)
            spec = resolve_task_spec(parsed.payload)
            data, extensions = _decode_record(parsed.payload, spec)
        except ImportFailure as failure:
            if failure.line is None:
                failure.line = chunk.line
            if failure.record_id is None:
                failure.record_id = _best_effort_id(chunk.raw)
            rejected.append(failure)
            continue
        except Exception as exc:  # defensive: one bad record must not stop others
            rejected.append(
                ImportFailure(
                    RECORD_DECODE_ERROR,
                    "$",
                    f"unexpected {type(exc).__name__}: {exc}",
                    line=chunk.line,
                    record_id=_best_effort_id(chunk.raw),
                )
            )
            continue

        accepted.append(
            Acceptance(
                line=parsed.line,
                record_id=parsed.record_id,
                version=spec.version,
                declared_task_type=spec.declared_task_type,
                canonical_task_type=spec.canonical_task_type,
                data=data,
                extensions=extensions,
                byte_length=parsed.byte_length,
            )
        )

    return _report(
        source, limits, accepted, rejected, input_bytes=len(raw)
    )


def _best_effort_id(raw: bytes) -> str | None:
    """Recover an id for reporting when the record failed before decoding."""

    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    for key in ("record_id", "task_id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _report(
    source: str,
    limits: Limits,
    accepted: list[Acceptance],
    rejected: list[ImportFailure],
    input_bytes: int,
) -> dict[str, Any]:
    records = [item.to_dict() for item in accepted]
    failures = [item.to_dict() for item in rejected]
    return {
        "tool": REPORT_TOOL,
        "schema_version": REPORT_SCHEMA_VERSION,
        "source": source,
        "limits": limits.to_dict(),
        "input_bytes": input_bytes,
        "summary": {
            "records_seen": len(records) + len(failures),
            "accepted": len(records),
            "rejected": len(failures),
        },
        "records": records,
        "failures": failures,
    }


def exit_code(report: dict[str, Any]) -> int:
    """0 when every record was accepted; 1 for record failures; 2 for input failure."""

    failures = report.get("failures", [])
    if not failures:
        return EXIT_OK
    if all(f["classification"] in FILE_LEVEL_CLASSIFICATIONS for f in failures):
        return EXIT_INPUT_ERROR
    return EXIT_RECORD_FAILURES


def import_file(
    path: str | Path, limits: Limits = Limits()
) -> dict[str, Any]:
    """Read and import one file, mapping unreadable input to a report failure."""

    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        failure = ImportFailure(
            INPUT_UNREADABLE,
            "$",
            f"cannot read {str(path)!r}: {exc.strerror or exc}",
        )
        return _report(str(path), limits, [], [failure], input_bytes=0)
    return import_bytes(raw, limits=limits, source=str(path))


def render(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
