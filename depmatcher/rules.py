"""Rule book: dimension equality, substitution rules and exclusions.

The :class:`RuleBook` is the single place that knows how to decide whether a
produced :class:`~depmatcher.model.Artifact` satisfies a slot constraint for
a particular :class:`~depmatcher.model.TestPackage`.

Two kinds of dimension semantics are supported:

* **Scalar dimensions** (e.g. ``platform``, ``compiler``) match on equality
  or on a directed substitution rule ``(dimension, from_value, to_value)``
  meaning an artifact carrying ``to_value`` may fill a requirement for
  ``from_value``.  Substitution chains are transitive.
* **The set-valued ``features`` dimension** matches by containment: the
  artifact's feature set must contain every required feature.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .model import (
    Artifact,
    Constraint,
    DimensionError,
    Exclusion,
    MatchAny,
    MatchAnyOf,
    MatchValue,
    SlotRequirement,
    TestPackage,
)

FEATURES = "features"


class RuleBook:
    """Immutable collection of substitution and exclusion rules."""

    def __init__(
        self,
        substitutions: Iterable[tuple[str, str, str]] = (),
        exclusions: Iterable[Exclusion] = (),
    ) -> None:
        # adjacency[dimension][required_value] = accepted artifact values
        adjacency: dict[str, dict[str, set[str]]] = {}
        for dimension, from_value, to_value in substitutions:
            adjacency.setdefault(dimension, {}).setdefault(
                from_value, set()
            ).add(to_value)
        self._adjacency = adjacency
        # Pre-compute the transitive accepted-value closure per dimension.
        self._closure: dict[str, dict[str, frozenset[str]]] = {
            dimension: {
                value: self._build_closure(dimension, value)
                for value in adjacency[dimension]
            }
            for dimension in adjacency
        }
        self._exclusions = tuple(exclusions)

    # ------------------------------------------------------------------
    # Substitution
    # ------------------------------------------------------------------
    def _build_closure(self, dimension: str, start: str) -> frozenset[str]:
        """All values accepted for ``start`` including transitive hops."""
        seen = {start}
        stack = [start]
        graph = self._adjacency.get(dimension, {})
        while stack:
            current = stack.pop()
            for nxt in graph.get(current, ()):  # direct substitution targets
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return frozenset(seen)

    def accepted_values(self, dimension: str, required: str) -> frozenset[str]:
        """Values an artifact may carry to satisfy a scalar requirement.

        Always includes the required value itself (plain equality).
        """
        closure = self._closure.get(dimension, {})
        if required in closure:
            return closure[required]
        # No substitution chain starts at this value: equality only.
        return frozenset({required})

    # ------------------------------------------------------------------
    # Per-constraint compatibility
    # ------------------------------------------------------------------
    def _dimension_matches(
        self, dimension: str, required: Any, artifact: Artifact
    ) -> bool:
        if dimension == FEATURES:
            # Containment: every required feature must be produced.
            return required <= artifact.features
        if dimension not in artifact.dimensions:
            raise DimensionError(
                f"artifact {artifact.id!r} has no dimension {dimension!r}"
            )
        return artifact.dimensions[dimension] in self.accepted_values(
            dimension, required
        )

    def _constraint_matches(
        self, constraint: Constraint, artifact: Artifact
    ) -> bool:
        if isinstance(constraint, MatchValue):
            return self._dimension_matches(
                constraint.dimension, constraint.value, artifact
            )
        if isinstance(constraint, MatchAnyOf):
            # A value is acceptable if it satisfies *any* branch; branches
            # are plain value requirements and therefore reuse the closure.
            if constraint.dimension == FEATURES:
                return any(
                    self._dimension_matches(
                        FEATURES, frozenset(value), artifact
                    )
                    for value in constraint.values
                )
            if constraint.dimension not in artifact.dimensions:
                raise DimensionError(
                    f"artifact {artifact.id!r} has no dimension "
                    f"{constraint.dimension!r}"
                )
            actual = artifact.dimensions[constraint.dimension]
            return any(
                actual in self.accepted_values(constraint.dimension, value)
                for value in constraint.values
            )
        if isinstance(constraint, MatchAny):
            if constraint.dimension == FEATURES:
                return True  # feature presence is optional by definition
            return constraint.dimension in artifact.dimensions
        raise DimensionError(f"unknown constraint type: {constraint!r}")

    # ------------------------------------------------------------------
    # Exclusions
    # ------------------------------------------------------------------
    @staticmethod
    def _artifact_pattern_matches(
        artifact: Artifact, pattern: Mapping[str, Any]
    ) -> bool:
        for dimension, expected in pattern.items():
            if dimension == FEATURES:
                expected_set = frozenset(expected)
                if not expected_set <= artifact.features:
                    return False
            else:
                if artifact.dimensions.get(dimension) != expected:
                    return False
        return True

    @staticmethod
    def _package_pattern_matches(
        package: TestPackage, pattern: Mapping[str, str]
    ) -> bool:
        return all(
            package.attributes.get(key) == value
            for key, value in pattern.items()
        )

    def is_excluded(self, artifact: Artifact, package: TestPackage) -> bool:
        """Return True when a conditional exclusion blocks this pairing."""
        for rule in self._exclusions:
            if self._artifact_pattern_matches(
                artifact, rule.artifact_match
            ) and self._package_pattern_matches(
                package, rule.package_match
            ):
                return True
        return False

    # ------------------------------------------------------------------
    # Candidate selection
    # ------------------------------------------------------------------
    def candidates_for_slot(
        self,
        slot: SlotRequirement,
        inventory: Sequence[Artifact],
        package: TestPackage,
    ) -> tuple[Artifact, ...]:
        """All artifacts compatible with one slot of one package.

        The result is sorted by artifact id so downstream matching is
        deterministic and replay bindings are stable.
        """
        result: list[Artifact] = []
        for artifact in inventory:
            if artifact.kind != slot.kind:
                continue
            if self.is_excluded(artifact, package):
                continue
            if all(
                self._constraint_matches(constraint, artifact)
                for constraint in slot.constraints
            ):
                result.append(artifact)
        return tuple(sorted(result, key=lambda item: item.id))
