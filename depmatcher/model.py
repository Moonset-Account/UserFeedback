"""Domain model for the release-artifact dependency matcher.

The world consists of:

* :class:`Artifact` – a produced piece of a release pipeline, described by
  scalar dimensions (``platform``, ``compiler`` ...) and a set-valued
  ``features`` dimension;
* :class:`TestPackage` – a consumer whose required positions are described by
  :class:`SlotRequirement` objects;
* :class:`Constraint` – dimension-level rules (equality, any-of, any-value)
  stored inside a slot requirement;
* :class:`Exclusion` – a conditional rule that removes otherwise-compatible
  artifact/package combinations.

Rules that connect dimension values (substitution) live in
:mod:`depmatcher.rules` next to the :class:`~depmatcher.rules.RuleBook`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, FrozenSet, Mapping


class DimensionError(ValueError):
    """Raised when input objects reference unknown or malformed dimensions."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _freeze_features(values: Any) -> FrozenSet[str]:
    """Normalise the ``features`` dimension into a frozenset of strings."""
    if values is None:
        return frozenset()
    if isinstance(values, str):
        # A single feature string is accepted, but an iterable is the norm.
        return frozenset({values})
    return frozenset(str(v) for v in values)


# ---------------------------------------------------------------------------
# Produced artifacts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    """A single produced artifact (a physical, independently usable item).

    ``dimensions`` holds scalar dimensions such as ``platform`` and
    ``compiler``.  The set-valued ``features`` dimension is kept separate so
    that containment-style matching can be applied to it.
    """

    id: str
    kind: str
    dimensions: Mapping[str, str] = field(default_factory=dict)
    features: FrozenSet[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise DimensionError("Artifact.id must be a non-empty string")
        if not isinstance(self.kind, str) or not self.kind:
            raise DimensionError("Artifact.kind must be a non-empty string")
        # Frozen dataclass: write through object.__setattr__ on purpose.
        object.__setattr__(self, "dimensions", dict(self.dimensions))
        object.__setattr__(self, "features", _freeze_features(self.features))

    def get(self, dimension: str) -> str:
        """Return a scalar dimension value, raising if it is absent."""
        try:
            return self.dimensions[dimension]
        except KeyError as exc:
            raise DimensionError(
                f"artifact {self.id!r} has no dimension {dimension!r}"
            ) from exc


# ---------------------------------------------------------------------------
# Slot-level constraints
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MatchValue:
    """Require a dimension to equal ``value`` (or contain it for features)."""

    dimension: str
    value: Any

    def __post_init__(self) -> None:
        if self.dimension == "features" and not isinstance(
            self.value, frozenset
        ):
            object.__setattr__(self, "value", _freeze_features(self.value))


@dataclass(frozen=True)
class MatchAnyOf:
    """Require a dimension to take one of several acceptable values."""

    dimension: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(self.values))
        if not self.values:
            raise DimensionError("MatchAnyOf needs at least one value")


@dataclass(frozen=True)
class MatchAny:
    """Require the dimension to exist on the artifact; any value is fine."""

    dimension: str


# Constraint is a tagged union of the three classes above.
Constraint = "MatchValue | MatchAnyOf | MatchAny"


@dataclass(frozen=True)
class SlotRequirement:
    """A required position (slot) of a test package.

    ``count`` is the slot cardinality: that many *distinct* physical
    artifacts must fill the slot, one per position.  Multiple constraints on
    different dimensions are combined with logical AND.
    """

    name: str
    kind: str
    count: int = 1
    constraints: tuple[Constraint, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise DimensionError("SlotRequirement.name must be non-empty")
        if not self.kind:
            raise DimensionError("SlotRequirement.kind must be non-empty")
        if self.count < 1:
            raise DimensionError(
                f"slot {self.name!r} count must be >= 1, got {self.count}"
            )
        object.__setattr__(self, "constraints", tuple(self.constraints))


@dataclass(frozen=True)
class TestPackage:
    """A consumer with an ordered list of required slots."""

    id: str
    slots: tuple[SlotRequirement, ...]
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise DimensionError("TestPackage.id must be non-empty")
        object.__setattr__(self, "slots", tuple(self.slots))
        object.__setattr__(self, "attributes", dict(self.attributes))
        names = [slot.name for slot in self.slots]
        if len(names) != len(set(names)):
            raise DimensionError(
                f"test package {self.id!r} has duplicate slot names"
            )

    def slot(self, name: str) -> SlotRequirement:
        for slot in self.slots:
            if slot.name == name:
                return slot
        raise DimensionError(
            f"test package {self.id!r} has no slot named {name!r}"
        )


# ---------------------------------------------------------------------------
# Conditional exclusions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Exclusion:
    """A conditional rule excluding matching artifacts from a test package.

    When an artifact's dimensions/features match every ``artifact_match``
    entry and the test package's attributes match every ``package_match``
    entry, the artifact is excluded for that package.  Empty match mappings
    act as wildcards, so an exclusion with neither side populated blocks the
    artifact for every package.
    """

    name: str
    artifact_match: Mapping[str, Any] = field(default_factory=dict)
    package_match: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise DimensionError("Exclusion.name must be non-empty")
        object.__setattr__(self, "artifact_match", dict(self.artifact_match))
        object.__setattr__(self, "package_match", dict(self.package_match))
