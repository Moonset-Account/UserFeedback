"""Inventory, matching engine and unsatisfiable-slot diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Mapping, Sequence

from .model import (
    Artifact,
    DimensionError,
    SlotRequirement,
    TestPackage,
)
from .rules import RuleBook


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class Inventory:
    """An indexed collection of produced artifacts."""

    def __init__(self, artifacts: Sequence[Artifact] = ()) -> None:
        self._by_id: dict[str, Artifact] = {}
        for artifact in artifacts:
            self.add(artifact)

    def add(self, artifact: Artifact) -> None:
        """Register one more produced artifact (the '补上一件制品' event)."""
        if artifact.id in self._by_id:
            raise DimensionError(
                f"duplicate artifact id in inventory: {artifact.id!r}"
            )
        self._by_id[artifact.id] = artifact

    def get(self, artifact_id: str) -> Artifact:
        try:
            return self._by_id[artifact_id]
        except KeyError as exc:
            raise DimensionError(
                f"artifact {artifact_id!r} is not in the inventory"
            ) from exc

    def __contains__(self, artifact_id: str) -> bool:
        return artifact_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    @property
    def all(self) -> tuple[Artifact, ...]:
        """All artifacts in stable id order."""
        return tuple(sorted(self._by_id.values(), key=lambda item: item.id))


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SlotShortage:
    """A slot whose candidate count is below its required cardinality."""

    slot: str
    required: int
    candidates: tuple[Artifact, ...]

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(artifact.id for artifact in self.candidates)


@dataclass(frozen=True)
class Contention:
    """A group of slots whose union of candidates cannot cover demand.

    This is a Hall-condition violation: every individual slot may have enough
    candidates on paper, but the slots compete for the same physical items
    and distinctness makes the whole group infeasible.
    """

    slots: tuple[str, ...]
    required_total: int
    candidates: tuple[Artifact, ...]

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(artifact.id for artifact in self.candidates)


@dataclass(frozen=True)
class MatchResult:
    """Outcome of matching one test package against one inventory."""

    package_id: str
    success: bool
    binding: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    shortages: tuple[SlotShortage, ...] = ()
    contention: Contention | None = None

    @property
    def assigned_artifact_ids(self) -> frozenset[str]:
        """All physical artifacts occupied by this complete binding."""
        used: set[str] = set()
        for ids in self.binding.values():
            used.update(ids)
        return frozenset(used)


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------

class DependencyMatcher:
    """Match the required slots of a test package against an inventory."""

    def __init__(self, rule_book: RuleBook) -> None:
        self._rules = rule_book

    def candidate_map(
        self, package: TestPackage, inventory: Inventory
    ) -> dict[str, tuple[Artifact, ...]]:
        """Compatible candidate artifacts per slot name."""
        return {
            slot.name: self._rules.candidates_for_slot(
                slot, inventory.all, package
            )
            for slot in package.slots
        }

    def match(
        self, package: TestPackage, inventory: Inventory
    ) -> MatchResult:
        """Return a complete distinct-artifact binding, or a diagnosis.

        A package with no required slots matches trivially with an empty
        binding.
        """
        candidates = self.candidate_map(package, inventory)

        # Phase 1: per-slot cardinality shortages (the headline failure mode).
        shortages: list[SlotShortage] = []
        for slot in package.slots:
            pool = candidates[slot.name]
            if len(pool) < slot.count:
                shortages.append(
                    SlotShortage(
                        slot=slot.name,
                        required=slot.count,
                        candidates=pool,
                    )
                )
        if shortages:
            return MatchResult(
                package_id=package.id,
                success=False,
                shortages=tuple(shortages),
            )

        # Phase 2: backtracking with global distinctness across every slot.
        ordered = sorted(package.slots, key=lambda s: (len(candidates[s.name]), s.name))
        binding = self._search(ordered, candidates, used=set(), assignment={})
        if binding is not None:
            # Restore the package's declared slot order in the result.
            ordered_binding = {
                slot.name: binding[slot.name] for slot in package.slots
            }
            return MatchResult(
                package_id=package.id,
                success=True,
                binding=ordered_binding,
            )

        # Phase 3: every slot looked feasible alone but distinctness failed –
        # pinpoint the smallest group of slots whose candidate union is too
        # small for their combined demand (Hall's condition).
        contention = self._find_contention(package.slots, candidates)
        return MatchResult(
            package_id=package.id,
            success=False,
            contention=contention,
        )

    # ------------------------------------------------------------------
    def _search(
        self,
        ordered_slots: Sequence[SlotRequirement],
        candidates: Mapping[str, tuple[Artifact, ...]],
        used: frozenset[str],
        assignment: Mapping[str, tuple[str, ...]],
    ) -> dict[str, tuple[str, ...]] | None:
        if not ordered_slots:
            return dict(assignment)

        slot = ordered_slots[0]
        rest = ordered_slots[1:]
        pool = [artifact for artifact in candidates[slot.name] if artifact.id not in used]
        if len(pool) < slot.count:
            return None

        # Candidate pools are id-sorted; combinations therefore enumerate in
        # a deterministic order and the first solution is reproducible.
        for combo in _combinations(pool, slot.count):
            next_used = used | {artifact.id for artifact in combo}
            assignment = {**assignment, slot.name: tuple(a.id for a in combo)}
            result = self._search(rest, candidates, next_used, assignment)
            if result is not None:
                return result
        return None

    @staticmethod
    def _find_contention(
        slots: Sequence[SlotRequirement],
        candidates: Mapping[str, tuple[Artifact, ...]],
    ) -> Contention:
        """Smallest (then lexicographically first) Hall-violating slot group."""
        best: Contention | None = None
        for size in range(1, len(slots) + 1):
            for group in combinations(slots, size):
                names = tuple(slot.name for slot in group)
                demand = sum(slot.count for slot in group)
                union_ids: set[str] = set()
                for slot in group:
                    union_ids.update(a.id for a in candidates[slot.name])
                if len(union_ids) < demand:
                    union = tuple(
                        sorted(
                            (a for slot in group for a in candidates[slot.name]),
                            key=lambda item: item.id,
                        )
                    )
                    # Deduplicate while preserving order.
                    seen: set[str] = set()
                    unique_union = tuple(
                        a for a in union if not (a.id in seen or seen.add(a.id))
                    )
                    violator = Contention(
                        slots=names,
                        required_total=demand,
                        candidates=unique_union,
                    )
                    if best is None or names < best.slots:
                        best = violator
            if best is not None:
                return best
        # Unreachable: _search failing guarantees some Hall violation exists.
        raise RuntimeError("matching failed without a Hall violation")  # pragma: no cover


def _combinations(pool: Sequence[Artifact], count: int):
    """Thin wrapper around itertools.combinations kept out of the hot import."""
    return combinations(pool, count)
