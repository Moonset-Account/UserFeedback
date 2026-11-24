"""Result object returned by the placement planner."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Hashable, Mapping

from .model import Probe


def _json_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


@dataclass(frozen=True)
class PlacementResult:
    selected_probes: tuple[Probe, ...]
    ambiguous_pairs: tuple[tuple[Hashable, Hashable], ...]
    distinguished_pairs: tuple[tuple[Hashable, Hashable], ...]
    signatures: Mapping[Hashable, tuple[str, ...]]
    equivalence_classes: tuple[tuple[Hashable, ...], ...]
    total_cost: Any
    budget: Any
    required_entries: frozenset[Hashable]
    covered_entries: frozenset[Hashable]
    pair_count: int
    distinguished_pair_count: int

    @property
    def probes(self) -> tuple[Probe, ...]:
        """Alias for :attr:`selected_probes`."""

        return self.selected_probes

    @property
    def selected(self) -> tuple[Probe, ...]:
        return self.selected_probes

    @property
    def selected_ids(self) -> tuple[Hashable, ...]:
        return tuple(probe.id for probe in self.selected_probes)

    @property
    def same_result_pairs(self) -> tuple[tuple[Hashable, Hashable], ...]:
        """Alias for :attr:`ambiguous_pairs`."""

        return self.ambiguous_pairs

    @property
    def ambiguous_pair_count(self) -> int:
        return len(self.ambiguous_pairs)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-ish plain-data representation."""

        return {
            "selected_probes": [
                {
                    "id": probe.id,
                    "site": probe.site,
                    "entry": probe.entry,
                    "dependencies": list(probe.dependencies),
                    "cost": _json_number(probe.cost),
                }
                for probe in self.selected_probes
            ],
            "selected_ids": list(self.selected_ids),
            "ambiguous_pairs": [list(pair) for pair in self.ambiguous_pairs],
            "same_result_pairs": [list(pair) for pair in self.ambiguous_pairs],
            "distinguished_pairs": [list(pair) for pair in self.distinguished_pairs],
            "signatures": {
                str(fault_id): list(signature)
                for fault_id, signature in self.signatures.items()
            },
            "equivalence_classes": [list(group) for group in self.equivalence_classes],
            "total_cost": _json_number(self.total_cost),
            "budget": _json_number(self.budget),
            "required_entries": list(self.required_entries),
            "covered_entries": list(self.covered_entries),
            "pair_count": self.pair_count,
            "distinguished_pair_count": self.distinguished_pair_count,
            "ambiguous_pair_count": self.ambiguous_pair_count,
        }
