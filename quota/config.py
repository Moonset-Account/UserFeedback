"""Immutable configuration objects for the two quota layers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ValidationError


def require_finite_number(value: Any, field: str, *, minimum: float, exclusive: bool) -> float:
    """Validate a finite numeric config/clock value and return it as float."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValidationError(f"{field} must be finite, got {value!r}")
    if exclusive:
        if number <= minimum:
            raise ValidationError(f"{field} must be > {minimum!r}, got {number!r}")
    elif number < minimum:
        raise ValidationError(f"{field} must be >= {minimum!r}, got {number!r}")
    return number


def require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer, got {type(value).__name__}")
    if value < 1:
        raise ValidationError(f"{field} must be >= 1, got {value!r}")
    return value


@dataclass(frozen=True)
class LayerConfig:
    """Rolling window + limit for one quota layer."""

    limit: int
    window_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "limit", require_positive_int(self.limit, "limit"))
        object.__setattr__(
            self,
            "window_seconds",
            require_finite_number(self.window_seconds, "window_seconds", minimum=0.0, exclusive=True),
        )

    @classmethod
    def from_mapping(cls, data: Any, name: str) -> "LayerConfig":
        if not isinstance(data, Mapping):
            raise ValidationError(f"{name} config must be an object, got {type(data).__name__}")
        required = {"limit", "window_seconds"}
        unknown = set(data) - required
        missing = required - set(data)
        if unknown:
            raise ValidationError(f"{name} config has unknown keys: {sorted(unknown)}")
        if missing:
            raise ValidationError(f"{name} config is missing keys: {sorted(missing)}")
        return cls(limit=data["limit"], window_seconds=data["window_seconds"])


@dataclass(frozen=True)
class QuotaConfig:
    """Configuration for account + source layers and request-id deduplication."""

    account: LayerConfig
    source: LayerConfig
    dedup_ttl_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.account, LayerConfig):
            raise ValidationError("account config must be a LayerConfig")
        if not isinstance(self.source, LayerConfig):
            raise ValidationError("source config must be a LayerConfig")
        object.__setattr__(
            self,
            "dedup_ttl_seconds",
            require_finite_number(
                self.dedup_ttl_seconds, "dedup_ttl_seconds", minimum=0.0, exclusive=False
            ),
        )

    @classmethod
    def from_mapping(cls, data: Any) -> "QuotaConfig":
        if not isinstance(data, Mapping):
            raise ValidationError("config must be a JSON object")
        required = {"account", "source", "dedup_ttl_seconds"}
        unknown = set(data) - required
        missing = required - set(data)
        if unknown:
            raise ValidationError(f"config has unknown keys: {sorted(unknown)}")
        if missing:
            raise ValidationError(f"config is missing keys: {sorted(missing)}")
        ttl = require_finite_number(
            data["dedup_ttl_seconds"], "dedup_ttl_seconds", minimum=0.0, exclusive=False
        )
        return cls(
            account=LayerConfig.from_mapping(data["account"], "account"),
            source=LayerConfig.from_mapping(data["source"], "source"),
            dedup_ttl_seconds=ttl,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": {
                "limit": self.account.limit,
                "window_seconds": self.account.window_seconds,
            },
            "source": {
                "limit": self.source.limit,
                "window_seconds": self.source.window_seconds,
            },
            "dedup_ttl_seconds": self.dedup_ttl_seconds,
        }
