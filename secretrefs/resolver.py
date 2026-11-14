"""Layered reference resolution for one deployment.

Resolution is deterministic and purely structural: it maps each variable to a
registered reference identifier, a version and the config layer that supplied
it.  Alias entries are followed edge by edge so that failures can quote the
full chain that was traversed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .model import AliasTarget, Binding, CatalogEntry, DeploymentInputs, PathKey

LAYER_COMMON = "common"
LAYER_ENVIRONMENT = "environment"
LAYER_SERVICE = "service"


@dataclass
class Diagnostic:
    """A structural problem found while resolving one service."""

    kind: str
    variable: str
    layer: str
    ref: str
    message: str
    chain: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        data: dict = {
            "kind": self.kind,
            "variable": self.variable,
            "layer": self.layer,
            "ref": self.ref,
            "message": self.message,
        }
        if self.chain:
            data["chain"] = self.chain
        if self.details:
            data["details"] = self.details
        return data


@dataclass
class Resolution:
    """Outcome of resolving one binding, before it becomes a plan entry."""

    ok: bool
    ref: str
    version: int | None = None
    canonical_ref: str | None = None
    chain: list[dict] = field(default_factory=list)
    reason: str | None = None
    message: str | None = None
    version_rule: str | None = None


def _step(environment: str, service: str, ref: str, version: int | None = None) -> dict:
    step = {"environment": environment, "service": service, "ref": ref}
    if version is not None:
        step["version"] = version
    return step


def _find_elsewhere(
    catalog: dict[PathKey, CatalogEntry], environment: str, service: str, ref: str
) -> list[PathKey]:
    """Return catalog keys holding ``ref`` outside the requested identity.

    Same-environment matches sort first so error messages report the least
    surprising place the reference was actually found.
    """

    hits = [
        key
        for key in catalog
        if key[2] == ref and (key[0] != environment or key[1] != service)
    ]
    return sorted(hits, key=lambda key: (key[0] != environment, key[0], key[1]))


def _select_version(
    entry: CatalogEntry, requested: int | None
) -> tuple[int | None, str | None, str | None]:
    """Pick the concrete version for a leaf entry.

    Explicit versions must be declared.  Without one, ``current`` is used when
    present, otherwise the highest declared version.  The rule is surfaced so
    downstream readers never have to guess why a version was chosen.
    """

    if requested is not None:
        if requested not in entry.versions:
            return None, (
                f"version {requested} is not registered; available versions: "
                f"{list(entry.versions)}"
            ), None
        return requested, None, "explicit"
    if entry.current is not None:
        return entry.current, None, "catalog_current"
    if entry.versions:
        return max(entry.versions), None, "catalog_highest"
    return None, "reference has no declared versions", None


def resolve_reference(
    catalog: dict[PathKey, CatalogEntry],
    environment: str,
    service: str,
    ref: str,
    version: int | None,
    *,
    scope_environment: str,
) -> Resolution:
    """Resolve ``ref`` for one service identity, following aliases.

    ``scope_environment`` is the deployment environment; every catalog lookup
    must stay inside it and inside the requesting service identity.
    """

    chain: list[dict] = []
    seen: set[PathKey] = set()
    current_ref = ref
    current_version = version
    current_service = service
    current_environment = environment

    while True:
        key = (current_environment, current_service, current_ref)
        chain.append(_step(current_environment, current_service, current_ref, current_version))

        if key in seen:
            return Resolution(
                ok=False,
                ref=ref,
                chain=chain,
                reason="alias_cycle",
                message=(
                    "alias cycle detected while resolving "
                    f"{environment}/{service}/{ref}: "
                    + " -> ".join(
                        f"{s['environment']}/{s['service']}/{s['ref']}" for s in chain
                    )
                ),
            )
        seen.add(key)

        entry = catalog.get(key)
        if entry is None:
            elsewhere = _find_elsewhere(catalog, current_environment, current_service, current_ref)
            if elsewhere:
                other = elsewhere[0]
                chain.append(_step(other[0], other[1], other[2], current_version))
                if other[0] != current_environment:
                    return Resolution(
                        ok=False,
                        ref=ref,
                        chain=chain,
                        reason="cross_environment",
                        message=(
                            f"reference {current_ref!r} is registered under environment "
                            f"{other[0]!r}, not the deployment environment "
                            f"{scope_environment!r}: "
                            + " -> ".join(
                                f"{s['environment']}/{s['service']}/{s['ref']}" for s in chain
                            )
                        ),
                    )
                return Resolution(
                    ok=False,
                    ref=ref,
                    chain=chain,
                    reason="identity_mismatch",
                    message=(
                        f"reference {current_ref!r} is registered under service "
                        f"{other[1]!r}, not {current_service!r}: "
                        + " -> ".join(
                            f"{s['environment']}/{s['service']}/{s['ref']}" for s in chain
                        )
                    ),
                )
            return Resolution(
                ok=False,
                ref=ref,
                chain=chain,
                reason="not_registered",
                message=(
                    f"reference {current_ref!r} is not registered for "
                    f"{current_environment}/{current_service}"
                ),
            )

        alias: AliasTarget | None = entry.alias_of
        if alias is None:
            selected, problem, rule = _select_version(entry, current_version)
            if problem is not None:
                return Resolution(
                    ok=False,
                    ref=ref,
                    chain=chain,
                    reason="version_unavailable",
                    message=(
                        f"cannot resolve {current_environment}/{current_service}/"
                        f"{current_ref} version {current_version}: {problem}"
                    ),
                )
            return Resolution(
                ok=True,
                ref=ref,
                version=selected,
                canonical_ref=current_ref,
                chain=chain,
                version_rule=rule,
            )

        target_key = (alias.environment, alias.service, alias.ref)
        target_step = _step(alias.environment, alias.service, alias.ref, current_version)
        if alias.environment != scope_environment:
            chain.append(target_step)
            return Resolution(
                ok=False,
                ref=ref,
                chain=chain,
                reason="cross_environment",
                message=(
                    f"alias target {alias.environment}/{alias.service}/{alias.ref} leaves "
                    f"the deployment environment {scope_environment!r}: "
                    + " -> ".join(
                        f"{s['environment']}/{s['service']}/{s['ref']}" for s in chain
                    )
                ),
            )
        if alias.service != current_service:
            chain.append(target_step)
            return Resolution(
                ok=False,
                ref=ref,
                chain=chain,
                reason="identity_mismatch",
                message=(
                    f"alias target {alias.environment}/{alias.service}/{alias.ref} belongs to "
                    f"another service identity, expected {current_service!r}: "
                    + " -> ".join(
                        f"{s['environment']}/{s['service']}/{s['ref']}" for s in chain
                    )
                ),
            )
        if target_key not in catalog:
            chain.append(target_step)
            elsewhere = _find_elsewhere(
                catalog, alias.environment, alias.service, alias.ref
            )
            if elsewhere:
                chain.append(_step(elsewhere[0][0], elsewhere[0][1], elsewhere[0][2]))
                if elsewhere[0][0] != scope_environment:
                    return Resolution(
                        ok=False,
                        ref=ref,
                        chain=chain,
                        reason="cross_environment",
                        message=(
                            f"alias target {alias.environment}/{alias.service}/{alias.ref} is "
                            f"only registered under environment {elsewhere[0][0]!r}: "
                            + " -> ".join(
                                f"{s['environment']}/{s['service']}/{s['ref']}" for s in chain
                            )
                        ),
                    )
                return Resolution(
                    ok=False,
                    ref=ref,
                    chain=chain,
                    reason="identity_mismatch",
                    message=(
                        f"alias target {alias.environment}/{alias.service}/{alias.ref} is "
                        f"only registered under service {elsewhere[0][1]!r}: "
                        + " -> ".join(
                            f"{s['environment']}/{s['service']}/{s['ref']}" for s in chain
                        )
                    ),
                )
            return Resolution(
                ok=False,
                ref=ref,
                chain=chain,
                reason="dangling_alias",
                message=(
                    f"alias target {alias.environment}/{alias.service}/{alias.ref} is not "
                    "registered: "
                    + " -> ".join(
                        f"{s['environment']}/{s['service']}/{s['ref']}" for s in chain
                    )
                ),
            )

        current_environment = alias.environment
        current_service = alias.service
        current_ref = alias.ref


def _layer_bindings(
    inputs: DeploymentInputs, service: str
) -> list[tuple[str, tuple[Binding, ...]]]:
    return [
        (LAYER_COMMON, inputs.common),
        (LAYER_ENVIRONMENT, inputs.environment_overrides),
        (LAYER_SERVICE, inputs.service_overrides.get(service, ())),
    ]


def _detect_layer_conflicts(
    service: str, layer_name: str, bindings: Iterable[Binding]
) -> list[Diagnostic]:
    """Report duplicate variables declared inside a single layer.

    A conflict only blocks when it sits in the layer that actually wins for
    that variable; a duplicate in a fully overshadowed layer is still reported
    (it is a real authoring smell) but cannot change the injection plan.
    """

    grouped: dict[str, list[Binding]] = {}
    for binding in bindings:
        grouped.setdefault(binding.variable, []).append(binding)
    conflicts: list[Diagnostic] = []
    for variable in sorted(grouped):
        variants = grouped[variable]
        if len(variants) < 2:
            continue
        signatures = {variant.signature() for variant in variants}
        conflicts.append(
            Diagnostic(
                kind="layer_conflict",
                variable=variable,
                layer=layer_name,
                ref=variants[0].ref,
                message=(
                    f"variable {variable!r} is declared {len(variants)} times in the "
                    f"{layer_name} layer for service {service!r} with "
                    + ("different targets" if len(signatures) > 1 else "identical targets")
                ),
                details={
                    "declarations": [
                        {
                            "variable": v.variable,
                            "ref": v.ref,
                            "version": v.version,
                            "required": v.required,
                        }
                        for v in variants
                    ]
                },
            )
        )
    return conflicts


def _winning_bindings(
    layers: list[tuple[str, tuple[Binding, ...]]]
) -> tuple[dict[str, tuple[str, Binding]], dict[str, list[dict]]]:
    """Apply layer precedence and record what each winner shadowed."""

    winning: dict[str, tuple[str, Binding]] = {}
    shadowed: dict[str, list[dict]] = {}
    for layer_name, bindings in layers:
        for binding in bindings:
            previous = winning.get(binding.variable)
            if previous is not None:
                prev_layer, prev_binding = previous
                shadowed.setdefault(binding.variable, []).append(
                    {
                        "layer": prev_layer,
                        "ref": prev_binding.ref,
                        "version": prev_binding.version,
                    }
                )
            winning[binding.variable] = (layer_name, binding)
    return winning, shadowed


def build_service_plan(inputs: DeploymentInputs, service: str) -> dict:
    """Build the injection plan for a single service."""

    layers = _layer_bindings(inputs, service)
    conflicts: list[Diagnostic] = []
    for layer_name, bindings in layers:
        conflicts.extend(_detect_layer_conflicts(service, layer_name, bindings))

    winning, shadowed = _winning_bindings(layers)

    # A conflict blocks only when it is in the winning layer for its variable.
    # A duplicate wholly shadowed by a higher layer stays reported as a warning.
    blocking_conflicts = {
        diag.variable for diag in conflicts if winning[diag.variable][0] == diag.layer
    }

    variables: list[dict] = []
    unresolved: list[dict] = []
    diagnostics: list[Diagnostic] = []

    for variable in sorted(winning):
        layer_name, binding = winning[variable]
        entry: dict = {
            "variable": variable,
            "source_layer": layer_name,
            "required": binding.required,
            "ref": binding.ref,
        }
        if binding.version is not None:
            entry["requested_version"] = binding.version

        if variable in blocking_conflicts:
            entry["status"] = "unresolved"
            entry["reason"] = "layer_conflict"
            entry["blocking"] = True
            unresolved.append(entry)
            continue

        outcome = resolve_reference(
            inputs.catalog,
            inputs.environment,
            service,
            binding.ref,
            binding.version,
            scope_environment=inputs.environment,
        )
        if outcome.ok:
            entry["status"] = "resolved"
            entry["version"] = outcome.version
            entry["resolved_ref"] = outcome.canonical_ref
            entry["version_rule"] = outcome.version_rule
            entry["aliased"] = outcome.canonical_ref != binding.ref
            if entry["aliased"]:
                entry["resolution_chain"] = outcome.chain
            if variable in shadowed:
                entry["overrides"] = list(reversed(shadowed[variable]))
            variables.append(entry)
            continue

        entry["status"] = "unresolved"
        entry["reason"] = outcome.reason
        entry["message"] = outcome.message
        entry["resolution_chain"] = outcome.chain
        entry["blocking"] = binding.required
        unresolved.append(entry)
        diagnostics.append(
            Diagnostic(
                kind=outcome.reason or "unresolved",
                variable=variable,
                layer=layer_name,
                ref=binding.ref,
                message=outcome.message or "reference could not be resolved",
                chain=outcome.chain,
                details={"required": binding.required},
            )
        )

    blocking = [item for item in unresolved if item.get("blocking")]
    executable = not blocking

    shaded_conflicts = [
        diag.as_dict() for diag in conflicts if diag.variable not in blocking_conflicts
    ]

    return {
        "service": service,
        "executable": executable,
        "variables": variables,
        "unresolved": unresolved,
        "conflicts": [diag.as_dict() for diag in conflicts],
        "shadowed_conflicts": shaded_conflicts,
        "diagnostics": [diag.as_dict() for diag in diagnostics],
        "summary": {
            "resolved": len(variables),
            "unresolved": len(unresolved),
            "blocking": len(blocking),
            "conflicts": len(conflicts),
            "shadowed_conflicts": len(shaded_conflicts),
        },
    }


def _same_name_report(plans: list[dict]) -> list[dict]:
    """Group every emitted variable that appears in two or more services.

    Deployment staff need to see, before rollout, which services receive a
    variable of the same name but from a different reference identity or
    version.  Uniform names are reported too, but flagged as ``uniform`` so the
    distinction is explicit rather than implied by silence.  Variables that
    only one service receives cannot be a cross-service duplicate and are
    omitted.
    """

    by_name: dict[str, list[dict]] = {}
    for plan in plans:
        for variable in plan["variables"]:
            by_name.setdefault(variable["variable"], []).append(
                {
                    "service": plan["service"],
                    "ref": variable["resolved_ref"],
                    "version": variable["version"],
                    "source_layer": variable["source_layer"],
                }
            )
    report: list[dict] = []
    for name in sorted(by_name):
        targets = by_name[name]
        if len(targets) < 2:
            continue
        distinct = {(item["ref"], item["version"]) for item in targets}
        layers = {(item["source_layer"]) for item in targets}
        report.append(
            {
                "variable": name,
                "uniform": len(distinct) == 1,
                "distinct_targets": len(distinct),
                "distinct_layers": sorted(layers),
                "services": targets,
            }
        )
    return report


def build_plan(inputs: DeploymentInputs) -> dict:
    """Build the deployment-wide plan, preserving per-service independence."""

    plans = [build_service_plan(inputs, service) for service in inputs.services]
    executable = all(plan["executable"] for plan in plans)
    same_name = _same_name_report(plans)
    collisions = [entry for entry in same_name if not entry["uniform"]]
    return {
        "environment": inputs.environment,
        "executable": executable,
        "services": plans,
        "same_name_variables": same_name,
        "same_name_collisions": collisions,
        "summary": {
            "services": len(plans),
            "executable_services": sum(1 for plan in plans if plan["executable"]),
            "blocked_services": sum(1 for plan in plans if not plan["executable"]),
            "same_name_collisions": len(collisions),
        },
    }


def plan_exit_code(plan: dict) -> int:
    """0 when every service plan is executable, 1 when at least one is blocked."""

    return 0 if plan["executable"] else 1
