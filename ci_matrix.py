"""Dependency-free matrix expansion for a small CI engine.

The module expands job matrices, resolves dependencies between expanded
instances, and detects cycles in the expanded instance graph.  It only builds
an in-memory plan; it does not execute jobs or perform I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Mapping, Sequence

# A dependency has concrete upstream instances and therefore must wait.
WAITING = "waiting"

# An optional dependency matched no expanded instance and is treated as done.
SATISFIED = "satisfied"

# A required dependency matched no expanded instance and blocks the instance.
UNSATISFIED = "unsatisfied"

_WHITE, _GRAY, _BLACK = 0, 1, 2


class MatrixSpecError(ValueError):
    """Raised when a static job or matrix specification is invalid."""


class MatrixPlanError(RuntimeError):
    """Raised when an expanded plan is not safe to run."""


@dataclass(frozen=True)
class DependencySpec:
    """Dependency from the current job to an upstream job.

    ``equal`` lists axis names whose values must be equal between the
    downstream and upstream instances.  Axes not listed here are unconstrained
    and allow multiple upstream instances to join into one downstream instance.
    """

    job: str
    equal: Sequence[str] = field(default_factory=tuple)
    required: bool = True


@dataclass(frozen=True)
class JobSpec:
    """A job template before matrix expansion."""

    name: str
    axes: Mapping[str, Sequence[str]]
    excludes: Sequence[Mapping[str, str]] = field(default_factory=tuple)
    dependencies: Sequence[DependencySpec] = field(default_factory=tuple)


@dataclass(frozen=True)
class _ExpandedInstance:
    id: int
    job_name: str
    coordinates: Mapping[str, str]


@dataclass(frozen=True)
class ResolvedDependency:
    """Result of matching one dependency rule against expanded upstream jobs."""

    upstream_job: str
    equal_dimensions: tuple[str, ...]
    required: bool
    matched_instance_ids: tuple[int, ...]
    state: str


@dataclass(frozen=True)
class MatrixInstance:
    """One concrete matrix instance with resolved dependency edges."""

    id: int
    job_name: str
    coordinates: Mapping[str, str]
    dependencies: Sequence[ResolvedDependency] = field(default_factory=tuple)

    @property
    def waiting_on(self) -> list[int]:
        """All expanded upstream instance IDs this instance must wait for."""
        return [
            instance_id
            for dependency in self.dependencies
            if dependency.state == WAITING
            for instance_id in dependency.matched_instance_ids
        ]

    @property
    def errors(self) -> list[str]:
        """Pre-run errors caused by required dependencies with no match."""
        result: list[str] = []
        for dependency in self.dependencies:
            if dependency.state != UNSATISFIED:
                continue
            dimensions = ", ".join(dependency.equal_dimensions) or "<none>"
            result.append(
                "required dependency "
                f"{dependency.upstream_job!r} matched no expanded instance "
                f"(equal dimensions: {dimensions})"
            )
        return result

    @property
    def is_blocked(self) -> bool:
        return bool(self.errors)

    @property
    def status(self) -> str:
        return "blocked" if self.is_blocked else "pending"


@dataclass(frozen=True)
class MatrixPlan:
    """Expanded instances and their dependency graph."""

    job_names: tuple[str, ...]
    instances: tuple[MatrixInstance, ...]
    ids_by_job: Mapping[str, tuple[int, ...]]

    def instance_ids_for_job(self, job_name: str) -> tuple[int, ...]:
        return self.ids_by_job[job_name]

    def blocked_instances(self) -> tuple[MatrixInstance, ...]:
        return tuple(instance for instance in self.instances if instance.is_blocked)

    def find_cycle_path(self) -> list[int]:
        """Return a closed path of expanded instance IDs.

        Edges point from a downstream instance to an upstream instance it waits
        for.  A returned path has the form ``[start, ..., start]``.  An empty
        list means there is no cycle among actual expanded instances.

        Only WAITING edges are included.  Empty optional dependencies have no
        edge, and empty required dependencies produce blocked errors but no
        edge to a non-existent instance.
        """
        adjacency = {
            instance.id: list(instance.waiting_on) for instance in self.instances
        }
        colors = {instance_id: _WHITE for instance_id in adjacency}
        next_edge: dict[int, int] = {}
        stack: list[int] = []

        for root in sorted(adjacency):
            if colors[root] != _WHITE:
                continue

            stack = [root]
            colors[root] = _GRAY
            next_edge[root] = 0

            while stack:
                node = stack[-1]
                neighbors = sorted(adjacency[node])
                edge_index = next_edge[node]

                if edge_index == len(neighbors):
                    colors[node] = _BLACK
                    stack.pop()
                    continue

                neighbor = neighbors[edge_index]
                next_edge[node] += 1

                if colors[neighbor] == _WHITE:
                    colors[neighbor] = _GRAY
                    stack.append(neighbor)
                    next_edge[neighbor] = 0
                elif colors[neighbor] == _GRAY:
                    start_index = stack.index(neighbor)
                    return stack[start_index:] + [neighbor]

        return []

    def format_instance_path(self, path: Sequence[int]) -> str:
        """Format an instance-ID path with job names and coordinates."""
        instances_by_id = {instance.id: instance for instance in self.instances}
        parts: list[str] = []

        for instance_id in path:
            instance = instances_by_id[instance_id]
            coordinates = ", ".join(
                f"{axis}={value}" for axis, value in instance.coordinates.items()
            )
            parts.append(f"{instance.job_name}#{instance.id}[{coordinates}]")

        return " -> ".join(parts)

    def validation_errors(self) -> list[str]:
        """All blocked-instance and cycle errors for the expanded plan."""
        errors: list[str] = []
        for instance in self.instances:
            errors.extend(instance.errors)

        cycle_path = self.find_cycle_path()
        if cycle_path:
            errors.append(
                "dependency cycle detected among expanded instances: "
                + " -> ".join(str(instance_id) for instance_id in cycle_path)
            )

        return errors

    def raise_if_invalid(self) -> None:
        """Perform the pre-run check and raise if the plan cannot run safely."""
        errors = self.validation_errors()
        if errors:
            raise MatrixPlanError("; ".join(errors))


def _matches_selector(
    coordinates: Mapping[str, str], selector: Mapping[str, str]
) -> bool:
    """A partial selector matches when every listed axis/value pair matches."""
    return all(coordinates.get(axis) == value for axis, value in selector.items())


def _validate_jobs(jobs: Sequence[JobSpec]) -> None:
    if not jobs:
        raise MatrixSpecError("at least one job is required")

    names: list[str] = []
    for job in jobs:
        if not job.name:
            raise MatrixSpecError("job names must be non-empty")
        if job.name in names:
            raise MatrixSpecError(f"duplicate job name: {job.name!r}")
        names.append(job.name)

        if not job.axes:
            raise MatrixSpecError(f"job {job.name!r} must declare at least one axis")

        for axis, values in job.axes.items():
            if not axis:
                raise MatrixSpecError(f"job {job.name!r} has an empty axis name")
            if not values:
                raise MatrixSpecError(
                    f"axis {axis!r} of job {job.name!r} has no values"
                )
            if len(values) != len(set(values)):
                raise MatrixSpecError(
                    f"axis {axis!r} of job {job.name!r} contains duplicate values"
                )

    specs_by_name = {job.name: job for job in jobs}

    for job in jobs:
        for exclusion in job.excludes:
            for axis, value in exclusion.items():
                if axis not in job.axes:
                    raise MatrixSpecError(
                        f"exclusion for job {job.name!r} refers to unknown axis "
                        f"{axis!r}"
                    )
                if value not in job.axes[axis]:
                    raise MatrixSpecError(
                        f"exclusion value {value!r} is not a value of axis "
                        f"{axis!r} in job {job.name!r}"
                    )

        seen_upstreams: set[str] = set()
        for dependency in job.dependencies:
            if not dependency.job:
                raise MatrixSpecError(
                    f"job {job.name!r} has a dependency with an empty job name"
                )
            if dependency.job not in specs_by_name:
                raise MatrixSpecError(
                    f"job {job.name!r} depends on unknown job "
                    f"{dependency.job!r}"
                )
            if dependency.job in seen_upstreams:
                raise MatrixSpecError(
                    f"job {job.name!r} declares multiple dependencies on "
                    f"{dependency.job!r}"
                )
            seen_upstreams.add(dependency.job)

            equal_dimensions = tuple(dependency.equal)
            if len(equal_dimensions) != len(set(equal_dimensions)):
                raise MatrixSpecError(
                    f"dependency {dependency.job!r} of job {job.name!r} "
                    "contains duplicate equality dimensions"
                )

            upstream = specs_by_name[dependency.job]
            for axis in equal_dimensions:
                if axis not in job.axes:
                    raise MatrixSpecError(
                        f"equality axis {axis!r} does not exist on downstream "
                        f"job {job.name!r}"
                    )
                if axis not in upstream.axes:
                    raise MatrixSpecError(
                        f"equality axis {axis!r} does not exist on upstream "
                        f"job {dependency.job!r}"
                    )


def expand_jobs(jobs: Sequence[JobSpec]) -> MatrixPlan:
    """Expand job matrices and resolve dependencies between instances.

    Instance IDs are zero-based global IDs.  Jobs are processed in input order,
    and each job's Cartesian product follows its axis dictionary order.
    Excluded combinations are removed before IDs are assigned.
    """
    job_specs = tuple(jobs)
    _validate_jobs(job_specs)

    expanded_by_job: dict[str, list[_ExpandedInstance]] = {}
    next_instance_id = 0

    for spec in job_specs:
        axis_names = tuple(spec.axes)
        expanded_instances: list[_ExpandedInstance] = []

        for values in product(*(spec.axes[axis] for axis in axis_names)):
            coordinates = dict(zip(axis_names, values))
            if any(
                _matches_selector(coordinates, selector)
                for selector in spec.excludes
            ):
                continue

            expanded_instances.append(
                _ExpandedInstance(
                    id=next_instance_id,
                    job_name=spec.name,
                    coordinates=coordinates,
                )
            )
            next_instance_id += 1

        expanded_by_job[spec.name] = expanded_instances

    resolved_instances: list[MatrixInstance] = []

    for spec in job_specs:
        for node in expanded_by_job[spec.name]:
            resolved_dependencies: list[ResolvedDependency] = []

            for dependency in spec.dependencies:
                equal_dimensions = tuple(dependency.equal)
                matched_ids = tuple(
                    upstream.id
                    for upstream in expanded_by_job[dependency.job]
                    if all(
                        upstream.coordinates[axis] == node.coordinates[axis]
                        for axis in equal_dimensions
                    )
                )

                if matched_ids:
                    state = WAITING
                elif dependency.required:
                    state = UNSATISFIED
                else:
                    state = SATISFIED

                resolved_dependencies.append(
                    ResolvedDependency(
                        upstream_job=dependency.job,
                        equal_dimensions=equal_dimensions,
                        required=dependency.required,
                        matched_instance_ids=matched_ids,
                        state=state,
                    )
                )

            resolved_instances.append(
                MatrixInstance(
                    id=node.id,
                    job_name=spec.name,
                    coordinates=node.coordinates,
                    dependencies=tuple(resolved_dependencies),
                )
            )

    ids_by_job = {
        spec.name: tuple(node.id for node in expanded_by_job[spec.name])
        for spec in job_specs
    }

    return MatrixPlan(
        job_names=tuple(spec.name for spec in job_specs),
        instances=tuple(resolved_instances),
        ids_by_job=ids_by_job,
    )
