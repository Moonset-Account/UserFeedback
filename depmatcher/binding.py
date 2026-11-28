"""Persisted bindings and replay after the inventory grows.

Once a test package has been matched successfully its binding is stored.
When another artifact is produced afterwards, previously successful,
unrelated packages must keep working with their *original* physical
artifacts – :class:`BindingRegistry` replays the stored binding against the
current inventory and rule book instead of recomputing a new assignment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .matcher import DependencyMatcher, Inventory, MatchResult
from .model import DimensionError, TestPackage


@dataclass(frozen=True)
class Binding:
    """A successful assignment: slot name -> distinct artifact ids."""

    package_id: str
    assignment: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "assignment", {k: tuple(v) for k, v in self.assignment.items()}
        )
        # A binding itself must never reuse one physical artifact twice.
        seen: set[str] = set()
        for ids in self.assignment.values():
            for artifact_id in ids:
                if artifact_id in seen:
                    raise DimensionError(
                        f"binding for {self.package_id!r} reuses artifact "
                        f"{artifact_id!r}"
                    )
                seen.add(artifact_id)

    @property
    def artifact_ids(self) -> frozenset[str]:
        used: set[str] = set()
        for ids in self.assignment.values():
            used.update(ids)
        return frozenset(used)


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of replaying a stored binding against current state."""

    package_id: str
    valid: bool
    binding: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()


class BindingRegistry:
    """Stores successful bindings and replays them on later inventories."""

    def __init__(self, rule_book) -> None:
        self._rule_book = rule_book
        self._bindings: dict[str, Binding] = {}

    def store(self, result: MatchResult) -> Binding:
        """Persist the binding of a successful match."""
        if not result.success:
            raise DimensionError(
                f"cannot store unsuccessful match for {result.package_id!r}"
            )
        binding = Binding(result.package_id, dict(result.binding))
        self._bindings[result.package_id] = binding
        return binding

    def has(self, package_id: str) -> bool:
        return package_id in self._bindings

    def get(self, package_id: str) -> Binding:
        try:
            return self._bindings[package_id]
        except KeyError as exc:
            raise DimensionError(
                f"no stored binding for package {package_id!r}"
            ) from exc

    def replay(
        self,
        package: TestPackage,
        inventory: Inventory,
        matcher: DependencyMatcher | None = None,
    ) -> ReplayResult:
        """Re-check the stored binding against the current inventory.

        Adding an artifact never invalidates a previously valid binding;
        removing or changing one can, and every failure reason is reported.
        A matcher can be supplied to additionally verify that every stored
        artifact is still rule-compatible with its slot.
        """
        binding = self._bindings.get(package.id)
        if binding is None:
            return ReplayResult(
                package_id=package.id,
                valid=False,
                reasons=("no stored binding",),
            )

        reasons: list[str] = []
        declared = {slot.name: slot for slot in package.slots}

        # Slot topology of the package must not have changed.
        if set(binding.assignment) != set(declared):
            missing = sorted(set(binding.assignment) - set(declared))
            extra = sorted(set(declared) - set(binding.assignment))
            if missing:
                reasons.append(f"slots removed from package: {missing}")
            if extra:
                reasons.append(f"new slots without binding: {extra}")

        candidate_map = (
            matcher.candidate_map(package, inventory)
            if matcher is not None
            else None
        )

        for slot_name, artifact_ids in binding.assignment.items():
            slot = declared.get(slot_name)
            if slot is None:
                continue  # already reported as a removed slot
            if len(artifact_ids) != slot.count:
                reasons.append(
                    f"slot {slot_name!r} needs {slot.count} artifacts but "
                    f"binding holds {len(artifact_ids)}"
                )
            if len(set(artifact_ids)) != len(artifact_ids):
                reasons.append(f"slot {slot_name!r} binding repeats an artifact")
            for artifact_id in artifact_ids:
                if artifact_id not in inventory:
                    reasons.append(
                        f"artifact {artifact_id!r} no longer in inventory"
                    )
                    continue
                if candidate_map is not None:
                    compatible = {a.id for a in candidate_map[slot_name]}
                    if artifact_id not in compatible:
                        reasons.append(
                            f"artifact {artifact_id!r} no longer compatible "
                            f"with slot {slot_name!r}"
                        )

        if reasons:
            return ReplayResult(
                package_id=package.id,
                valid=False,
                binding=dict(binding.assignment),
                reasons=tuple(reasons),
            )
        return ReplayResult(
            package_id=package.id,
            valid=True,
            binding=dict(binding.assignment),
        )
