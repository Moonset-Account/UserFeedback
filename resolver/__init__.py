"""Cross-compilation dependency solver.

Public surface:

* :func:`resolve` — solve a :class:`~resolver.model.ResolutionRequest`.
* The data model in :mod:`resolver.model` (domains, dependency kinds,
  candidates, roots, lock, results).
* Version primitives in :mod:`resolver.versions`.
* Convenience constructors :func:`build`, :func:`run`, :func:`pkg`,
  :func:`root`, :func:`locked`.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence, Tuple

from .model import (
    DOMAIN_ORDER,
    DOMAIN_RANK,
    ChainLink,
    ConflictReport,
    Dependency,
    DependencyKind,
    Domain,
    LockedPackage,
    PackageVersion,
    ResolutionRequest,
    ResolutionResult,
    RootRequirement,
    Selection,
    dependency_target_domain,
    render_chain,
    render_link,
)
from .solver import ResolutionLimitError, resolve, slot_sort_key
from .versions import (
    Interval,
    Version,
    VersionRange,
    compare_versions,
    normalise_version,
)


def vrange(
    components: Optional[Mapping[int, Tuple[Optional[int], Optional[int]]]] = None,
) -> VersionRange:
    """Build a :class:`VersionRange` from ``{coord: (low, high)}`` pairs.

    ``None`` bounds are unbounded.  An empty mapping means any version.
    """
    if not components:
        return VersionRange.any()
    pairs = []
    for coord in sorted(components):
        low, high = components[coord]
        pairs.append((coord, Interval(low, high)))
    return VersionRange(tuple(pairs))


def build(
    name: str,
    range: VersionRange = VersionRange.any(),
    platforms: Iterable[str] = (),
) -> Dependency:
    """A build-tool dependency (resolved in the host domain)."""
    return Dependency(
        name=name, kind=DependencyKind.BUILD, range=range, platforms=frozenset(platforms)
    )


def run(
    name: str,
    range: VersionRange = VersionRange.any(),
    platforms: Iterable[str] = (),
) -> Dependency:
    """A runtime dependency (resolved in the referrer's domain)."""
    return Dependency(
        name=name, kind=DependencyKind.RUN, range=range, platforms=frozenset(platforms)
    )


def pkg(
    name: str,
    version: Sequence[int],
    dependencies: Iterable[Dependency] = (),
    platforms: Iterable[str] = (),
) -> PackageVersion:
    """A candidate package version."""
    return PackageVersion(
        name=name,
        version=normalise_version(version),
        dependencies=tuple(dependencies),
        platforms=frozenset(platforms),
    )


def root(
    name: str,
    domain: Domain | str,
    range: VersionRange = VersionRange.any(),
) -> RootRequirement:
    """A root requirement stated in ``domain``."""
    return RootRequirement(name=name, domain=Domain(domain), range=range)


def locked(
    domain: Domain | str,
    name: str,
    version: Sequence[int],
) -> LockedPackage:
    """A slot recorded in the previous lock."""
    return LockedPackage(
        domain=Domain(domain), name=name, version=normalise_version(version)
    )


def request(
    roots: Iterable[RootRequirement],
    candidates: Iterable[PackageVersion],
    host_platform: str,
    target_platform: str,
    lock: Iterable[LockedPackage] = (),
) -> ResolutionRequest:
    """Assemble a :class:`ResolutionRequest`."""
    return ResolutionRequest(
        roots=tuple(roots),
        candidates=tuple(candidates),
        host_platform=host_platform,
        target_platform=target_platform,
        lock=tuple(lock),
    )


__all__ = [
    "ChainLink",
    "ConflictReport",
    "DOMAIN_ORDER",
    "DOMAIN_RANK",
    "Dependency",
    "DependencyKind",
    "Domain",
    "Interval",
    "LockedPackage",
    "PackageVersion",
    "ResolutionLimitError",
    "ResolutionRequest",
    "ResolutionResult",
    "RootRequirement",
    "Selection",
    "Version",
    "VersionRange",
    "build",
    "compare_versions",
    "dependency_target_domain",
    "locked",
    "normalise_version",
    "pkg",
    "render_chain",
    "render_link",
    "request",
    "resolve",
    "root",
    "run",
    "slot_sort_key",
    "vrange",
]
