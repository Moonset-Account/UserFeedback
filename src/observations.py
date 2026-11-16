"""Immutable, redacted Stage 7 observation primitives.

This module intentionally has no scanner, filesystem, network or subprocess dependency.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any


_SOURCE_KINDS = frozenset({"native-line-rule", "native-context-rule", "config-classifier", "external-adapter"})
_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_CONFIDENCES = frozenset({"low", "medium", "high"})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT = 512
_MAX_REDACTED = 1024
_MAX_EVIDENCE_LENGTH = 1_000_000
_MAX_METADATA = 64


def _text(value: Any, field_name: str, maximum: int = _MAX_TEXT) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field_name} must be a non-empty bounded string")
    return value


def normalize_identity_path(path: str) -> str:
    """Normalize a display/identity path without resolving it against the host."""
    value = _text(path, "path", 4096).replace("\\", "/")
    if re.match(r"^[A-Z]:", value):
        value = value[0].lower() + value[1:]
    normalized = posixpath.normpath(value)
    if normalized == "." or normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        raise ValueError("path must be relative")
    return normalized


def _flat_metadata(metadata: Any) -> tuple[tuple[str, Any], ...]:
    if not isinstance(metadata, dict) or len(metadata) > _MAX_METADATA:
        raise ValueError("metadata must be a bounded flat object")
    result = []
    for key, value in metadata.items():
        if not isinstance(key, str) or not key or len(key) > _MAX_TEXT:
            raise ValueError("metadata keys must be bounded strings")
        if not (value is None or isinstance(value, (str, int, float, bool))):
            raise ValueError("metadata values must be JSON-safe scalars")
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError("metadata cannot contain non-finite numbers")
        result.append((key, value))
    return tuple(sorted(result))


@dataclass(frozen=True)
class ObservationLocation:
    path: str
    start_line: int
    end_line: int
    identity_path: str | None = None
    hunk_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_identity_path(self.path))
        if not isinstance(self.start_line, int) or isinstance(self.start_line, bool) or self.start_line < 0:
            raise ValueError("start_line must be a non-negative integer")
        if not isinstance(self.end_line, int) or isinstance(self.end_line, bool) or self.end_line < self.start_line:
            raise ValueError("end_line must be an integer >= start_line")
        if self.identity_path is not None:
            object.__setattr__(self, "identity_path", normalize_identity_path(self.identity_path))
        if self.hunk_id is not None:
            object.__setattr__(self, "hunk_id", _text(self.hunk_id, "hunk_id"))

    @property
    def stable_path(self) -> str:
        return self.identity_path or self.path

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "start_line": self.start_line, "end_line": self.end_line,
                "identity_path": self.identity_path, "hunk_id": self.hunk_id}


@dataclass(frozen=True)
class ObservationEvidence:
    redacted: str
    digest: str
    length: int

    def __post_init__(self) -> None:
        if not isinstance(self.redacted, str) or len(self.redacted) > _MAX_REDACTED:
            raise ValueError("redacted evidence must be bounded text")
        if not isinstance(self.digest, str) or not _DIGEST.fullmatch(self.digest):
            raise ValueError("digest must be a lower-case SHA-256 hex digest")
        if not isinstance(self.length, int) or isinstance(self.length, bool) or not 0 <= self.length <= _MAX_EVIDENCE_LENGTH:
            raise ValueError("evidence length is outside the permitted bound")

    @classmethod
    def from_raw(cls, raw: str, redacted: str = "[REDACTED]") -> "ObservationEvidence":
        if not isinstance(raw, str) or len(raw) > _MAX_EVIDENCE_LENGTH:
            raise ValueError("raw evidence is outside the permitted bound")
        if not isinstance(redacted, str) or len(redacted) > _MAX_REDACTED:
            raise ValueError("redacted evidence must be bounded text")
        return cls(redacted=redacted, digest=hashlib.sha256(raw.encode("utf-8")).hexdigest(), length=len(raw))

    def to_dict(self) -> dict[str, Any]:
        return {"redacted": self.redacted, "digest": self.digest, "length": self.length}


@dataclass(frozen=True)
class Observation:
    source_kind: str
    source_id: str
    kind: str
    category: str
    severity: str
    confidence: str
    location: ObservationLocation
    evidence: ObservationEvidence
    metadata: dict[str, Any] = field(default_factory=dict)
    observation_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.source_kind not in _SOURCE_KINDS:
            raise ValueError("unsupported observation source_kind")
        object.__setattr__(self, "source_id", _text(self.source_id, "source_id"))
        object.__setattr__(self, "kind", _text(self.kind, "kind"))
        object.__setattr__(self, "category", _text(self.category, "category"))
        if self.severity not in _SEVERITIES:
            raise ValueError("unsupported observation severity")
        if self.confidence not in _CONFIDENCES:
            raise ValueError("unsupported observation confidence")
        if not isinstance(self.location, ObservationLocation):
            raise ValueError("location must be ObservationLocation")
        if not isinstance(self.evidence, ObservationEvidence):
            raise ValueError("evidence must be ObservationEvidence")
        normalized_metadata = _flat_metadata(self.metadata)
        object.__setattr__(self, "metadata", dict(normalized_metadata))
        identity = {
            "version": 1, "source_kind": self.source_kind, "source_id": self.source_id,
            "kind": self.kind, "category": self.category, "severity": self.severity,
            "confidence": self.confidence, "location": {
                "path": self.location.stable_path, "start_line": self.location.start_line,
                "end_line": self.location.end_line, "hunk_id": self.location.hunk_id,
            }, "metadata": dict(normalized_metadata),
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        object.__setattr__(self, "observation_id", "obs-sha256:" + hashlib.sha256(encoded).hexdigest())

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id, "source_kind": self.source_kind, "source_id": self.source_id,
            "kind": self.kind, "category": self.category, "severity": self.severity,
            "confidence": self.confidence, "location": self.location.to_dict(),
            "evidence": self.evidence.to_dict(), "metadata": dict(self.metadata),
        }
