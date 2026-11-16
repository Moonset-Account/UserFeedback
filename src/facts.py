"""Immutable, deterministic Stage 7 facts and deltas."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .observations import Observation

_ID = re.compile(r"^(?:obs|fact|delta)-sha256:[0-9a-f]{64}$")
_SEVERITY = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_CONFIDENCE = {"low": 0, "medium": 1, "high": 2}
_MAX_ITEMS = 256


def _bounded_text(value: Any, name: str, limit: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{name} must be non-empty bounded text")
    return value


def _ids(values: Iterable[str], prefix: str) -> tuple[str, ...]:
    result = tuple(sorted(set(values)))
    if len(result) > _MAX_ITEMS or any(not isinstance(value, str) or not re.fullmatch(prefix + r"-sha256:[0-9a-f]{64}", value) for value in result):
        raise ValueError("invalid or oversized model ID collection")
    return result


def _attributes(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 64:
        raise ValueError("attributes must be a bounded flat object")
    for key, item in value.items():
        if not isinstance(key, str) or not key or not (item is None or isinstance(item, (str, int, float, bool))):
            raise ValueError("attributes must contain only JSON-safe scalar values")
        if isinstance(item, float) and (item != item or item in (float("inf"), float("-inf"))):
            raise ValueError("attributes cannot contain non-finite numbers")
    return dict(sorted(value.items()))


@dataclass(frozen=True)
class Fact:
    fact_id: str
    family_id: str
    category: str
    severity: str
    confidence: str
    observation_ids: tuple[str, ...]
    scope: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.fact_id, str) or not re.fullmatch(r"fact-sha256:[0-9a-f]{64}", self.fact_id):
            raise ValueError("fact_id must be a fact-sha256 identity")
        _bounded_text(self.family_id, "family_id")
        _bounded_text(self.category, "category")
        if self.severity not in _SEVERITY or self.confidence not in _CONFIDENCE:
            raise ValueError("invalid fact severity or confidence")
        normalized_ids = _ids(self.observation_ids, "obs")
        if not normalized_ids:
            raise ValueError("a fact requires at least one observation")
        object.__setattr__(self, "observation_ids", normalized_ids)
        object.__setattr__(self, "scope", _bounded_text(self.scope, "scope"))
        object.__setattr__(self, "attributes", _attributes(self.attributes))

    @classmethod
    def from_observations(cls, family_id: str, category: str, scope: str, observations: Iterable[Observation], attributes: dict[str, Any] | None = None) -> "Fact":
        items = tuple(observations)
        if not items or any(not isinstance(item, Observation) for item in items):
            raise ValueError("facts require one or more Observation objects")
        _bounded_text(category, "category")
        categories = {item.category for item in items}
        if categories != {category}:
            raise ValueError("observations do not match the fact category")
        severity = max(items, key=lambda item: _SEVERITY[item.severity]).severity
        confidence = min(items, key=lambda item: _CONFIDENCE[item.confidence]).confidence
        ids = tuple(sorted({item.observation_id for item in items}))
        attrs = _attributes(attributes or {})
        identity = {"version": 1, "family_id": family_id, "category": category, "severity": severity,
                    "confidence": confidence, "observation_ids": ids, "scope": scope, "attributes": attrs}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return cls("fact-sha256:" + digest, family_id, category, severity, confidence, ids, scope, attrs)

    def to_dict(self) -> dict[str, Any]:
        return {"fact_id": self.fact_id, "family_id": self.family_id, "category": self.category,
                "severity": self.severity, "confidence": self.confidence,
                "observation_ids": list(self.observation_ids), "scope": self.scope,
                "attributes": dict(self.attributes)}


@dataclass(frozen=True)
class Delta:
    delta_id: str
    kind: str
    before_fact_ids: tuple[str, ...]
    after_fact_ids: tuple[str, ...]
    scope: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.delta_id, str) or not re.fullmatch(r"delta-sha256:[0-9a-f]{64}", self.delta_id):
            raise ValueError("delta_id must be a delta-sha256 identity")
        if self.kind not in {"added", "removed", "unchanged", "changed", "superseded"}:
            raise ValueError("invalid delta kind")
        before = _ids(self.before_fact_ids, "fact"); after = _ids(self.after_fact_ids, "fact")
        object.__setattr__(self, "before_fact_ids", before); object.__setattr__(self, "after_fact_ids", after)
        object.__setattr__(self, "scope", _bounded_text(self.scope, "scope"))
        object.__setattr__(self, "reason", _bounded_text(self.reason, "reason"))
        if self.kind == "added" and (before or not after): raise ValueError("added delta requires only after facts")
        if self.kind == "removed" and (not before or after): raise ValueError("removed delta requires only before facts")
        if self.kind == "unchanged" and (not before or before != after): raise ValueError("unchanged delta requires equal non-empty facts")
        if self.kind in {"changed", "superseded"} and (not before or not after or before == after):
            raise ValueError("changed/superseded delta requires distinct before and after facts")

    @classmethod
    def create(cls, kind: str, before_fact_ids: Iterable[str], after_fact_ids: Iterable[str], scope: str, reason: str) -> "Delta":
        before = _ids(before_fact_ids, "fact"); after = _ids(after_fact_ids, "fact")
        identity = {"version": 1, "kind": kind, "before": before, "after": after, "scope": scope, "reason": reason}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return cls("delta-sha256:" + digest, kind, before, after, scope, reason)

    def to_dict(self) -> dict[str, Any]:
        return {"delta_id": self.delta_id, "kind": self.kind, "before_fact_ids": list(self.before_fact_ids),
                "after_fact_ids": list(self.after_fact_ids), "scope": self.scope, "reason": self.reason}
