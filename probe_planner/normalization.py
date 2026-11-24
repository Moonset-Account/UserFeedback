"""Flexible normalization for plain-data and object-based API inputs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, Hashable, Iterable

from .errors import ValidationError
from .model import Fault, Probe, validate_cost

Number = int | float | Decimal


def _first_key(data: Mapping[str, Any], keys: Iterable[str]) -> tuple[bool, Any]:
    for key in keys:
        if key in data:
            return True, data[key]
    return False, None


def _mapping_cost(
    cost_map: Mapping[Any, Any],
    *,
    probe_id: Hashable | None,
    site: Hashable,
    entry: Hashable,
) -> tuple[bool, Any]:
    candidates: list[Any] = [
        probe_id,
        (site, entry),
        f"{site}->{entry}",
        f"{site}:{entry}",
        f"{site}/{entry}",
        f"{site}.{entry}",
    ]
    for key in candidates:
        if key is not None and key in cost_map:
            return True, cost_map[key]
    return False, None


def _resolve_dynamic_cost(
    source: Any,
    *,
    probe_id: Hashable | None,
    site: Hashable,
    entry: Hashable,
) -> Any:
    if isinstance(source, Mapping):
        found, value = _mapping_cost(
            source, probe_id=probe_id, site=site, entry=entry
        )
        if found:
            return value
        return None
    if callable(source):
        return source(site, entry)
    return source


def normalize_probes(
    probes: Iterable[Probe | Mapping[str, Any] | tuple[Any, ...]],
    *,
    request_cost: Number | Callable[[Hashable, Hashable], Number] = 1,
    cost_for: Mapping[Any, Number] | Callable[..., Number] | None = None,
) -> list[Probe]:
    """Normalize supported probe inputs into unique-id :class:`Probe` objects."""

    if probes is None:
        return []
    if isinstance(probes, (Probe, Mapping)):
        probes = [probes]

    normalized: list[Probe] = []
    seen_ids: set[Hashable] = set()

    for index, raw in enumerate(probes):
        default_id = f"P{index + 1}"

        if isinstance(raw, Probe):
            site = raw.site
            entry = raw.entry
            dependencies = raw.dependencies
            explicit_cost = raw.cost
            probe_id = raw.id or default_id
        elif isinstance(raw, Mapping):
            found_site, site = _first_key(
                raw, ("site", "station", "probe_site", "source", "from")
            )
            found_entry, entry = _first_key(
                raw,
                ("entry", "entry_point", "endpoint", "target", "entrance", "to"),
            )
            if not found_site:
                raise ValidationError(f"probe #{index + 1} is missing a site")
            if not found_entry:
                raise ValidationError(f"probe #{index + 1} is missing an entry")

            _, dependencies = _first_key(
                raw,
                (
                    "dependencies",
                    "dependency",
                    "path",
                    "path_resources",
                    "depends_on",
                    "traversed_dependencies",
                ),
            )
            has_cost, explicit_cost = _first_key(
                raw, ("cost", "request_cost", "price", "cost_per_request")
            )
            if not has_cost:
                explicit_cost = None
            _, probe_id = _first_key(raw, ("id", "probe_id", "name", "key"))
            probe_id = probe_id or default_id
        elif isinstance(raw, (tuple, list)):
            if len(raw) == 2:
                site, entry = raw
                dependencies = ()
                explicit_cost = None
            elif len(raw) == 3:
                site, entry, third = raw
                if isinstance(third, (int, float, Decimal)) and not isinstance(
                    third, bool
                ):
                    dependencies = ()
                    explicit_cost = third
                else:
                    dependencies = third
                    explicit_cost = None
            elif len(raw) == 4:
                site, entry, dependencies, explicit_cost = raw
            else:
                raise ValidationError(
                    f"probe tuple #{index + 1} must have 2, 3, or 4 fields"
                )
            probe_id = default_id
        else:
            raise ValidationError(
                f"probe #{index + 1} must be a Probe, mapping, or tuple"
            )

        if probe_id in seen_ids:
            raise ValidationError(f"duplicate probe id: {probe_id!r}")
        seen_ids.add(probe_id)

        cost_value: Any = explicit_cost
        if cost_value is None and cost_for is not None:
            cost_value = _resolve_dynamic_cost(
                cost_for, probe_id=probe_id, site=site, entry=entry
            )
        if cost_value is None:
            cost_value = _resolve_dynamic_cost(
                request_cost, probe_id=probe_id, site=site, entry=entry
            )
        if cost_value is None:
            cost_value = 1

        cost = validate_cost(
            cost_value, field_name=f"cost for probe {probe_id!r}"
        )

        try:
            probe = Probe(
                id=probe_id,
                site=site,
                entry=entry,
                dependencies=dependencies or (),
                cost=cost,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        normalized.append(probe)

    return normalized


_SITE_KIND_VALUES = {
    "site",
    "station",
    "site_outage",
    "station_outage",
    "site_down",
    "unavailable",
    "outage",
}
_RESOURCE_KIND_VALUES = {
    "dependency",
    "dep",
    "resource",
    "service",
    "node",
    "entry",
    "endpoint",
    "business",
    "business_failure",
    "dependency_failure",
    "resource_failure",
}


def _parse_prefixed_fault(value: str) -> tuple[str, str] | None:
    if ":" not in value:
        return None
    prefix, target = value.split(":", 1)
    prefix = prefix.strip().lower()
    target = target.strip()
    if prefix in _SITE_KIND_VALUES or prefix in {"site", "station"}:
        return "site", target
    if prefix in _RESOURCE_KIND_VALUES or prefix in {"dep", "dependency"}:
        return "resource", target
    return None


def normalize_faults(
    faults: Iterable[Fault | Mapping[str, Any] | tuple[Any, ...] | str],
    *,
    known_sites: Iterable[Hashable] | None = None,
) -> list[Fault]:
    """Normalize fault objects, dicts, tuples, and prefixed strings."""

    if faults is None:
        return []
    if isinstance(faults, (Fault, Mapping, str)):
        faults = [faults]
    if isinstance(known_sites, (str, bytes)):
        known_sites = {known_sites}

    site_ids = set(known_sites or [])
    normalized: list[Fault] = []
    seen_ids: set[Hashable] = set()

    for index, raw in enumerate(faults):
        default_id = f"F{index + 1}"

        if isinstance(raw, Fault):
            fault = raw
        elif isinstance(raw, str):
            parsed = _parse_prefixed_fault(raw)
            if parsed is not None:
                kind, target = parsed
                if kind == "site":
                    fault = Fault.site_outage(target, id=raw)
                else:
                    fault = Fault.resource_failure(target, id=raw)
            elif raw in site_ids:
                fault = Fault.site_outage(raw, id=raw)
            else:
                fault = Fault.resource_failure(raw, id=raw)
        elif isinstance(raw, Mapping):
            _, fault_id = _first_key(raw, ("id", "fault_id", "name", "key"))
            fault_id = fault_id or default_id

            _, kind_value = _first_key(raw, ("kind", "type", "fault_type", "category"))
            kind = str(kind_value).strip().lower() if kind_value is not None else ""

            site_values: list[Hashable] = []
            found, values = _first_key(
                raw,
                (
                    "unavailable_sites",
                    "down_sites",
                    "sites",
                    "site",
                    "station",
                    "stations",
                ),
            )
            if found and values is not None:
                site_values.extend(
                    [values]
                    if isinstance(values, (str, bytes))
                    else list(values)
                )

            resource_values: list[Hashable] = []
            found, values = _first_key(
                raw,
                (
                    "failed_resources",
                    "resources",
                    "resource",
                    "dependencies",
                    "dependency",
                    "affected_resources",
                    "affected",
                    "target",
                    "targets",
                ),
            )
            if found and values is not None:
                resource_values.extend(
                    [values]
                    if isinstance(values, (str, bytes))
                    else list(values)
                )

            if not kind:
                if site_values and not resource_values:
                    kind = "site"
                elif resource_values and not site_values:
                    kind = "resource"

            if kind in _SITE_KIND_VALUES:
                if not site_values:
                    _, target = _first_key(raw, ("target", "site", "station", "id", "name"))
                    if target is not None:
                        site_values = [target]
                if not site_values:
                    raise ValidationError(
                        f"site fault {fault_id!r} requires a site identifier"
                    )
                fault = Fault(
                    id=fault_id, unavailable_sites=frozenset(site_values)
                )
            elif kind in _RESOURCE_KIND_VALUES:
                if not resource_values:
                    _, target = _first_key(
                        raw,
                        ("target", "resource", "dependency", "entry", "id", "name"),
                    )
                    if target is not None:
                        resource_values = [target]
                if not resource_values:
                    raise ValidationError(
                        f"resource fault {fault_id!r} requires a resource identifier"
                    )
                fault = Fault(
                    id=fault_id, failed_resources=frozenset(resource_values)
                )
            elif site_values or resource_values:
                fault = Fault(
                    id=fault_id,
                    unavailable_sites=frozenset(site_values),
                    failed_resources=frozenset(resource_values),
                )
            else:
                raise ValidationError(
                    f"fault {fault_id!r} must declare a site or failed resource"
                )
        elif isinstance(raw, tuple):
            if len(raw) == 2:
                fault_id, target = raw
                if target in site_ids:
                    fault = Fault.site_outage(target, id=fault_id)
                else:
                    fault = Fault.resource_failure(target, id=fault_id)
            elif len(raw) == 3:
                fault_id, kind, target = raw
                if str(kind).lower() in _SITE_KIND_VALUES:
                    fault = Fault.site_outage(target, id=fault_id)
                else:
                    fault = Fault.resource_failure(target, id=fault_id)
            else:
                raise ValidationError(
                    f"fault tuple #{index + 1} must have 2 or 3 fields"
                )
        else:
            raise ValidationError(
                f"fault #{index + 1} must be a Fault, mapping, string, or tuple"
            )

        if fault.id in seen_ids:
            raise ValidationError(f"duplicate fault id: {fault.id!r}")
        seen_ids.add(fault.id)
        normalized.append(fault)

    return normalized
