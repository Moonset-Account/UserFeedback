"""Public data model for the module loading planner."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Mapping

from .versions import Version, VersionRange


class PlannerSpecificationError(ValueError):
    """Raised when the caller supplied an invalid planner specification."""


class DepKind(str, Enum):
    LOAD = "load"
    RUNTIME = "runtime"


class Sharing(str, Enum):
    #: Compatible consumers reuse one instance; incompatible requirements may
    #: create side-by-side versioned generations.
    SHARED = "shared"
    #: Every consumer receives its own instance.  ``ISOLATED`` is a readable
    #: alias for this member.
    CALLER = "caller"
    ISOLATED = "caller"
    #: Exactly one instance is allowed in the sharing scope; disagreement is
    #: unsatisfiable rather than solved by coexistence.
    SINGLETON = "singleton"


class OperationKind(str, Enum):
    START = "start"
    BIND = "bind"
    REPLACE = "replace"
    RETAIN = "retain"
    RELEASE = "release"


@dataclass(frozen=True)
class Dependency:
    """A dependency declaration.

    ``defer_groups=None`` means the planner default scope applies.  An empty
    frozenset means the runtime edge may not be deferred for any group.
    """

    name: str
    version_range: VersionRange | str = "*"
    sharing: Sharing | str = Sharing.SHARED
    kind: DepKind | str = DepKind.RUNTIME
    alias: str | None = None
    optional: bool = False
    pin_to_caller_version: bool = False
    deferrable: bool = True
    defer_groups: frozenset[str] | None = None
    share_scope: str = "host"
    origin: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise PlannerSpecificationError("dependency name must be a non-empty string")
        object.__setattr__(self, "version_range", VersionRange.parse(self.version_range))
        object.__setattr__(self, "sharing", Sharing(self.sharing))
        object.__setattr__(self, "kind", DepKind(self.kind))
        if self.alias is None:
            object.__setattr__(self, "alias", self.name)
        if self.defer_groups is not None and not isinstance(self.defer_groups, frozenset):
            object.__setattr__(self, "defer_groups", frozenset(self.defer_groups))
        if not isinstance(self.deferrable, bool):
            raise PlannerSpecificationError("deferrable must be a boolean")
        if self.kind == DepKind.LOAD and self.deferrable is not True:
            # Load-time edges are always eager.  Accepting deferrable=False is
            # harmless, but deferrable=True must not make a load edge lazy.
            pass

    def with_kind(self, kind: DepKind | str) -> "Dependency":
        return replace(self, kind=DepKind(kind))


@dataclass(frozen=True)
class Implementation:
    """An available implementation at an exact version."""

    module: str
    version: Version | str
    load_deps: tuple[Dependency, ...] = ()
    runtime_deps: tuple[Dependency, ...] = ()

    def __post_init__(self) -> None:
        if not self.module:
            raise PlannerSpecificationError("implementation module must be non-empty")
        object.__setattr__(self, "version", Version.parse(self.version))
        load = tuple(_as_dependency(d, DepKind.LOAD) for d in self.load_deps)
        runtime = tuple(_as_dependency(d, DepKind.RUNTIME) for d in self.runtime_deps)
        _validate_aliases(self.module, load + runtime)
        object.__setattr__(self, "load_deps", load)
        object.__setattr__(self, "runtime_deps", runtime)


@dataclass(frozen=True)
class PluginRequest:
    """A root plugin to load in one of the two plugin groups."""

    key: str
    module: str = ""
    group: str = "default"
    version_range: VersionRange | str = "*"
    load_deps: tuple[Dependency, ...] | None = None
    runtime_deps: tuple[Dependency, ...] | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise PlannerSpecificationError("plugin key must be non-empty")
        if not self.module:
            object.__setattr__(self, "module", self.key)
        object.__setattr__(self, "version_range", VersionRange.parse(self.version_range))
        if self.load_deps is not None:
            load = tuple(_as_dependency(d, DepKind.LOAD) for d in self.load_deps)
            _validate_aliases(f"{self.key}:load", load)
            object.__setattr__(self, "load_deps", load)
        if self.runtime_deps is not None:
            runtime = tuple(_as_dependency(d, DepKind.RUNTIME) for d in self.runtime_deps)
            _validate_aliases(f"{self.key}:runtime", runtime)
            object.__setattr__(self, "runtime_deps", runtime)
        if self.load_deps is not None and self.runtime_deps is not None:
            _validate_aliases(f"{self.key}:all", self.load_deps + self.runtime_deps)


@dataclass(frozen=True)
class ObjectRef:
    """A strong object handle that must remain valid during hot unload."""

    ref_id: str
    instance_id: str
    holder: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.ref_id:
            raise PlannerSpecificationError("reference id must be non-empty")
        if not self.instance_id:
            raise PlannerSpecificationError("reference instance_id must be non-empty")


@dataclass(frozen=True)
class PlannedInstance:
    id: str
    module: str
    version: Version
    share_key: str
    group: str
    request_key: str | None = None
    groups: frozenset[str] = frozenset()
    sharing: Sharing = Sharing.SHARED
    active: bool = True
    retained: bool = False
    held_by_refs: tuple[str, ...] = ()
    provider_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "module": self.module,
            "version": str(self.version),
            "share_key": self.share_key,
            "group": self.group,
            "groups": sorted(self.groups),
            "request_key": self.request_key,
            "sharing": self.sharing.value,
            "active": self.active,
            "retained": self.retained,
            "held_by_refs": list(self.held_by_refs),
            "provider_keys": list(self.provider_keys),
        }


@dataclass(frozen=True)
class Binding:
    id: str
    caller_id: str
    dependency_alias: str
    dependency_id: str
    kind: DepKind
    deferred: bool = False
    origin: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "caller_id": self.caller_id,
            "dependency_alias": self.dependency_alias,
            "dependency_id": self.dependency_id,
            "kind": self.kind.value,
            "deferred": self.deferred,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class Operation:
    kind: OperationKind
    order: int
    instance_id: str | None = None
    target_id: str | None = None
    binding_id: str | None = None
    replaces_id: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "order": self.order,
            "instance_id": self.instance_id,
            "target_id": self.target_id,
            "binding_id": self.binding_id,
            "replaces_id": self.replaces_id,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LoadPlan:
    """A resolved instance set and an ordered lifecycle transition."""

    feasible: bool
    groups: tuple[str, ...]
    requests: tuple[PluginRequest, ...]
    instances: tuple[PlannedInstance, ...] = ()
    bindings: tuple[Binding, ...] = ()
    operations: tuple[Operation, ...] = ()
    start_order: tuple[str, ...] = ()
    preserved_ids: tuple[str, ...] = ()
    started_ids: tuple[str, ...] = ()
    released_ids: tuple[str, ...] = ()
    runtime_cycles: tuple[tuple[str, ...], ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    retained_refs: tuple[ObjectRef, ...] = ()

    def instance_ids(self) -> set[str]:
        return {instance.id for instance in self.instances}

    def instance(self, instance_id: str) -> PlannedInstance:
        for item in self.instances:
            if item.id == instance_id:
                return item
        raise KeyError(instance_id)

    def bindings_for(self, caller_id: str) -> tuple[Binding, ...]:
        return tuple(binding for binding in self.bindings if binding.caller_id == caller_id)

    def operations_of(self, kind: OperationKind | str) -> tuple[Operation, ...]:
        wanted = OperationKind(kind)
        return tuple(operation for operation in self.operations if operation.kind == wanted)

    def operation_index(
        self,
        kind: OperationKind | str,
        instance_id: str,
        binding_id: str | None = None,
    ) -> int:
        wanted = OperationKind(kind)
        for operation in self.operations:
            if operation.kind != wanted or operation.instance_id != instance_id:
                continue
            if binding_id is not None and operation.binding_id != binding_id:
                continue
            return operation.order
        raise KeyError((wanted, instance_id, binding_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "groups": list(self.groups),
            "instances": [item.to_dict() for item in self.instances],
            "bindings": [item.to_dict() for item in self.bindings],
            "operations": [item.to_dict() for item in self.operations],
            "start_order": list(self.start_order),
            "preserved_ids": list(self.preserved_ids),
            "started_ids": list(self.started_ids),
            "released_ids": list(self.released_ids),
            "runtime_cycles": [list(cycle) for cycle in self.runtime_cycles],
            "conflicts": [item.to_dict() for item in self.conflicts],
            "retained_refs": [
                {"ref_id": ref.ref_id, "instance_id": ref.instance_id, "holder": ref.holder}
                for ref in self.retained_refs
            ],
        }


@dataclass(frozen=True)
class Conflict:
    code: str
    message: str
    modules: tuple[str, ...] = ()
    requesters: tuple[str, ...] = ()
    ranges: Mapping[str, str] = field(default_factory=dict)
    candidates: tuple[str, ...] = ()
    cycle: tuple[str, ...] = ()
    references: tuple[str, ...] = ()

    def format(self) -> str:
        lines = [f"[{self.code}] {self.message}"]
        if self.modules:
            lines.append(f"  modules: {', '.join(self.modules)}")
        if self.requesters:
            lines.append(f"  requesters: {', '.join(self.requesters)}")
        if self.ranges:
            for requester, interval in self.ranges.items():
                lines.append(f"    {requester}: {interval}")
        if self.candidates:
            lines.append(f"  candidates: {', '.join(self.candidates)}")
        if self.cycle:
            lines.append(f"  cycle: {' -> '.join(self.cycle)}")
        if self.references:
            lines.append(f"  references: {', '.join(self.references)}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "modules": list(self.modules),
            "requesters": list(self.requesters),
            "ranges": dict(self.ranges),
            "candidates": list(self.candidates),
            "cycle": list(self.cycle),
            "references": list(self.references),
        }


class UnsatisfiablePlanError(ValueError):
    """Raised when no loading instance set can satisfy the constraints."""

    def __init__(self, conflicts: Iterable[Conflict]):
        self.conflicts: tuple[Conflict, ...] = tuple(conflicts)
        if not self.conflicts:
            self.conflicts = (Conflict(code="UNSATISFIABLE", message="plan is unsatisfiable"),)
        super().__init__("\n".join(conflict.format() for conflict in self.conflicts))


def _as_dependency(value: Dependency | Mapping[str, Any], kind: DepKind) -> Dependency:
    if isinstance(value, Dependency):
        if value.kind != kind:
            return value.with_kind(kind)
        return value
    if isinstance(value, Mapping):
        kwargs = dict(value)
        kwargs["kind"] = kind
        return Dependency(**kwargs)
    raise PlannerSpecificationError(f"invalid dependency specification: {value!r}")


def _validate_aliases(owner: str, deps: tuple[Dependency, ...]) -> None:
    seen: set[tuple[DepKind, str]] = set()
    for dep in deps:
        key = (dep.kind, dep.alias or dep.name)
        if key in seen:
            raise PlannerSpecificationError(
                f"duplicate {dep.kind.value} dependency alias {key[1]!r} for {owner}"
            )
        seen.add(key)
