"""Versioned calculation definitions.

A definition version must stay registered for the lifetime of snapshots that
may lazily compute pending groups.  The built-in definitions are pure and
serializable; callers can additionally register custom callables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Tuple

from .errors import DefinitionNotRegisteredError


@dataclass(frozen=True)
class ScaledFieldDefinition:
    """value = row[field] * factor + offset."""

    name: str
    version: int
    field: str
    factor: int = 1
    offset: int = 0
    kind: str = "scaled_field"

    def compute(self, row: Mapping[str, Any]) -> Any:
        return row[self.field] * self.factor + self.offset

    def descriptor(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "field": self.field,
            "factor": self.factor,
            "offset": self.offset,
        }


@dataclass(frozen=True)
class ConcatDefinition:
    """Concatenate ordered fields with a separator."""

    name: str
    version: int
    fields: Tuple[str, ...]
    separator: str = " "
    kind: str = "concat"

    def compute(self, row: Mapping[str, Any]) -> str:
        return self.separator.join(str(row[field]) for field in self.fields)

    def descriptor(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "version": self.version,
            "fields": list(self.fields),
            "separator": self.separator,
        }


@dataclass(frozen=True)
class CallableDefinition:
    """Adapter for user-supplied pure functions.

    The callable is not persisted as executable code.  Reopen the process with
    an equivalent registration when old snapshots need lazy computation.
    """

    name: str
    version: int
    func: Callable[[Mapping[str, Any]], Any]
    kind: str = "callable"

    def compute(self, row: Mapping[str, Any]) -> Any:
        return self.func(row)

    def descriptor(self) -> Dict[str, Any]:
        return {"kind": "callable", "name": self.name, "version": self.version}


Definition = ScaledFieldDefinition | ConcatDefinition | CallableDefinition


class DefinitionRegistry:
    def __init__(self) -> None:
        self._definitions: Dict[Tuple[str, int], Definition] = {}

    def register(self, definition: Definition) -> Definition:
        key = (definition.name, definition.version)
        self._definitions[key] = definition
        return definition

    def get(self, name: str, version: int) -> Definition:
        try:
            return self._definitions[(name, version)]
        except KeyError as exc:
            raise DefinitionNotRegisteredError(
                f"definition {name!r} version {version} is not registered"
            ) from exc

    def has(self, name: str, version: int) -> bool:
        return (name, version) in self._definitions

    @classmethod
    def with_score_versions(cls) -> "DefinitionRegistry":
        """Registry used by tests/demo.

        v1 doubles ``x``.  v2 doubles and adds one, making an accidental mix of
        definition versions immediately visible.
        """

        registry = cls()
        registry.register(
            ScaledFieldDefinition("score", 1, field="x", factor=2, offset=0)
        )
        registry.register(
            ScaledFieldDefinition("score", 2, field="x", factor=2, offset=1)
        )
        return registry
