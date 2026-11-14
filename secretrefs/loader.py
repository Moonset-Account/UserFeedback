"""Load and validate the three deployment input documents.

The three inputs mirror the deployment question directly:

* environment file  - deployment environment, common config, environment overrides
* services file     - the services in this deployment and their service overrides
* catalog file      - the registered reference identities per environment/service

Everything here is about shape and identity; resolution semantics live in
:mod:`secretrefs.resolver`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import InputError
from .model import AliasTarget, Binding, CatalogEntry, DeploymentInputs

_ENV_KEYS = {"environment", "common", "environment_overrides"}
_SERVICES_KEYS = {"services", "service_overrides"}
_CATALOG_KEYS = {"environment", "services", "references"}


def _load_json(path: Path, label: str) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {label} file: {exc}", path=str(path)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InputError(f"{label} file is not valid JSON: {exc}", path=str(path)) from exc
    if not isinstance(data, dict):
        raise InputError(f"{label} file must contain a JSON object", path=str(path))
    return data


def _require_str(value: Any, context: str, path: Path) -> str:
    if not isinstance(value, str) or not value:
        raise InputError(f"{context} must be a non-empty string", path=str(path))
    return value


def _require_str_list(value: Any, context: str, path: Path) -> list[str]:
    if not isinstance(value, list) or not value:
        raise InputError(f"{context} must be a non-empty list of strings", path=str(path))
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise InputError(f"{context}[{index}] must be a non-empty string", path=str(path))
        out.append(item)
    if len(set(out)) != len(out):
        raise InputError(f"{context} contains duplicates", path=str(path))
    return out


def _parse_version(value: Any, context: str, path: Path) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{context} must be an integer version or null", path=str(path))
    if value < 0:
        raise InputError(f"{context} version must be >= 0", path=str(path))
    return value


def _parse_bindings(raw: Any, context: str, path: Path) -> tuple[Binding, ...]:
    """Parse one layer's bindings, preserving duplicate variables.

    Duplicates are *not* an input error: they are a same-layer conflict the
    resolver must report against the affected service.
    """

    if not isinstance(raw, list):
        raise InputError(f"{context} must be a list", path=str(path))
    bindings: list[Binding] = []
    for index, item in enumerate(raw):
        where = f"{context}[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{where} must be an object", path=str(path))
        unknown = set(item) - {"variable", "ref", "version", "required"}
        if unknown:
            raise InputError(f"{where} has unknown keys: {sorted(unknown)}", path=str(path))
        variable = _require_str(item.get("variable"), f"{where}.variable", path)
        ref = _require_str(item.get("ref"), f"{where}.ref", path)
        version = _parse_version(item.get("version"), f"{where}.version", path)
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise InputError(f"{where}.required must be a boolean", path=str(path))
        bindings.append(Binding(variable=variable, ref=ref, version=version, required=required))
    return tuple(bindings)


def _parse_grouped(raw: Any, context: str, path: Path) -> dict[str, tuple[Binding, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise InputError(f"{context} must be an object keyed by service", path=str(path))
    grouped: dict[str, tuple[Binding, ...]] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise InputError(f"{context} keys must be non-empty service names", path=str(path))
        grouped[key] = _parse_bindings(value, f"{context}[{key!r}]", path)
    return grouped


def _parse_environment(data: dict, path: Path) -> tuple[str, tuple[Binding, ...], tuple[Binding, ...]]:
    unknown = set(data) - _ENV_KEYS
    if unknown:
        raise InputError(f"environment file has unknown keys: {sorted(unknown)}", path=str(path))
    if "environment" not in data:
        raise InputError("environment file is missing 'environment'", path=str(path))
    environment = _require_str(data["environment"], "environment", path)
    common = _parse_bindings(data.get("common", []), "common", path)
    overrides = _parse_bindings(
        data.get("environment_overrides", []), "environment_overrides", path
    )
    return environment, common, overrides


def _parse_services(data: dict, path: Path) -> tuple[tuple[str, ...], dict[str, tuple[Binding, ...]]]:
    unknown = set(data) - _SERVICES_KEYS
    if unknown:
        raise InputError(f"services file has unknown keys: {sorted(unknown)}", path=str(path))
    services = _require_str_list(data.get("services"), "services", path)
    overrides = _parse_grouped(data.get("service_overrides"), "service_overrides", path)
    return tuple(services), overrides


def _parse_alias_target(
    raw: Any, entry_env: str, entry_svc: str, context: str, path: Path
) -> AliasTarget:
    if not isinstance(raw, dict):
        raise InputError(f"{context} must be an object", path=str(path))
    unknown = set(raw) - {"environment", "service", "ref"}
    if unknown:
        raise InputError(f"{context} has unknown keys: {sorted(unknown)}", path=str(path))
    nested = raw.get("environment")
    environment = entry_env if nested is None else _require_str(
        nested, f"{context}.environment", path
    )
    nested = raw.get("service")
    service = entry_svc if nested is None else _require_str(
        nested, f"{context}.service", path
    )
    ref = _require_str(raw.get("ref"), f"{context}.ref", path)
    return AliasTarget(environment=environment, service=service, ref=ref)


def _parse_catalog(data: dict, path: Path) -> tuple[str, list[str], dict[tuple[str, str, str], CatalogEntry]]:
    unknown = set(data) - _CATALOG_KEYS
    if unknown:
        raise InputError(f"catalog file has unknown keys: {sorted(unknown)}", path=str(path))
    environment = _require_str(data.get("environment"), "catalog.environment", path)
    services = _require_str_list(data.get("services"), "catalog.services", path)
    raw_refs = data.get("references")
    if not isinstance(raw_refs, list):
        raise InputError("catalog.references must be a list", path=str(path))
    entries: dict[tuple[str, str, str], CatalogEntry] = {}
    allowed_services = set(services)
    for index, item in enumerate(raw_refs):
        where = f"catalog.references[{index}]"
        if not isinstance(item, dict):
            raise InputError(f"{where} must be an object", path=str(path))
        unknown = set(item) - {
            "environment", "service", "ref", "versions", "current", "alias_of"
        }
        if unknown:
            raise InputError(f"{where} has unknown keys: {sorted(unknown)}", path=str(path))
        env_raw = item.get("environment", environment)
        env = _require_str(env_raw, f"{where}.environment", path)
        svc = _require_str(item.get("service"), f"{where}.service", path)
        if env == environment and svc not in allowed_services:
            raise InputError(
                f"{where}.service {svc!r} is not declared in catalog.services", path=str(path)
            )
        ref = _require_str(item.get("ref"), f"{where}.ref", path)
        key = (env, svc, ref)
        if key in entries:
            raise InputError(f"{where} duplicates reference {env}/{svc}/{ref}", path=str(path))
        raw_versions = item.get("versions", [])
        if not isinstance(raw_versions, list):
            raise InputError(f"{where}.versions must be a list", path=str(path))
        versions: list[int] = []
        for pos, value in enumerate(raw_versions):
            parsed = _parse_version(value, f"{where}.versions[{pos}]", path)
            if parsed is None:
                raise InputError(f"{where}.versions[{pos}] must be an integer", path=str(path))
            if parsed in versions:
                raise InputError(f"{where}.versions duplicates version {parsed}", path=str(path))
            versions.append(parsed)
        current = _parse_version(item.get("current"), f"{where}.current", path)
        raw_alias = item.get("alias_of")
        alias = None
        if raw_alias is not None:
            alias = _parse_alias_target(raw_alias, env, svc, f"{where}.alias_of", path)
        if alias is not None and versions:
            raise InputError(f"{where} declares both versions and alias_of", path=str(path))
        if alias is None and not versions:
            raise InputError(f"{where} must declare versions or alias_of", path=str(path))
        if alias is not None and current is not None:
            raise InputError(f"{where} declares alias_of with current", path=str(path))
        if current is not None and current not in versions:
            raise InputError(
                f"{where}.current {current} is not among declared versions", path=str(path)
            )
        entries[key] = CatalogEntry(
            environment=env,
            service=svc,
            ref=ref,
            versions=tuple(versions),
            current=current,
            alias_of=alias,
        )
    return environment, services, entries


def load_inputs(
    environment_path: str | Path,
    services_path: str | Path,
    catalog_path: str | Path,
) -> DeploymentInputs:
    """Load the environment, services and catalog documents and cross-check them."""

    env_path = Path(environment_path)
    svc_path = Path(services_path)
    cat_path = Path(catalog_path)

    env_data = _load_json(env_path, "environment")
    svc_data = _load_json(svc_path, "services")
    cat_data = _load_json(cat_path, "catalog")

    environment, common, env_overrides = _parse_environment(env_data, env_path)
    services, svc_overrides = _parse_services(svc_data, svc_path)
    catalog_env, catalog_services, catalog = _parse_catalog(cat_data, cat_path)

    if catalog_env != environment:
        raise InputError(
            f"catalog environment {catalog_env!r} does not match environment file "
            f"{environment!r}",
            path=str(cat_path),
        )
    missing = set(services) - set(catalog_services)
    if missing:
        raise InputError(
            f"services {sorted(missing)} are not registered in catalog.services",
            path=str(cat_path),
        )
    extra = set(svc_overrides) - set(services)
    if extra:
        raise InputError(
            f"service_overrides targets undeclared services: {sorted(extra)}",
            path=str(svc_path),
        )
    return DeploymentInputs(
        environment=environment,
        services=services,
        common=common,
        environment_overrides=env_overrides,
        service_overrides=svc_overrides,
        catalog=catalog,
    )
