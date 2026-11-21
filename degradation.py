"""Capacity-constrained degradation planning.

The module models a small capacity-planning problem:

* up to 10 independent entry points (for example, recommendation and search);
* each entry has at most 3 ordered tiers, from the best tier (index ``0``) to
  the most degraded one; a per-entry floor says how far the entry may degrade;
* each tier has an integer business benefit and private CPU/memory costs;
* tiers may require shared components.  A component has CPU/memory costs and
  zero or more component dependencies.  A component is enabled at most once,
  no matter how many entries need it, and enabling it also enables all of its
  transitive dependencies.

A plan is feasible when the sum of private costs and the costs of the enabled
component closure fit into the remaining CPU and memory capacity.  Among
feasible plans the solver maximises total benefit, then minimises total CPU
(including shared components).  Any remaining tie is broken deterministically
by taking the lexicographically first tier-index vector in input order.

The search space is bounded by ``3 ** 10 == 59049`` assignments, so the
implementation enumerates every assignment.  It is exact: no greedy or
heuristic approximation is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "MAX_ENTRIES",
    "MAX_TIERS",
    "PlannerError",
    "InputError",
    "InfeasiblePlanError",
    "Component",
    "Tier",
    "Entry",
    "Selection",
    "Plan",
    "solve",
]


MAX_ENTRIES = 10
MAX_TIERS = 3


class PlannerError(ValueError):
    """Base class for all planner errors."""


class InputError(PlannerError):
    """The supplied problem description is malformed."""


class InfeasiblePlanError(PlannerError):
    """No assignment satisfies the per-entry floors and both capacities."""


def _require_int(value: Any, field_name: str, *, minimum: int | None = None) -> int:
    # bool is a subclass of int, but it is almost certainly a data error here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{field_name} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise InputError(f"{field_name} must be >= {minimum}, got {value!r}")
    return value


def _require_name(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise InputError(f"{field_name} must be a non-empty string, got {value!r}")
    return value


def _name_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    try:
        items = tuple(value)
    except TypeError as exc:
        raise InputError(f"{field_name} must be an iterable of names") from exc
    for item in items:
        _require_name(item, field_name)
    # Duplicate requirements are harmless, but canonicalising keeps the model
    # (and error messages) tidy.
    return tuple(dict.fromkeys(items))


@dataclass(frozen=True)
class Component:
    """A shared component that can be enabled once and reused.

    ``requires`` lists direct dependencies.  Enabling the component also
    enables those dependencies transitively.
    """

    name: str
    cpu: int
    memory: int
    requires: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_name(self.name, "component name"))
        object.__setattr__(
            self, "cpu", _require_int(self.cpu, f"component {self.name!r} cpu", minimum=0)
        )
        object.__setattr__(
            self,
            "memory",
            _require_int(self.memory, f"component {self.name!r} memory", minimum=0),
        )
        object.__setattr__(
            self,
            "requires",
            _name_tuple(self.requires, f"component {self.name!r} requires"),
        )


@dataclass(frozen=True)
class Tier:
    """One degradation level of an entry point.

    Tiers are ordered from best (index 0) to most degraded.  The benefit does
    not have to be monotone in the index; the solver uses the supplied values
    literally.  ``components`` names shared components required by this tier.
    """

    benefit: int
    cpu: int
    memory: int
    components: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "benefit", _require_int(self.benefit, "tier benefit"))
        object.__setattr__(self, "cpu", _require_int(self.cpu, "tier cpu", minimum=0))
        object.__setattr__(
            self, "memory", _require_int(self.memory, "tier memory", minimum=0)
        )
        object.__setattr__(
            self, "components", _name_tuple(self.components, "tier components")
        )


@dataclass(frozen=True)
class Entry:
    """An entry point and its degradation ladder.

    ``floor`` is the largest tier index the entry may use.  ``None`` means the
    last supplied tier, i.e. the entry may use every tier.  A floor of ``0``
    means the entry cannot degrade at all.
    """

    name: str
    tiers: tuple[Tier, ...]
    floor: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_name(self.name, "entry name"))
        if isinstance(self.tiers, Tier):
            tiers: tuple[Tier, ...] = (self.tiers,)
        else:
            try:
                tiers = tuple(self.tiers)
            except TypeError as exc:
                raise InputError(f"entry {self.name!r} tiers must be iterable") from exc
        if not tiers:
            raise InputError(f"entry {self.name!r} must have at least one tier")
        if len(tiers) > MAX_TIERS:
            raise InputError(
                f"entry {self.name!r} has {len(tiers)} tiers; at most {MAX_TIERS} are allowed"
            )
        for tier in tiers:
            if not isinstance(tier, Tier):
                raise InputError(f"entry {self.name!r} contains a non-Tier value")
        object.__setattr__(self, "tiers", tiers)

        floor = len(tiers) - 1 if self.floor is None else self.floor
        floor = _require_int(floor, f"entry {self.name!r} floor", minimum=0)
        if floor >= len(tiers):
            raise InputError(
                f"entry {self.name!r} floor {floor} is outside its "
                f"{len(tiers)} tier(s)"
            )
        object.__setattr__(self, "floor", floor)


@dataclass(frozen=True)
class Selection:
    """The tier chosen for one entry."""

    entry: str
    tier_index: int
    tier: Tier

    @property
    def benefit(self) -> int:
        return self.tier.benefit

    @property
    def cpu(self) -> int:
        return self.tier.cpu

    @property
    def memory(self) -> int:
        return self.tier.memory


@dataclass(frozen=True)
class Plan:
    """An optimal feasible plan and its resource accounting."""

    selections: tuple[Selection, ...]
    enabled_components: frozenset[str]
    total_benefit: int
    total_cpu: int
    total_memory: int
    private_cpu: int
    private_memory: int
    component_cpu: int
    component_memory: int

    @property
    def choices(self) -> dict[str, int]:
        """Map entry name to the chosen tier index."""

        return {selection.entry: selection.tier_index for selection in self.selections}

    def tier_index(self, entry: str) -> int:
        for selection in self.selections:
            if selection.entry == entry:
                return selection.tier_index
        raise KeyError(entry)


def _coerce_component(spec: Any, name_hint: str | None = None) -> Component:
    if isinstance(spec, Component):
        component = spec
    elif isinstance(spec, Mapping):
        name = spec.get("name", name_hint)
        if name is None:
            raise InputError("a component spec needs a name")
        missing = [key for key in ("cpu", "memory") if key not in spec]
        if missing:
            raise InputError(
                f"component {name!r} is missing {', '.join(missing)}"
            )
        requires = spec.get("requires", spec.get("dependencies", spec.get("components", ())))
        component = Component(
            name=name,
            cpu=spec["cpu"],
            memory=spec["memory"],
            requires=requires,
        )
    else:
        raise InputError(f"unsupported component spec: {spec!r}")

    if name_hint is not None and component.name != name_hint:
        raise InputError(
            f"component mapping key {name_hint!r} does not match spec name "
            f"{component.name!r}"
        )
    return component


def _coerce_tier(spec: Any) -> Tier:
    if isinstance(spec, Tier):
        return spec
    if isinstance(spec, Mapping):
        missing = [key for key in ("benefit", "cpu", "memory") if key not in spec]
        if missing:
            raise InputError(f"tier spec is missing {', '.join(missing)}")
        components = spec.get("components", spec.get("requires", ()))
        return Tier(
            benefit=spec["benefit"],
            cpu=spec["cpu"],
            memory=spec["memory"],
            components=components,
        )
    raise InputError(f"unsupported tier spec: {spec!r}")


def _coerce_entry(spec: Any, name_hint: str | None = None) -> Entry:
    if isinstance(spec, Entry):
        entry = spec
    elif isinstance(spec, Mapping):
        name = spec.get("name", name_hint)
        if name is None:
            raise InputError("an entry spec needs a name")
        if "tiers" not in spec:
            raise InputError(f"entry {name!r} is missing tiers")
        entry = Entry(
            name=name,
            tiers=tuple(_coerce_tier(tier) for tier in spec["tiers"]),
            floor=spec.get("floor"),
        )
    elif isinstance(spec, Sequence) and not isinstance(spec, (str, bytes, bytearray)):
        if name_hint is None:
            raise InputError("an entry spec given as a tier sequence needs a name")
        entry = Entry(
            name=name_hint,
            tiers=tuple(_coerce_tier(tier) for tier in spec),
        )
    else:
        raise InputError(f"unsupported entry spec: {spec!r}")

    if name_hint is not None and entry.name != name_hint:
        raise InputError(
            f"entry mapping key {name_hint!r} does not match spec name {entry.name!r}"
        )
    return entry


def _normalise_components(raw: Any) -> dict[str, Component]:
    if raw is None:
        return {}
    if isinstance(raw, Component):
        raw = (raw,)
    if isinstance(raw, Mapping):
        items = [_coerce_component(spec, str(name)) for name, spec in raw.items()]
    else:
        try:
            items = [_coerce_component(spec) for spec in raw]
        except TypeError as exc:
            raise InputError("components must be an iterable or a mapping") from exc

    result: dict[str, Component] = {}
    for component in items:
        if component.name in result:
            raise InputError(f"duplicate component name {component.name!r}")
        result[component.name] = component
    return result


def _normalise_entries(raw: Any) -> tuple[Entry, ...]:
    if raw is None:
        return ()
    if isinstance(raw, Entry):
        raw = (raw,)
    if isinstance(raw, Mapping):
        entries = [_coerce_entry(spec, str(name)) for name, spec in raw.items()]
    else:
        try:
            entries = [_coerce_entry(spec) for spec in raw]
        except TypeError as exc:
            raise InputError("entries must be an iterable or a mapping") from exc

    if len(entries) > MAX_ENTRIES:
        raise InputError(
            f"at most {MAX_ENTRIES} entries are allowed, got {len(entries)}"
        )
    seen: set[str] = set()
    for entry in entries:
        if entry.name in seen:
            raise InputError(f"duplicate entry name {entry.name!r}")
        seen.add(entry.name)
    return tuple(entries)


def _validate_component_graph(components: Mapping[str, Component]) -> None:
    """Check that dependencies exist and that the graph is acyclic."""

    state: dict[str, int] = {name: 0 for name in components}
    for start in components:
        if state[start] != 0:
            continue
        state[start] = 1
        stack: list[tuple[str, Any]] = [(start, iter(components[start].requires))]
        while stack:
            node, dependencies = stack[-1]
            try:
                dependency = next(dependencies)
            except StopIteration:
                state[node] = 2
                stack.pop()
                continue
            if dependency not in components:
                raise InputError(
                    f"component {node!r} depends on unknown component {dependency!r}"
                )
            if state[dependency] == 1:
                raise InputError(
                    f"component dependency cycle involving {node!r} and {dependency!r}"
                )
            if state[dependency] == 0:
                state[dependency] = 1
                stack.append((dependency, iter(components[dependency].requires)))


def _closure_costs(
    components: Mapping[str, Component],
) -> dict[str, frozenset[str]]:
    """Return each component's transitive dependency closure.

    Costs are deliberately *not* precomputed per closure: the solver sums the
    cost of each distinct enabled component exactly once, because one component
    is paid for once even when several tiers (or several dependencies' closures)
    reach it.
    """

    closures: dict[str, frozenset[str]] = {}

    for name in components:
        if name in closures:
            continue
        seen: set[str] = set()
        stack = [name]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(components[current].requires)
        closures[name] = frozenset(seen)

    return closures


def _check_capacity(value: Any, name: str) -> int:
    return _require_int(value, name, minimum=0)


def solve(
    entries: Iterable[Entry] | Mapping[str, Any],
    components: Iterable[Component] | Mapping[str, Any] = (),
    *,
    cpu_capacity: int,
    memory_capacity: int,
) -> Plan:
    """Find the highest-benefit feasible degradation plan.

    Parameters
    ----------
    entries:
        Entry objects, or mappings/sequences accepted by the coercion layer.
        Input order is used for deterministic tie-breaking.
    components:
        Shared components.  ``components`` may be given before they are
        referenced by tiers; references are resolved after normalisation.
    cpu_capacity, memory_capacity:
        Remaining non-negative CPU and memory capacity.

    Returns
    -------
    Plan
        The optimal plan, including private and shared resource accounting.

    Raises
    ------
    InputError
        Malformed input, unknown component reference, or a dependency cycle.
    InfeasiblePlanError
        The per-entry floors alone exceed the capacity.
    """

    cpu_capacity = _check_capacity(cpu_capacity, "cpu_capacity")
    memory_capacity = _check_capacity(memory_capacity, "memory_capacity")

    component_map = _normalise_components(components)
    entry_list = _normalise_entries(entries)

    _validate_component_graph(component_map)

    known_components = set(component_map)
    for entry in entry_list:
        for index, tier in enumerate(entry.tiers):
            for component_name in tier.components:
                if component_name not in known_components:
                    raise InputError(
                        f"entry {entry.name!r} tier {index} requires unknown "
                        f"component {component_name!r}"
                    )

    closures = _closure_costs(component_map)
    component_cpu = {name: component.cpu for name, component in component_map.items()}
    component_memory = {
        name: component.memory for name, component in component_map.items()
    }

    # options[i] holds the tiers entry i is allowed to use, ascending by index.
    # Keeping ascending order makes the first best plan encountered the
    # deterministic lexicographic tie-break winner.
    options: list[list[tuple[Tier, frozenset[str]]]] = []
    for entry in entry_list:
        # ``Entry`` normalises the floor to an int in ``__post_init__``.
        floor = entry.floor
        assert floor is not None
        row: list[tuple[Tier, frozenset[str]]] = []
        for tier in entry.tiers[: floor + 1]:
            if tier.components:
                required: frozenset[str] = frozenset().union(
                    *(closures[name] for name in tier.components)
                )
            else:
                required = frozenset()
            row.append((tier, required))
        options.append(row)

    best_key: tuple[int, int] | None = None
    best: tuple[tuple[int, ...], int, int, int, frozenset[str]] | None = None

    # At most 3 ** 10 leaves; exhaustive enumeration is exact and comfortably
    # small for the problem bounds.
    for choice in product(*(range(len(row)) for row in options)):
        benefit = 0
        private_cpu = 0
        private_memory = 0
        enabled: set[str] = set()
        for entry_index, tier_index in enumerate(choice):
            tier, required = options[entry_index][tier_index]
            benefit += tier.benefit
            private_cpu += tier.cpu
            private_memory += tier.memory
            enabled.update(required)

        # Each distinct enabled component is paid for exactly once, which is
        # what makes sharing cheap and what makes a component's cost vanish
        # only when its last user leaves.
        shared_cpu = sum(component_cpu[name] for name in enabled)
        shared_memory = sum(component_memory[name] for name in enabled)
        total_cpu = private_cpu + shared_cpu
        total_memory = private_memory + shared_memory

        if total_cpu > cpu_capacity or total_memory > memory_capacity:
            continue

        key = (benefit, -total_cpu)
        if best_key is None or key > best_key:
            best_key = key
            best = (
                choice,
                benefit,
                private_cpu,
                private_memory,
                frozenset(enabled),
            )

    if best is None:
        raise InfeasiblePlanError(
            "no tier assignment honors the per-entry floors within "
            f"CPU={cpu_capacity} and memory={memory_capacity}"
        )

    choice, benefit, private_cpu, private_memory, enabled = best
    selections = tuple(
        Selection(
            entry=entry_list[index].name,
            tier_index=tier_index,
            tier=entry_list[index].tiers[tier_index],
        )
        for index, tier_index in enumerate(choice)
    )
    shared_cpu = sum(component_cpu[name] for name in enabled)
    shared_memory = sum(component_memory[name] for name in enabled)
    return Plan(
        selections=selections,
        enabled_components=enabled,
        total_benefit=benefit,
        total_cpu=private_cpu + shared_cpu,
        total_memory=private_memory + shared_memory,
        private_cpu=private_cpu,
        private_memory=private_memory,
        component_cpu=shared_cpu,
        component_memory=shared_memory,
    )
