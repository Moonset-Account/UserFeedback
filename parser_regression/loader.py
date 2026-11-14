"""Loading and validating structured test-result exports.

The accepted document shape is documented in ``README.md``.  Validation is
deliberately strict: a regression summary built from ambiguous input would be
worse than a clear load error, so every rejected input raises ``LoadError``
with the offending record index and field path.
"""

import json
from typing import Any, Dict, Mapping, Optional, Tuple

from .identity import IdentityError, resolve_identity, summarize
from .models import FAILED, PASSED, SKIPPED, Report, STATUS_VALUES, TestRecord

TOP_LEVEL_KEYS = {"build_id", "source", "results"}
RECORD_KEYS = {
    "sample_name",
    "parse_mode",
    "environment",
    "build_id",
    "attempt",
    "status",
    "failure_category",
    "content",
    "content_hash",
    "summary",
}


class LoadError(ValueError):
    """Input export cannot be interpreted as a trustworthy result set."""


def _fail(where: str, message: str) -> "LoadError":
    return LoadError(f"{where}: {message}")


def _require_text(container: Mapping[str, Any], key: str, where: str,
                  allow_empty: bool = False) -> str:
    if key not in container:
        raise _fail(where, f"missing required field {key!r}")
    value = container[key]
    if not isinstance(value, str):
        raise _fail(where, f"field {key!r} must be a string")
    if not allow_empty and not value.strip():
        raise _fail(where, f"field {key!r} must not be blank")
    return value


def _optional_text(container: Mapping[str, Any], key: str,
                   where: str) -> Optional[str]:
    if key not in container or container[key] is None:
        return None
    value = container[key]
    if not isinstance(value, str):
        raise _fail(where, f"field {key!r} must be a string or null")
    if not value.strip():
        raise _fail(where, f"field {key!r} must not be blank when present")
    return value


def _parse_attempt(raw: Any, where: str) -> int:
    if isinstance(raw, bool):
        raise _fail(where, "field 'attempt' must be a positive integer, not a boolean")
    if isinstance(raw, float):
        if raw.is_integer():
            raw = int(raw)
        else:
            raise _fail(where, "field 'attempt' must be a whole positive integer")
    if not isinstance(raw, int):
        raise _fail(where, "field 'attempt' must be a positive integer")
    if raw < 1:
        raise _fail(where, "field 'attempt' must be >= 1")
    return raw


def _parse_record(raw: Any, index: int, top_build_id: str) -> TestRecord:
    where = f"results[{index}]"
    if not isinstance(raw, Mapping):
        raise _fail(where, "each result must be a JSON object")
    unknown = sorted(set(raw) - RECORD_KEYS)
    if unknown:
        raise _fail(where, f"unknown field(s): {', '.join(unknown)}")

    sample_name = _require_text(raw, "sample_name", where)
    parse_mode = _require_text(raw, "parse_mode", where)
    environment = _require_text(raw, "environment", where)

    record_build = _optional_text(raw, "build_id", where)
    if record_build is not None and record_build.strip() != top_build_id:
        raise _fail(
            where,
            "per-result 'build_id' must equal the document build_id "
            f"({top_build_id!r}); one export describes exactly one build",
        )

    attempt = _parse_attempt(raw.get("attempt"), where)

    status_raw = _require_text(raw, "status", where)
    status = status_raw.strip().casefold()
    if status not in STATUS_VALUES:
        raise _fail(where, f"status must be one of {', '.join(STATUS_VALUES)}")

    failure_category = _optional_text(raw, "failure_category", where)
    if status == FAILED and failure_category is None:
        raise _fail(where, "failed results must declare a failure_category")
    if status != FAILED and failure_category is not None:
        raise _fail(
            where,
            "only failed results may declare failure_category "
            f"(status is {status!r})",
        )

    content = _optional_text(raw, "content", where)
    content_hash = _optional_text(raw, "content_hash", where)
    explicit_summary = _optional_text(raw, "summary", where)

    try:
        identity, basis = resolve_identity(sample_name, content, content_hash)
    except IdentityError as exc:
        raise _fail(where, str(exc)) from exc

    summary = summarize(content, explicit_summary, sample_name)

    return TestRecord(
        sample_name=sample_name.strip(),
        parse_mode=parse_mode.strip(),
        environment=environment.strip(),
        build_id=top_build_id,
        attempt=attempt,
        status=status,
        failure_category=(failure_category.strip().casefold()
                          if failure_category is not None else None),
        content=content,
        content_hash=content_hash,
        summary=summary,
        identity=identity,
        identity_basis=basis,
    )


def parse_report(document: Any, source: str = "<inline>") -> Report:
    """Validate an already-decoded JSON document and build a ``Report``."""
    if not isinstance(document, Mapping):
        raise _fail(source, "top-level JSON value must be an object")
    unknown = sorted(set(document) - TOP_LEVEL_KEYS)
    if unknown:
        raise _fail(source, f"unknown top-level field(s): {', '.join(unknown)}")

    build_id = _require_text(document, "build_id", source)
    if "results" not in document:
        raise _fail(source, "missing required field 'results'")
    results = document["results"]
    if not isinstance(results, list):
        raise _fail(source, "field 'results' must be a JSON array")
    if not results:
        raise _fail(source, "field 'results' must contain at least one result")

    source_note = _optional_text(document, "source", source) or source

    records = tuple(
        _parse_record(entry, index, build_id)
        for index, entry in enumerate(results)
    )
    return Report(build_id=build_id, records=records, source=source_note,
                  path=source)


def load_report(path: str) -> Report:
    """Read a UTF-8 JSON export from ``path`` and validate it."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise LoadError(f"{path}: cannot read file: {exc}") from exc

    try:
        document = json.loads(text)
    except UnicodeDecodeError as exc:
        raise LoadError(f"{path}: file is not valid UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise LoadError(
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc

    return parse_report(document, source=path)
