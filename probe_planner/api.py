"""Public planning and evaluation API."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Hashable, Iterable

from .errors import InfeasibleCoverageError, ValidationError
from .model import Fault, Probe, validate_cost
from .normalization import normalize_faults, normalize_probes
from .result import PlacementResult
from .semantics import pair_index_map, signatures_for_probes
from .solver import optimal_probe_subset

Number = int | float


def plan_probes(
    probes: Iterable[Probe | Mapping[str, Any] | tuple[Any, ...]],
    faults: Iterable[Fault | Mapping[str, Any] | str | tuple[Any, ...]] | None = None,
    *,
    budget: Number | None = None,
    required_entries: Iterable[Hashable] | None = None,
    request_cost: Number | Callable[[Hashable, Hashable], Number] = 1,
    cost_for: Mapping[Any, Number] | Callable[..., Number] | None = None,
    known_sites: Iterable[Hashable] | None = None,
) -> PlacementResult:
    """Select the optimal feasible subset of candidate probes.

    The subset covers every entry in ``required_entries`` (defaults to every
    entry appearing in ``probes``), costs at most ``budget``, and maximizes the
    number of distinguishable unordered pairs of single-fault scenarios.
    Ties are resolved by lower cost, fewer probes, then earlier candidate
    order.
    """

    if isinstance(probes, (Probe, Mapping)):
        probes = [probes]
    probes = normalize_probes(
        probes, request_cost=request_cost, cost_for=cost_for
    )
    if known_sites is None:
        known_sites = {probe.site for probe in probes}
    elif isinstance(known_sites, (str, bytes)):
        known_sites = {known_sites}
    faults = normalize_faults(faults or [], known_sites=known_sites)

    if budget is None:
        budget_value: Any = float("inf")
    else:
        budget_value = validate_cost(budget, field_name="budget")

    candidate_entries = tuple(dict.fromkeys(probe.entry for probe in probes))
    if required_entries is None:
        required_tuple = candidate_entries
    elif isinstance(required_entries, (str, bytes)):
        required_tuple = (required_entries,)
    else:
        required_tuple = tuple(dict.fromkeys(required_entries))

    entry_set = set(candidate_entries)
    missing_entries = frozenset(entry for entry in required_tuple if entry not in entry_set)
    if missing_entries:
        raise InfeasibleCoverageError(
            "some required entries have no candidate probe: "
            + ", ".join(map(str, sorted(missing_entries, key=str))),
            required_entries=frozenset(required_tuple),
            missing_entries=missing_entries,
            minimum_cost=None,
            budget=budget,
        )

    required_index = {entry: i for i, entry in enumerate(required_tuple)}
    entry_masks: list[int] = []
    for probe in probes:
        index = required_index.get(probe.entry)
        entry_masks.append(0 if index is None else 1 << index)

    required_entry_mask = (
        (1 << len(required_tuple)) - 1 if required_tuple else 0
    )

    # Import here to keep semantics construction readable.
    from .semantics import probe_distinction_mask

    distinction_masks, pairs, matrix = probe_distinction_mask(probes, faults)
    costs = tuple(probe.cost if probe.cost is not None else 0 for probe in probes)

    old_limit = sys.getrecursionlimit()
    needed_limit = max(old_limit, len(probes) * 4 + 100)
    sys.setrecursionlimit(needed_limit)
    try:
        optimal = optimal_probe_subset(
            costs=costs,
            distinction_masks=distinction_masks,
            entry_masks=entry_masks,
            required_entry_mask=required_entry_mask,
            budget=budget_value,
            required_entries=frozenset(required_tuple),
            entry_ids=required_tuple,
        )
    finally:
        sys.setrecursionlimit(old_limit)

    selected_indices = tuple(
        i for i in range(len(probes)) if optimal.mask & (1 << i)
    )
    selected_probes = tuple(probes[i] for i in selected_indices)

    selected_union = 0
    for i in selected_indices:
        selected_union |= distinction_masks[i]

    ambiguous_pairs: list[tuple[Hashable, Hashable]] = []
    distinguished_pairs: list[tuple[Hashable, Hashable]] = []
    for pair_bit, (left, right) in enumerate(pairs):
        pair = (faults[left].id, faults[right].id)
        if selected_union & (1 << pair_bit):
            distinguished_pairs.append(pair)
        else:
            ambiguous_pairs.append(pair)

    selected_matrix = [
        tuple(matrix[fi][i] for i in selected_indices)
        for fi in range(len(faults))
    ]
    signatures = {
        fault.id: selected_matrix[fi] for fi, fault in enumerate(faults)
    }

    groups: dict[tuple[str, ...], list[Hashable]] = {}
    for fi, fault in enumerate(faults):
        groups.setdefault(selected_matrix[fi], []).append(fault.id)
    equivalence_classes = tuple(tuple(group) for group in groups.values())

    covered_entries = frozenset(probe.entry for probe in selected_probes)

    return PlacementResult(
        selected_probes=selected_probes,
        ambiguous_pairs=tuple(ambiguous_pairs),
        distinguished_pairs=tuple(distinguished_pairs),
        signatures=signatures,
        equivalence_classes=equivalence_classes,
        total_cost=optimal.cost,
        budget=budget,
        required_entries=frozenset(required_tuple),
        covered_entries=covered_entries,
        pair_count=len(pairs),
        distinguished_pair_count=len(distinguished_pairs),
    )


def evaluate_selection(
    probes: Iterable[Probe | Mapping[str, Any] | tuple[Any, ...]],
    faults: Iterable[Fault | Mapping[str, Any] | str | tuple[Any, ...]] | None,
    selected: Iterable[Hashable | Probe | Mapping[str, Any]],
    *,
    request_cost: Number | Callable[[Hashable, Hashable], Number] = 1,
    known_sites: Iterable[Hashable] | None = None,
) -> dict[str, Any]:
    """Evaluate one explicitly supplied probe selection.

    ``selected`` contains probe ids, or probe objects/mappings that include an
    id.  Returns signatures, equivalence classes, ambiguous pairs, and cost.
    """

    all_probes = normalize_probes(probes, request_cost=request_cost)
    if known_sites is None:
        known_sites = {probe.site for probe in all_probes}
    faults = normalize_faults(faults or [], known_sites=known_sites)
    by_id = {probe.id: probe for probe in all_probes}

    chosen: list[Probe] = []
    seen: set[Hashable] = set()
    total_cost: Any = 0
    if isinstance(selected, (str, bytes, Probe, Mapping)):
        selected = [selected]
    for item in selected or ():
        probe_id: Hashable | None
        if isinstance(item, Probe):
            probe_id = item.id
        elif isinstance(item, Mapping):
            probe_id = item.get("id") or item.get("probe_id") or item.get("name")
        else:
            probe_id = item

        if probe_id not in by_id:
            raise ValidationError(f"selected probe id is unknown: {probe_id!r}")
        if probe_id in seen:
            continue
        seen.add(probe_id)
        probe = by_id[probe_id]
        chosen.append(probe)
        total_cost += probe.cost

    signatures = signatures_for_probes(chosen, faults)
    pairs = pair_index_map(len(faults))
    ambiguous: list[tuple[Hashable, Hashable]] = []
    distinguished: list[tuple[Hashable, Hashable]] = []
    for left, right in pairs:
        pair = (faults[left].id, faults[right].id)
        if signatures[faults[left].id] == signatures[faults[right].id]:
            ambiguous.append(pair)
        else:
            distinguished.append(pair)

    groups: dict[tuple[str, ...], list[Hashable]] = {}
    for fault in faults:
        groups.setdefault(signatures[fault.id], []).append(fault.id)

    return {
        "selected_probes": tuple(chosen),
        "selected_ids": tuple(probe.id for probe in chosen),
        "total_cost": total_cost,
        "signatures": signatures,
        "equivalence_classes": tuple(tuple(v) for v in groups.values()),
        "ambiguous_pairs": tuple(ambiguous),
        "same_result_pairs": tuple(ambiguous),
        "distinguished_pairs": tuple(distinguished),
        "pair_count": len(pairs),
        "ambiguous_pair_count": len(ambiguous),
        "distinguished_pair_count": len(distinguished),
    }


def _path_dependencies(
    paths: Any, site: Hashable, entry: Hashable
) -> tuple[Hashable, ...] | None:
    if paths is None:
        return ()

    if callable(paths):
        value = paths(site, entry)
        if value is None or value is False:
            return None
        if isinstance(value, Probe):
            return value.dependencies
        return value

    if isinstance(paths, Mapping):
        if site in paths and isinstance(paths[site], Mapping):
            nested = paths[site]
            if entry in nested:
                value = nested[entry]
                return None if value is None or value is False else value

        for key in (
            (site, entry),
            f"{site}->{entry}",
            f"{site}:{entry}",
            f"{site}/{entry}",
        ):
            if key in paths:
                value = paths[key]
                return None if value is None or value is False else value
        return ()

    raise ValidationError("paths must be a mapping, callable, or None")


def build_probes(
    sites: Iterable[Hashable],
    entries: Iterable[Hashable] | None = None,
    *,
    paths: Mapping[Any, Any] | Callable[[Hashable, Hashable], Any] | None = None,
    request_cost: Number | Callable[[Hashable, Hashable], Number] = 1,
    costs: Mapping[Any, Number] | Callable[..., Number] | None = None,
) -> list[Probe]:
    """Build site-by-entry candidate probes from a path specification."""

    if isinstance(sites, (str, bytes)):
        sites = (sites,)
    site_list = tuple(dict.fromkeys(sites or ()))
    if not site_list:
        raise ValidationError("at least one candidate site is required")

    if entries is None:
        entry_values: list[Hashable] = []
        if isinstance(paths, Mapping):
            for key, value in paths.items():
                if isinstance(key, tuple) and len(key) == 2:
                    entry_values.append(key[1])
                elif isinstance(value, Mapping):
                    entry_values.extend(value.keys())
        entries = tuple(dict.fromkeys(entry_values))
    elif isinstance(entries, (str, bytes)):
        entries = (entries,)
    entry_list = tuple(dict.fromkeys(entries))
    if not entry_list:
        raise ValidationError("at least one entry is required")

    raw_probes: list[dict[str, Any]] = []
    for site in site_list:
        for entry in entry_list:
            dependencies = _path_dependencies(paths, site, entry)
            if dependencies is None:
                continue
            raw_probes.append(
                {
                    "id": f"{site}->{entry}",
                    "site": site,
                    "entry": entry,
                    "dependencies": dependencies,
                }
            )

    return normalize_probes(
        raw_probes, request_cost=request_cost, cost_for=costs
    )


def plan_placement(
    probes: Iterable[Probe | Mapping[str, Any] | tuple[Any, ...]] | None = None,
    faults: Iterable[Fault | Mapping[str, Any] | str | tuple[Any, ...]] | None = None,
    *,
    sites: Iterable[Hashable] | None = None,
    candidate_sites: Iterable[Hashable] | None = None,
    entries: Iterable[Hashable] | None = None,
    required_entries: Iterable[Hashable] | None = None,
    paths: Mapping[Any, Any] | Callable[[Hashable, Hashable], Any] | None = None,
    budget: Number | None = None,
    request_cost: Number | Callable[[Hashable, Hashable], Number] = 1,
    costs: Mapping[Any, Number] | Callable[..., Number] | None = None,
) -> PlacementResult:
    """All-in-one placement API accepting sites/entries/paths or probe dicts."""

    site_ids = sites if sites is not None else candidate_sites

    if probes is None:
        if site_ids is None:
            raise ValidationError("provide either probes or sites")
        probe_inputs: Any = build_probes(
            site_ids,
            entries,
            paths=paths,
            request_cost=request_cost,
            costs=costs,
        )
    else:
        probe_inputs = probes

    if required_entries is None and entries is not None:
        required_entries = entries

    return plan_probes(
        probe_inputs,
        faults,
        budget=budget,
        required_entries=required_entries,
        request_cost=request_cost,
        cost_for=costs,
        known_sites=site_ids,
    )


class ProbePlacementPlanner:
    """Stateful placement component for a fixed synthetic patrol inventory."""

    def __init__(
        self,
        probes: Iterable[Probe | Mapping[str, Any] | tuple[Any, ...]] | None = None,
        faults: Iterable[Fault | Mapping[str, Any] | str | tuple[Any, ...]] | None = None,
        *,
        sites: Iterable[Hashable] | None = None,
        entries: Iterable[Hashable] | None = None,
        required_entries: Iterable[Hashable] | None = None,
        paths: Mapping[Any, Any] | Callable[[Hashable, Hashable], Any] | None = None,
        budget: Number | None = None,
        request_cost: Number | Callable[[Hashable, Hashable], Number] = 1,
        costs: Mapping[Any, Number] | Callable[..., Number] | None = None,
    ) -> None:
        self.probes = probes
        self.faults = faults
        self.sites = sites
        self.entries = entries
        self.required_entries = required_entries
        self.paths = paths
        self.budget = budget
        self.request_cost = request_cost
        self.costs = costs

    def plan(self, **overrides: Any) -> PlacementResult:
        """Run placement; keyword arguments override constructor defaults."""

        params: dict[str, Any] = {
            "probes": self.probes,
            "faults": self.faults,
            "sites": self.sites,
            "entries": self.entries,
            "required_entries": self.required_entries,
            "paths": self.paths,
            "budget": self.budget,
            "request_cost": self.request_cost,
            "costs": self.costs,
        }
        params.update(overrides)
        return plan_placement(**params)

    def select(self, **overrides: Any) -> PlacementResult:
        return self.plan(**overrides)

    def evaluate(
        self,
        selected: Iterable[Hashable | Probe | Mapping[str, Any]],
        **overrides: Any,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "probes": overrides.pop("probes", self.probes),
            "faults": overrides.pop("faults", self.faults),
            "request_cost": overrides.pop("request_cost", self.request_cost),
            "known_sites": overrides.pop("known_sites", self.sites),
        }
        if overrides:
            raise ValidationError(f"unknown evaluate() options: {sorted(overrides)}")
        return evaluate_selection(selected=selected, **params)

    __call__ = plan


def solve(payload: Mapping[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    """Dictionary-in/dictionary-out entry point for JSON-like callers."""

    data: dict[str, Any] = dict(payload or {})
    data.update(overrides)

    probes = data.get("probes", data.get("candidate_probes"))
    sites = data.get("sites", data.get("candidate_sites"))
    entries = data.get("entries", data.get("entry_points"))
    required_entries = data.get("required_entries", data.get("targets", entries))
    faults = data.get("faults", data.get("fault_list", []))
    budget = data.get("budget", data.get("cost_budget"))
    request_cost = data.get(
        "request_cost", data.get("cost_per_request", data.get("default_cost", 1))
    )
    costs = data.get("costs", data.get("cost_for"))
    paths = data.get("paths", data.get("dependency_paths"))

    result = plan_placement(
        probes=probes,
        faults=faults,
        sites=sites,
        entries=entries,
        required_entries=required_entries,
        paths=paths,
        budget=budget,
        request_cost=request_cost,
        costs=costs,
    )
    return result.to_dict()

