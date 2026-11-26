"""JSON loading helpers. JSON is data only; no import or deserialization hooks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .session import AuditSession, audit_topology


def load_request_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("audit request JSON must contain an object")
    return data


def audit_from_file(path: str | Path):
    data = load_request_json(path)
    return audit_topology(**data)


def session_from_file(path: str | Path) -> AuditSession:
    data = load_request_json(path)
    return AuditSession.create(**data)
