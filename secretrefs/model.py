"""Immutable input model for the secret reference resolver."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class Binding:
    """A variable -> secret reference binding declared in one config layer."""

    variable: str
    ref: str
    version: int | None = None
    required: bool = True

    def signature(self) -> tuple[str, int | None, bool]:
        return (self.ref, self.version, self.required)


@dataclass(frozen=True)
class AliasTarget:
    """The declared target of an alias entry."""

    environment: str
    service: str
    ref: str


@dataclass(frozen=True)
class CatalogEntry:
    """One registered reference identity in one environment/service scope."""

    environment: str
    service: str
    ref: str
    versions: tuple[int, ...] = ()
    current: int | None = None
    alias_of: AliasTarget | None = None


@dataclass(frozen=True)
class DeploymentInputs:
    environment: str
    services: tuple[str, ...]
    common: tuple[Binding, ...]
    environment_overrides: tuple[Binding, ...]
    service_overrides: Mapping[str, tuple[Binding, ...]]
    catalog: Mapping[tuple[str, str, str], CatalogEntry] = field(default_factory=dict)


PathKey = tuple[str, str, str]
