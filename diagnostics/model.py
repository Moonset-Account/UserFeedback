"""Load and validate the JSON dataset that feeds the diagnostics page.

The dataset is intentionally small: a title plus a flat list of records.
Every record has a stable id, a group heading, a plain-text summary and an
optional preformatted detail block and an optional documentation link.

All text is treated as data, never as markup.  Line endings are normalised to
LF (HTML parsers normalise them anyway); NUL is rejected because HTML text
nodes cannot represent it faithfully.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DataError(ValueError):
    """Raised when the JSON dataset does not match the documented schema."""


@dataclass(frozen=True)
class DocLink:
    """One optional documentation link attached to a record."""

    label: str
    url: str


@dataclass(frozen=True)
class Record:
    record_id: str
    group: str
    summary: str
    details: str | None
    doc: DocLink | None


@dataclass(frozen=True)
class Dataset:
    title: str
    records: tuple[Record, ...]

    def grouped(self) -> list[tuple[str, tuple[Record, ...]]]:
        """Group records by heading, preserving first-appearance order."""
        order: list[str] = []
        buckets: dict[str, list[Record]] = {}
        for record in self.records:
            if record.group not in buckets:
                buckets[record.group] = []
                order.append(record.group)
            buckets[record.group].append(record)
        return [(name, tuple(buckets[name])) for name in order]


DEFAULT_TITLE = "设备错误诊断 / Device Error Diagnostics"


def _normalize_text(value: str) -> str:
    if "\x00" in value:
        raise DataError("text contains NUL (U+0000), which HTML text cannot carry")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _require_text(obj: dict[str, Any], key: str, where: str, *, allow_empty: bool) -> str:
    if key not in obj:
        raise DataError(f"{where}: missing required field {key!r}")
    value = obj[key]
    if not isinstance(value, str):
        raise DataError(f"{where}: field {key!r} must be a string")
    if not allow_empty and value == "":
        raise DataError(f"{where}: field {key!r} must not be empty")
    return _normalize_text(value)


def _parse_doc(raw: Any, where: str) -> DocLink | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DataError(f"{where}: field 'doc' must be an object or null")
    if "url" not in raw:
        raise DataError(f"{where}: 'doc' requires a 'url' field")
    url = raw["url"]
    if not isinstance(url, str):
        raise DataError(f"{where}: 'doc.url' must be a string")
    label_raw = raw.get("label", "")
    if label_raw is None:
        label_raw = ""
    if not isinstance(label_raw, str):
        raise DataError(f"{where}: 'doc.label' must be a string")
    return DocLink(label=_normalize_text(label_raw), url=url)


def _parse_record(raw: Any, index: int) -> Record:
    where = f"records[{index}]"
    if not isinstance(raw, dict):
        raise DataError(f"{where}: record must be an object")
    record_id = _require_text(raw, "id", where, allow_empty=False)
    where = f"records[{index}] (id={record_id!r})"
    group = _require_text(raw, "group", where, allow_empty=False)
    summary = _require_text(raw, "summary", where, allow_empty=True)

    details: str | None = None
    if "details" in raw and raw["details"] is not None:
        raw_details = raw["details"]
        if not isinstance(raw_details, str):
            raise DataError(f"{where}: field 'details' must be a string or null")
        details = _normalize_text(raw_details)

    doc = _parse_doc(raw.get("doc") if "doc" in raw else None, where)
    return Record(record_id=record_id, group=group, summary=summary, details=details, doc=doc)


def parse_dataset(data: Any, source: str = "<dataset>") -> Dataset:
    """Validate a decoded JSON value and return a :class:`Dataset`."""
    if not isinstance(data, dict):
        raise DataError(f"{source}: top-level JSON value must be an object")
    title_raw = data.get("title", DEFAULT_TITLE)
    if title_raw is None:
        title_raw = DEFAULT_TITLE
    if not isinstance(title_raw, str):
        raise DataError(f"{source}: field 'title' must be a string")
    title = _normalize_text(title_raw)

    records_raw = data.get("records")
    if not isinstance(records_raw, list):
        raise DataError(f"{source}: field 'records' must be a list")

    records: list[Record] = []
    seen: set[str] = set()
    for index, raw in enumerate(records_raw):
        record = _parse_record(raw, index)
        if record.record_id in seen:
            raise DataError(f"{source}: duplicate record id {record.record_id!r}")
        seen.add(record.record_id)
        records.append(record)
    return Dataset(title=title, records=tuple(records))


def _read_json_text(path: str | Path, source: str) -> Any:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise DataError(f"{source}: cannot read {path}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataError(f"{source}: invalid JSON in {path}: {exc}") from exc


def load_dataset(path: str | Path) -> Dataset:
    """Read and validate a dataset file.  Raises :class:`DataError`."""
    source = str(path)
    return parse_dataset(_read_json_text(path, source), source=source)
