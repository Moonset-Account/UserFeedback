"""Dependency resolution, lifecycle ordering, and hot-reload planning."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any, Iterable, Mapping

from .graph import find_cycles, topological_sort
from .model import (
    Binding,
    Conflict,
    Dependency,
    DepKind,
    Implementation,
    LoadPlan,
    ObjectRef,
    Operation,
    OperationKind,
    PlannedInstance,
    PluginRequest,
    Sharing,
    UnsatisfiablePlanError,
)
from .versions import Version, VersionRange


class ModuleLoadPlanner:
    """Stateful convenience wrapper around a fixed implementation catalog."""

    def __init__(self, implementations: Iterable[Implementation | Mapping[str, Any]] = ()):
        self.implementations = tuple(_as_implementation(item) for item in implementations)

    def plan(
        self,
        plugins: Mapping[str, Iterable[PluginRequest]] | Iterable[PluginRequest],
        *,
        retained_refs: Iterable[ObjectRef] = (),
        default_defer_groups: Iterable[str] | None = None,
        strict: bool = True,
    ) -> LoadPlan:
        return plan_load(
            plugins,
            self.implementations,
            retained_refs=retained_refs,
            default_defer_groups=default_defer_groups,
            strict=strict,
        )

    def replan(
        self,
        previous: LoadPlan,
        plugins: Mapping[str, Iterable[PluginRequest]] | Iterable[PluginRequest] | None = None,
        *,
        extra_implementations: Iterable[Implementation | Mapping[str, Any]] = (),
        changed_groups: Iterable[str] | None = None,
        preserve_groups: Iterable[str] | None = None,
        retained_refs: Iterable[ObjectRef] = (),
        default_defer_groups: Iterable[str] | None = None,
        strict: bool = True,
    ) -> LoadPlan:
        implementations = self.implementations + tuple(
            _as_implementation(item) for item in extra_implementations
        )
        return plan_load(
            plugins if plugins is not None else previous.requests,
            implementations,
            retained_refs=retained_refs,
            previous=previous,
            changed_groups=changed_groups,
            preserve_groups=preserve_groups,
            default_defer_groups=default_defer_groups,
            strict=strict,
        )

    def change_group(
        self,
        previous: LoadPlan,
        group: str,
        plugins: Iterable[PluginRequest],
        *,
        extra_implementations: Iterable[Implementation | Mapping[str, Any]] = (),
        retained_refs: Iterable[ObjectRef] = (),
        default_defer_groups: Iterable[str] | None = None,
        strict: bool = True,
    ) -> LoadPlan:
        grouped: dict[str, list[PluginRequest]] = defaultdict(list)
        for request in previous.requests:
            if request.group != group:
                grouped[request.group].append(request)
        grouped[group] = [_with_group(item, group) for item in plugins]
        return self.replan(
            previous,
            grouped,
            extra_implementations=extra_implementations,
            changed_groups={group},
            retained_refs=retained_refs,
            default_defer_groups=default_defer_groups,
            strict=strict,
        )


def plan_load(
    plugins: Mapping[str, Iterable[PluginRequest]] | Iterable[PluginRequest] | None = None,
    implementations: Iterable[Implementation | Mapping[str, Any]] = (),
    *,
    retained_refs: Iterable[ObjectRef | Mapping[str, Any]] = (),
    previous: LoadPlan | None = None,
    changed_groups: Iterable[str] | None = None,
    preserve_groups: Iterable[str] | None = None,
    default_defer_groups: Iterable[str] | None = None,
    strict: bool = True,
) -> LoadPlan:
    """Resolve and order a load plan.

    With ``strict=True`` (the default), unsatisfiable specifications raise
    :class:`UnsatisfiablePlanError`.  With ``strict=False`` a partial
    :class:`LoadPlan` with ``feasible=False`` and structured conflicts is
    returned instead.
    """

    impls = tuple(_as_implementation(item) for item in implementations)
    refs = tuple(_as_ref(item) for item in retained_refs)
    requests, groups = _normalize_plugins(plugins, previous)
    resolver = _Resolver(
        requests=requests,
        groups=groups,
        implementations=impls,
        refs=refs,
        previous=previous,
        changed_groups=frozenset(changed_groups) if changed_groups is not None else None,
        preserve_groups=frozenset(preserve_groups) if preserve_groups is not None else None,
        default_defer_groups=(
            frozenset(default_defer_groups) if default_defer_groups is not None else None
        ),
    )
    plan = resolver.run()
    if not plan.feasible and strict:
        raise UnsatisfiablePlanError(plan.conflicts)
    return plan


def try_plan(*args: Any, **kwargs: Any) -> LoadPlan:
    """Convenience wrapper that never raises for modeled unsatisfiability."""

    kwargs["strict"] = False
    return plan_load(*args, **kwargs)


def replan(
    previous: LoadPlan,
    plugins: Mapping[str, Iterable[PluginRequest]] | Iterable[PluginRequest] | None = None,
    implementations: Iterable[Implementation | Mapping[str, Any]] = (),
    **kwargs: Any,
) -> LoadPlan:
    """Functional wrapper around :meth:`ModuleLoadPlanner.replan`."""

    return plan_load(
        plugins if plugins is not None else previous.requests,
        implementations,
        previous=previous,
        **kwargs,
    )


class _Resolver:
    def __init__(
        self,
        *,
        requests: tuple[PluginRequest, ...],
        groups: tuple[str, ...],
        implementations: tuple[Implementation, ...],
        refs: tuple[ObjectRef, ...],
        previous: LoadPlan | None,
        changed_groups: frozenset[str] | None,
        preserve_groups: frozenset[str] | None,
        default_defer_groups: frozenset[str] | None,
    ):
        self.requests = requests
        self.groups = groups
        self.refs = refs
        self.previous = previous
        self.default_defer_groups = default_defer_groups

        self.catalog: dict[str, dict[Version, Implementation]] = defaultdict(dict)
        for impl in implementations:
            module_versions = self.catalog[impl.module]
            existing = module_versions.get(impl.version)
            if existing is not None and existing != impl:
                from .model import PlannerSpecificationError

                raise PlannerSpecificationError(
                    f"conflicting implementation metadata for {impl.module}@{impl.version}"
                )
            module_versions[impl.version] = impl

        self.old_by_id: dict[str, PlannedInstance] = {}
        self.old_bindings_by_caller: dict[str, list[Binding]] = defaultdict(list)
        self.old_binding_by_id: dict[str, Binding] = {}
        self.old_share_index: dict[str, list[str]] = defaultdict(list)
        self.old_root_by_key: dict[str, str] = {}
        self.old_groups: tuple[str, ...] = ()
        if previous is not None:
            self.old_by_id = {item.id: item for item in previous.instances}
            self.old_groups = previous.groups
            for binding in previous.bindings:
                self.old_bindings_by_caller[binding.caller_id].append(binding)
                self.old_binding_by_id[binding.id] = binding
            for instance in previous.instances:
                self.old_share_index[instance.share_key].append(instance.id)
                for provider_key in instance.provider_keys:
                    self.old_share_index[provider_key].append(instance.id)
                if instance.request_key:
                    self.old_root_by_key[instance.request_key] = instance.id

        if preserve_groups is None and changed_groups is not None and previous is not None:
            preserve_groups = frozenset(g for g in previous.groups if g not in changed_groups)
        self.preserve_groups = preserve_groups or frozenset()

        self.instances: dict[str, PlannedInstance] = {}
        self.bindings: dict[str, Binding] = {}
        self.share_index: dict[str, list[str]] = defaultdict(list)
        self.deps_processed: set[str] = set()
        self.demands: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
        self.conflicts: list[Conflict] = []
        self.root_ids: dict[str, str] = {}

    def run(self) -> LoadPlan:
        self._validate_refs()
        self._seed_preserved_groups()

        # Select every root first.  This makes forward references between the
        # two plugin groups bind to the explicitly loaded plugin instance.
        for request in self.requests:
            instance = self._resolve_root(request)
            if instance is not None:
                self.root_ids[request.key] = instance.id

        for request in self.requests:
            root_id = self.root_ids.get(request.key)
            if root_id is not None:
                self._process_request_deps(request, self.instances[root_id])

        self._prune_unreachable()
        self._apply_retained_refs()
        self._compute_instance_groups()

        active_instances = {
            item.id: item for item in self.instances.values() if item.active
        }
        ordered_instances = tuple(
            self.instances[iid] for iid in sorted(self.instances, key=str)
        )
        ordered_bindings = tuple(
            self.bindings[bid] for bid in sorted(self.bindings, key=str)
        )

        active_ids = set(active_instances)
        edges: list[tuple[str, str]] = []
        eager_edges: list[tuple[str, str]] = []
        edge_bindings: dict[tuple[str, str], list[Binding]] = defaultdict(list)
        for binding in ordered_bindings:
            if binding.caller_id not in active_ids or binding.dependency_id not in active_ids:
                continue
            edge = (binding.dependency_id, binding.caller_id)
            edges.append(edge)
            edge_bindings[edge].append(binding)
            if not binding.deferred:
                eager_edges.append(edge)

        start_order, eager_cycles = topological_sort(active_ids, eager_edges)
        for cycle in eager_cycles:
            self.conflicts.append(self._cycle_conflict(cycle, edge_bindings))

        runtime_cycles: list[tuple[str, ...]] = []
        for cycle in find_cycles(active_ids, edges):
            cycle_edges = list(zip(cycle, cycle[1:]))
            if any(
                any(binding.kind == DepKind.RUNTIME for binding in edge_bindings.get(edge, ()))
                for edge in cycle_edges
            ):
                runtime_cycles.append(cycle)

        feasible = not self.conflicts
        operations: tuple[Operation, ...] = ()
        started_ids: tuple[str, ...] = ()
        released_ids: tuple[str, ...] = ()
        if feasible:
            operations_list = self._build_operations(start_order, ordered_bindings)
            operations = tuple(operations_list)
            started_ids = tuple(
                op.instance_id
                for op in operations
                if op.kind == OperationKind.START and op.instance_id is not None
            )
            released_ids = tuple(
                op.instance_id
                for op in operations
                if op.kind == OperationKind.RELEASE and op.instance_id is not None
            )

        preserved_ids: tuple[str, ...] = ()
        if self.previous is not None:
            preserved_ids = tuple(
                sorted(iid for iid in self.instances if iid in self.old_by_id)
            )

        return LoadPlan(
            feasible=feasible,
            groups=self.groups or self.old_groups,
            requests=tuple(self.requests),
            instances=ordered_instances,
            bindings=ordered_bindings,
            operations=operations,
            start_order=tuple(start_order),
            preserved_ids=preserved_ids,
            started_ids=started_ids,
            released_ids=released_ids,
            runtime_cycles=tuple(runtime_cycles),
            conflicts=tuple(self.conflicts),
            retained_refs=self.refs,
        )

    # ------------------------------------------------------------------
    # Seeding and resolution
    # ------------------------------------------------------------------

    def _validate_refs(self) -> None:
        for ref in self.refs:
            if self.previous is None or ref.instance_id not in self.old_by_id:
                self.conflicts.append(
                    Conflict(
                        code="UNKNOWN_RETAINED_INSTANCE",
                        message=(
                            f"retained reference {ref.ref_id!r} points to unknown "
                            f"instance {ref.instance_id!r}"
                        ),
                        references=(ref.ref_id,),
                    )
                )

    def _seed_preserved_groups(self) -> None:
        if self.previous is None or not self.preserve_groups:
            return
        root_ids = [
            item.id
            for item in self.old_by_id.values()
            if item.request_key is not None
            and item.active
            and item.group in self.preserve_groups
        ]
        for instance_id in root_ids:
            self._adopt_tree(instance_id)

    def _adopt_tree(self, instance_id: str) -> PlannedInstance | None:
        if instance_id in self.instances:
            return self.instances[instance_id]
        old_instance = self.old_by_id.get(instance_id)
        if old_instance is None:
            self.conflicts.append(
                Conflict(
                    code="MISSING_OLD_INSTANCE",
                    message=f"previous plan does not contain instance {instance_id!r}",
                )
            )
            return None

        instance = replace(old_instance, active=True)
        self.instances[instance_id] = instance
        self._index_instance(instance)
        self.deps_processed.add(instance_id)

        for binding in self.old_bindings_by_caller.get(instance_id, ()):
            target = self._adopt_tree(binding.dependency_id)
            if target is not None and binding.id not in self.bindings:
                self.bindings[binding.id] = binding
        return instance

    def _resolve_root(self, request: PluginRequest) -> PlannedInstance | None:
        share_key = f"root:{request.key}"

        for instance_id in self.share_index.get(share_key, ()):
            instance = self.instances[instance_id]
            if request.version_range.contains(instance.version):
                return instance

        old_candidate = self._old_candidate(share_key, request.version_range)
        if old_candidate is not None:
            return self._adopt_tree(old_candidate)

        version = request.version_range.best(self.catalog.get(request.module, {}).keys())
        if version is None:
            self.conflicts.append(
                Conflict(
                    code="NO_MATCHING_IMPLEMENTATION",
                    message=(
                        f"plugin {request.key!r} requires {request.module} "
                        f"{request.version_range}, but no matching implementation exists"
                    ),
                    modules=(request.module,),
                    requesters=(request.key,),
                    ranges={request.key: str(request.version_range)},
                    candidates=tuple(
                        sorted(str(v) for v in self.catalog.get(request.module, {}))
                    ),
                )
            )
            return None

        instance_id = f"inst:{share_key}@{version}"
        provider_keys = _provider_keys(request.module, request.group)
        instance = PlannedInstance(
            id=instance_id,
            module=request.module,
            version=version,
            share_key=share_key,
            group=request.group,
            request_key=request.key,
            groups=frozenset({request.group}),
            sharing=Sharing.CALLER,
            provider_keys=provider_keys,
        )
        self._add_instance(instance)
        return instance

    def _process_request_deps(
        self, request: PluginRequest, caller: PlannedInstance
    ) -> None:
        # Mark before resolution: a dependency cycle may re-enter this caller,
        # and request-level declarations own the edge set over catalog metadata.
        self.deps_processed.add(caller.id)
        implementation = self.catalog.get(caller.module, {}).get(caller.version)

        if request.load_deps is not None:
            load_deps: tuple[Dependency, ...] | None = request.load_deps
        elif implementation is not None:
            load_deps = implementation.load_deps
        else:
            # An adopted old instance can survive even when its exact old
            # implementation metadata is absent from the new catalog.  Its
            # copied old bindings remain authoritative in that case.
            load_deps = None

        if request.runtime_deps is not None:
            runtime_deps: tuple[Dependency, ...] | None = request.runtime_deps
        elif implementation is not None:
            runtime_deps = implementation.runtime_deps
        else:
            runtime_deps = None

        if load_deps is not None:
            self._reconcile_kind(caller, load_deps, DepKind.LOAD)
        if runtime_deps is not None:
            self._reconcile_kind(caller, runtime_deps, DepKind.RUNTIME)

    def _ensure_implementation_deps(self, caller: PlannedInstance) -> None:
        if caller.id in self.deps_processed:
            return
        self.deps_processed.add(caller.id)
        implementation = self.catalog.get(caller.module, {}).get(caller.version)
        if implementation is None:
            # Old/adopted instances have their old bindings copied.  A newly
            # selected instance always exists in the catalog, so this branch is
            # only defensive.
            return
        self._reconcile_kind(caller, implementation.load_deps, DepKind.LOAD)
        self._reconcile_kind(caller, implementation.runtime_deps, DepKind.RUNTIME)

    def _reconcile_kind(
        self,
        caller: PlannedInstance,
        dependencies: tuple[Dependency, ...],
        kind: DepKind,
    ) -> None:
        desired_ids = {
            f"bind:{caller.id}:{kind.value}:{dep.alias or dep.name}"
            for dep in dependencies
        }
        for binding_id in list(self.bindings):
            binding = self.bindings[binding_id]
            if (
                binding.caller_id == caller.id
                and binding.kind == kind
                and binding_id not in desired_ids
            ):
                del self.bindings[binding_id]

        for raw_dep in dependencies:
            dep = raw_dep.with_kind(kind)
            target = self._resolve_dependency(caller, dep)
            if target is not None:
                self._ensure_implementation_deps(target)

    def _resolve_dependency(
        self, caller: PlannedInstance, dep: Dependency
    ) -> PlannedInstance | None:
        alias = dep.alias or dep.name
        binding_id = f"bind:{caller.id}:{dep.kind.value}:{alias}"
        requester = caller.request_key or caller.id
        self.demands[self._demand_key(dep, caller)].append(
            (requester, str(dep.version_range), dep.origin)
        )

        wanted_version: Version | None = None
        if dep.pin_to_caller_version:
            if not dep.version_range.contains(caller.version):
                if not dep.optional:
                    self.conflicts.append(
                        Conflict(
                            code="CALLER_PIN_OUTSIDE_RANGE",
                            message=(
                                f"{requester} needs {dep.name} pinned to caller version "
                                f"{caller.version}, which is outside {dep.version_range}"
                            ),
                            modules=(dep.name,),
                            requesters=(requester,),
                            ranges={requester: str(dep.version_range)},
                            references=(dep.origin,) if dep.origin else (),
                        )
                    )
                return None
            wanted_version = caller.version

        share_key = self._share_key(dep, caller, alias)

        existing_binding = self.bindings.get(binding_id)
        if existing_binding is not None:
            existing_target = self.instances.get(existing_binding.dependency_id)
            if (
                existing_target is not None
                and existing_target.share_key == share_key
                and self._version_acceptable(
                    existing_target.version, dep.version_range, wanted_version
                )
            ):
                deferred = self._is_deferred(dep, caller.group)
                if existing_binding.deferred != deferred:
                    self.bindings[binding_id] = replace(
                        existing_binding, deferred=deferred
                    )
                return existing_target
            del self.bindings[binding_id]

        strict_conflict = self._strict_conflict_if_any(
            dep, share_key, requester, wanted_version
        )
        if strict_conflict is not None:
            if not dep.optional:
                self.conflicts.append(strict_conflict)
            return None

        candidate_id = self._active_candidate(share_key, dep.version_range, wanted_version)
        if candidate_id is None:
            old_candidate = self._old_candidate(
                share_key, dep.version_range, wanted_version
            )
            if old_candidate is not None:
                candidate_id = self._adopt_tree(old_candidate).id

        version: Version | None = None
        if candidate_id is not None:
            candidate = self.instances[candidate_id]
            version = candidate.version
        else:
            versions = self.catalog.get(dep.name, {})
            if wanted_version is not None:
                version = wanted_version if wanted_version in versions else None
            else:
                version = dep.version_range.best(versions.keys())

        if version is None:
            if dep.optional:
                return None
            code = (
                "NO_CALLER_PINNED_IMPLEMENTATION"
                if dep.pin_to_caller_version
                else "NO_MATCHING_IMPLEMENTATION"
            )
            self.conflicts.append(
                Conflict(
                    code=code,
                    message=(
                        f"{requester} requires {dep.name} {dep.version_range}"
                        + (f" pinned to {wanted_version}" if wanted_version else "")
                        + ", but no matching implementation is available"
                    ),
                    modules=(dep.name,),
                    requesters=tuple(item[0] for item in self.demands.get(share_key, ()))
                    or (requester,),
                    ranges={
                        item[0]: item[1] for item in self.demands.get(share_key, ())
                    }
                    or {requester: str(dep.version_range)},
                    candidates=tuple(sorted(str(v) for v in self.catalog.get(dep.name, {}))),
                    references=tuple(
                        sorted(
                            {
                                origin
                                for _, _, origin in self.demands.get(share_key, ())
                                if origin
                            }
                        )
                    ),
                )
            )
            return None

        if candidate_id is None:
            candidate_id = f"inst:{share_key}@{version}"
            if candidate_id not in self.instances:
                instance = PlannedInstance(
                    id=candidate_id,
                    module=dep.name,
                    version=version,
                    share_key=share_key,
                    group=caller.group,
                    groups=frozenset({caller.group}),
                    sharing=dep.sharing,
                )
                self._add_instance(instance)

        deferred = self._is_deferred(dep, caller.group)
        self.bindings[binding_id] = Binding(
            id=binding_id,
            caller_id=caller.id,
            dependency_alias=alias,
            dependency_id=candidate_id,
            kind=dep.kind,
            deferred=deferred,
            origin=dep.origin,
        )
        return self.instances[candidate_id]

    def _add_instance(self, instance: PlannedInstance) -> None:
        self.instances[instance.id] = instance
        self._index_instance(instance)

    def _index_instance(self, instance: PlannedInstance) -> None:
        keys = (instance.share_key, *instance.provider_keys)
        for key in keys:
            if instance.id not in self.share_index[key]:
                self.share_index[key].append(instance.id)

    def _demand_key(self, dep: Dependency, caller: PlannedInstance) -> str:
        return self._share_key(dep, caller, dep.alias or dep.name)

    @staticmethod
    def _share_key(
        dep: Dependency, caller: PlannedInstance, alias: str
    ) -> str:
        if dep.sharing == Sharing.CALLER:
            return f"caller:{caller.id}:{alias}"
        if dep.share_scope == "group":
            scope = f"group:{caller.group}"
        else:
            scope = dep.share_scope
        return f"{dep.sharing.value}:{scope}:{dep.name}"

    @staticmethod
    def _version_acceptable(
        version: Version,
        interval: VersionRange,
        wanted: Version | None,
    ) -> bool:
        if wanted is not None:
            return version == wanted and interval.contains(version)
        return interval.contains(version)

    def _active_candidate(
        self,
        share_key: str,
        interval: VersionRange,
        wanted: Version | None,
    ) -> str | None:
        candidates: list[str] = []
        for instance_id in self.share_index.get(share_key, ()):
            instance = self.instances.get(instance_id)
            if instance is None:
                continue
            if self._version_acceptable(instance.version, interval, wanted):
                candidates.append(instance_id)
        if not candidates:
            return None
        return self._prefer_old_then_version(candidates)

    def _old_candidate(
        self,
        share_key: str,
        interval: VersionRange,
        wanted: Version | None = None,
    ) -> str | None:
        candidates = [
            instance_id
            for instance_id in self.old_share_index.get(share_key, ())
            if instance_id in self.old_by_id
            and self._version_acceptable(
                self.old_by_id[instance_id].version, interval, wanted
            )
        ]
        if not candidates:
            return None
        return self._prefer_old_then_version(candidates)

    def _prefer_old_then_version(self, candidate_ids: list[str]) -> str:
        def preference(instance_id: str) -> tuple[int, int, "_VersionSortKey", str]:
            instance = self.instances.get(instance_id) or self.old_by_id[instance_id]
            is_old = 0 if self.previous is not None and instance_id in self.old_by_id else 1
            is_root_provider = 0 if instance.request_key is not None else 1
            return (
                is_old,
                is_root_provider,
                _VersionSortKey(instance.version),
                instance_id,
            )

        return min(candidate_ids, key=preference)

    def _strict_conflict_if_any(
        self,
        dep: Dependency,
        share_key: str,
        requester: str,
        wanted: Version | None,
    ) -> Conflict | None:
        if dep.sharing != Sharing.SINGLETON:
            return None
        existing_ids = list(self.share_index.get(share_key, ()))
        if not existing_ids and share_key in self.old_share_index:
            existing_ids.extend(self.old_share_index[share_key])
        incompatible: list[PlannedInstance] = []
        for instance_id in existing_ids:
            instance = self.instances.get(instance_id) or self.old_by_id.get(instance_id)
            if instance is None:
                continue
            if not self._version_acceptable(instance.version, dep.version_range, wanted):
                incompatible.append(instance)
        if not incompatible:
            return None

        demands = self.demands.get(share_key, [])
        ranges = {item[0]: item[1] for item in demands}
        ranges.setdefault(requester, str(dep.version_range))
        references = tuple(sorted({origin for _, _, origin in demands if origin}))
        versions = tuple(sorted({str(item.version) for item in incompatible}))
        return Conflict(
            code="SINGLETON_VERSION_CONFLICT",
            message=(
                f"strict shared singleton {dep.name} cannot satisfy all callers; "
                f"existing version(s): {', '.join(versions)}"
            ),
            modules=(dep.name,),
            requesters=tuple(ranges.keys()),
            ranges=ranges,
            candidates=tuple(sorted(str(v) for v in self.catalog.get(dep.name, {}))),
            references=references,
        )

    def _is_deferred(self, dep: Dependency, caller_group: str) -> bool:
        if dep.kind != DepKind.RUNTIME or not dep.deferrable:
            return False
        allowed = dep.defer_groups
        if allowed is None:
            allowed = self.default_defer_groups
        if allowed is None:
            return True
        return "*" in allowed or caller_group in allowed

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def _prune_unreachable(self) -> None:
        reachable: set[str] = set()
        stack = [instance_id for instance_id in self.root_ids.values() if instance_id]
        while stack:
            instance_id = stack.pop()
            if instance_id in reachable or instance_id not in self.instances:
                continue
            reachable.add(instance_id)
            for binding in self.bindings.values():
                if binding.caller_id == instance_id:
                    stack.append(binding.dependency_id)

        self.instances = {
            iid: instance for iid, instance in self.instances.items() if iid in reachable
        }
        self.bindings = {
            bid: binding
            for bid, binding in self.bindings.items()
            if binding.caller_id in reachable
            and binding.dependency_id in reachable
        }

    def _apply_retained_refs(self) -> None:
        if self.previous is None:
            return

        explicit: dict[str, list[str]] = defaultdict(list)
        for ref in self.refs:
            if ref.instance_id in self.old_by_id:
                explicit[ref.instance_id].append(ref.ref_id)

        closure: set[str] = set()
        stack = list(explicit.keys())
        while stack:
            instance_id = stack.pop()
            if instance_id in closure:
                continue
            closure.add(instance_id)
            for binding in self.old_bindings_by_caller.get(instance_id, ()):
                stack.append(binding.dependency_id)

        for instance_id in closure:
            old_instance = self.old_by_id[instance_id]
            refs = tuple(explicit.get(instance_id, ()))
            if instance_id in self.instances:
                current = self.instances[instance_id]
                self.instances[instance_id] = replace(
                    current,
                    retained=True,
                    held_by_refs=tuple(sorted(set(current.held_by_refs + refs))),
                )
            else:
                self.instances[instance_id] = replace(
                    old_instance,
                    active=False,
                    retained=True,
                    held_by_refs=refs,
                )
                # Preserve the old retained object's dependency graph for
                # diagnostics and release-order reasoning.
                for binding in self.old_bindings_by_caller.get(instance_id, ()):
                    if binding.id not in self.bindings:
                        self.bindings[binding.id] = binding

    def _compute_instance_groups(self) -> None:
        groups_for_instance: dict[str, set[str]] = defaultdict(set)
        bindings_by_caller: dict[str, list[Binding]] = defaultdict(list)
        for binding in self.bindings.values():
            bindings_by_caller[binding.caller_id].append(binding)

        for request in self.requests:
            root_id = self.root_ids.get(request.key)
            if not root_id:
                continue
            seen: set[str] = set()
            stack = [root_id]
            while stack:
                instance_id = stack.pop()
                if instance_id in seen or instance_id not in self.instances:
                    continue
                seen.add(instance_id)
                groups_for_instance[instance_id].add(request.group)
                for binding in bindings_by_caller.get(instance_id, ()):
                    stack.append(binding.dependency_id)

        for instance_id, instance in list(self.instances.items()):
            computed = groups_for_instance.get(instance_id)
            if computed:
                self.instances[instance_id] = replace(
                    instance, groups=frozenset(computed)
                )
            elif not instance.groups:
                self.instances[instance_id] = replace(
                    instance, groups=frozenset({instance.group})
                )

    def _cycle_conflict(
        self,
        cycle: tuple[Any, ...],
        edge_bindings: dict[tuple[str, str], list[Binding]],
    ) -> Conflict:
        cycle_ids = tuple(str(item) for item in cycle)
        all_bindings = [
            binding
            for source, target in zip(cycle_ids, cycle_ids[1:])
            for binding in edge_bindings.get((str(source), str(target)), ())
        ]
        eager_bindings = [binding for binding in all_bindings if not binding.deferred]
        classification_bindings = eager_bindings or all_bindings
        all_load = bool(classification_bindings) and all(
            b.kind == DepKind.LOAD for b in classification_bindings
        )
        code = "LOAD_CYCLE" if all_load else "STARTUP_CYCLE"
        label = "load-time dependency cycle" if all_load else "startup dependency cycle"
        references = tuple(
            sorted({b.origin for b in classification_bindings if b.origin})
        )
        requesters: list[str] = []
        for binding in classification_bindings:
            instance = self.instances.get(binding.caller_id)
            requesters.append(
                instance.request_key
                if instance and instance.request_key
                else binding.caller_id
            )
        return Conflict(
            code=code,
            message=f"unsatisfiable {label}: {' -> '.join(cycle_ids)}",
            modules=tuple(
                sorted(
                    {
                        self.instances[bid].module
                        for bid in cycle_ids
                        if bid in self.instances
                    }
                )
            ),
            requesters=tuple(requesters),
            cycle=cycle_ids,
            references=references,
        )

    def _build_operations(
        self,
        start_order: list[Any],
        bindings: tuple[Binding, ...],
    ) -> tuple[Operation, ...]:
        operations: list[Operation] = []
        old_binding_ids = self.old_binding_by_id
        old_by_id = self.old_by_id

        bindings_by_caller: dict[str, list[Binding]] = defaultdict(list)
        deferred: list[Binding] = []
        for binding in sorted(bindings, key=lambda item: item.id):
            if binding.deferred:
                deferred.append(binding)
            else:
                bindings_by_caller[binding.caller_id].append(binding)

        def binding_changed(binding: Binding) -> bool:
            old = old_binding_ids.get(binding.id)
            return (
                old is None
                or old.dependency_id != binding.dependency_id
                or old.deferred != binding.deferred
            )

        for raw_instance_id in start_order:
            instance_id = str(raw_instance_id)
            for binding in bindings_by_caller.get(instance_id, ()):
                if self.previous is None or binding_changed(binding):
                    operations.append(
                        Operation(
                            kind=OperationKind.BIND,
                            order=0,
                            instance_id=binding.caller_id,
                            target_id=binding.dependency_id,
                            binding_id=binding.id,
                            reason="load/eager binding",
                        )
                    )
            if self.previous is None or instance_id not in old_by_id:
                operations.append(
                    Operation(
                        kind=OperationKind.START,
                        order=0,
                        instance_id=instance_id,
                        reason="new instance",
                    )
                )

        for binding in deferred:
            if self.previous is None or binding_changed(binding):
                operations.append(
                    Operation(
                        kind=OperationKind.BIND,
                        order=0,
                        instance_id=binding.caller_id,
                        target_id=binding.dependency_id,
                        binding_id=binding.id,
                        reason="deferred runtime binding",
                    )
                )

        if self.previous is not None:
            old_root_by_key = self.old_root_by_key
            for instance in sorted(self.instances.values(), key=lambda item: item.id):
                if not instance.request_key or not instance.active:
                    continue
                old_root_id = old_root_by_key.get(instance.request_key)
                if old_root_id and old_root_id != instance.id:
                    operations.append(
                        Operation(
                            kind=OperationKind.REPLACE,
                            order=0,
                            instance_id=instance.id,
                            replaces_id=old_root_id,
                            reason=f"switch stable handle {instance.request_key} to new instance",
                        )
                    )

            for ref in self.refs:
                if ref.instance_id in self.old_by_id:
                    operations.append(
                        Operation(
                            kind=OperationKind.RETAIN,
                            order=0,
                            instance_id=ref.instance_id,
                            reason=(
                                f"held by {ref.ref_id}"
                                + (f" from {ref.holder}" if ref.holder else "")
                            ),
                        )
                    )

            current_ids = set(self.instances)
            release_ids = [iid for iid in old_by_id if iid not in current_ids]
            release_set = set(release_ids)
            ordered_old = [str(iid) for iid in reversed(self.previous.start_order)]
            emitted: set[str] = set()
            for instance_id in ordered_old:
                if instance_id in release_set and instance_id not in emitted:
                    operations.append(
                        Operation(
                            kind=OperationKind.RELEASE,
                            order=0,
                            instance_id=instance_id,
                            reason="no longer reachable or retained",
                        )
                    )
                    emitted.add(instance_id)
            for instance_id in sorted(release_ids):
                if instance_id not in emitted:
                    operations.append(
                        Operation(
                            kind=OperationKind.RELEASE,
                            order=0,
                            instance_id=instance_id,
                            reason="no longer reachable or retained",
                        )
                    )

        return tuple(replace(operation, order=index) for index, operation in enumerate(operations))


class _VersionSortKey:
    """Wrapper giving versions a descending preference under ``min``."""

    __slots__ = ("version",)

    def __init__(self, version: Version):
        self.version = version

    def __lt__(self, other: "_VersionSortKey") -> bool:
        return self.version > other.version


def _provider_keys(module: str, group: str) -> tuple[str, ...]:
    """Sharing keys under which an explicitly loaded root provides itself."""

    return (
        f"shared:host:{module}",
        f"shared:group:{group}:{module}",
        f"singleton:host:{module}",
        f"singleton:group:{group}:{module}",
    )


def _normalize_plugins(
    plugins: Mapping[str, Iterable[PluginRequest]] | Iterable[PluginRequest] | None,
    previous: LoadPlan | None,
) -> tuple[tuple[PluginRequest, ...], tuple[str, ...]]:
    if plugins is None:
        if previous is None:
            from .model import PlannerSpecificationError

            raise PlannerSpecificationError("plugins are required for an initial plan")
        return previous.requests, previous.groups

    requests: list[PluginRequest] = []
    groups: list[str] = []

    if isinstance(plugins, Mapping):
        for group, group_requests in plugins.items():
            if group not in groups:
                groups.append(str(group))
            for request in group_requests:
                requests.append(_with_group(request, str(group)))
    else:
        for request in plugins:
            request = _as_request(request)
            requests.append(request)
            if request.group not in groups:
                groups.append(request.group)

    keys = [request.key for request in requests]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        from .model import PlannerSpecificationError

        raise PlannerSpecificationError(f"duplicate plugin request keys: {', '.join(duplicates)}")
    return tuple(requests), tuple(groups)


def _with_group(request: PluginRequest | Mapping[str, Any], group: str) -> PluginRequest:
    request = _as_request(request)
    if request.group == group:
        return request
    return replace(request, group=group)


def _as_request(value: PluginRequest | Mapping[str, Any]) -> PluginRequest:
    if isinstance(value, PluginRequest):
        return value
    if isinstance(value, Mapping):
        return PluginRequest(**value)
    raise TypeError(f"invalid plugin request: {value!r}")


def _as_implementation(
    value: Implementation | Mapping[str, Any],
) -> Implementation:
    if isinstance(value, Implementation):
        return value
    if isinstance(value, Mapping):
        return Implementation(**value)
    raise TypeError(f"invalid implementation: {value!r}")


def _as_ref(value: ObjectRef | Mapping[str, Any]) -> ObjectRef:
    if isinstance(value, ObjectRef):
        return value
    if isinstance(value, Mapping):
        return ObjectRef(**value)
    raise TypeError(f"invalid object reference: {value!r}")
