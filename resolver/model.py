"""Public data model for the dependency solver.

The solver models a package assembly platform that cross-compiles: build
tools are assembled in the *host* domain (they run on the build machine),
while runtime libraries are assembled in the *target* domain (they are
shipped in the produced image).  The same package name may be selected at
different versions in the two domains, because each domain resolves
independently and only its own platform/version constraints apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Sequence, Tuple

from .versions import Version, VersionRange, normalise_version


class Domain(str, Enum):
    """Assembly domain.

    ``HOST`` holds build tooling that runs on the build platform; ``TARGET``
    holds runtime libraries shipped inside the produced image.
    """

    HOST = "host"
    TARGET = "target"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Fixed domain order used by every tie-break and by result ordering.
DOMAIN_ORDER: Tuple[Domain, ...] = (Domain.HOST, Domain.TARGET)
DOMAIN_RANK = {domain: rank for rank, domain in enumerate(DOMAIN_ORDER)}


class DependencyKind(str, Enum):
    """Dependency type, which decides the destination domain."""

    BUILD = "build"  #: build tooling; always resolved in the host domain
    RUN = "run"  #: runtime dependency; follows the referrer's domain

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


def dependency_target_domain(kind: DependencyKind, referrer: Domain) -> Domain:
    """Return the domain a dependency is resolved in.

    Build dependencies always land in the host domain.  Run dependencies
    follow the domain of the package that declares them.
    """
    return Domain.HOST if kind is DependencyKind.BUILD else referrer


def _coerce_domain(value: object, label: str) -> Domain:
    if isinstance(value, Domain):
        return value
    try:
        return Domain(value)  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be one of {[d.value for d in DOMAIN_ORDER]}") from exc


def _coerce_kind(value: object) -> DependencyKind:
    if isinstance(value, DependencyKind):
        return value
    try:
        return DependencyKind(value)  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"dependency kind must be one of {[k.value for k in DependencyKind]}"
        ) from exc


def _coerce_platforms(value: Iterable[str]) -> frozenset:
    if isinstance(value, str):
        raise TypeError("platforms must be an iterable of platform names, not a single string")
    platforms = frozenset(value)
    for platform in platforms:
        if not isinstance(platform, str) or not platform:
            raise ValueError(f"platform names must be non-empty strings, got {platform!r}")
    return platforms


def _require_name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} name must be a non-empty string, got {value!r}")
    return value


def _coerce_range(value: object) -> VersionRange:
    if not isinstance(value, VersionRange):
        raise TypeError(f"expected a VersionRange, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class Dependency:
    """A dependency edge declared by one concrete package version.

    ``platforms`` is the set of platforms on which this dependency is
    required.  It is evaluated against the platform of the *destination*
    domain.  An empty set means "every platform".
    """

    name: str
    kind: DependencyKind = DependencyKind.RUN
    range: VersionRange = VersionRange.any()
    platforms: frozenset = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_name(self.name, "dependency"))
        object.__setattr__(self, "kind", _coerce_kind(self.kind))
        object.__setattr__(self, "range", _coerce_range(self.range))
        object.__setattr__(self, "platforms", _coerce_platforms(self.platforms))


@dataclass(frozen=True)
class PackageVersion:
    """One candidate package version.

    ``platforms`` lists the platforms this version can run on; an empty set
    means universal.  ``dependencies`` may be empty.
    """

    name: str
    version: Version
    dependencies: Tuple[Dependency, ...] = ()
    platforms: frozenset = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_name(self.name, "package"))
        object.__setattr__(self, "version", normalise_version(self.version))
        dependencies = tuple(self.dependencies)
        for dependency in dependencies:
            if not isinstance(dependency, Dependency):
                raise TypeError("package dependencies must be Dependency instances")
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "platforms", _coerce_platforms(self.platforms))


@dataclass(frozen=True)
class RootRequirement:
    """A root package requirement stated in a specific domain."""

    name: str
    domain: Domain
    range: VersionRange = VersionRange.any()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _require_name(self.name, "root requirement"))
        object.__setattr__(self, "domain", _coerce_domain(self.domain, "root requirement domain"))
        object.__setattr__(self, "range", _coerce_range(self.range))


@dataclass(frozen=True)
class LockedPackage:
    """A slot (domain, package) and the version recorded in the old lock."""

    domain: Domain
    name: str
    version: Version

    def __post_init__(self) -> None:
        object.__setattr__(self, "domain", _coerce_domain(self.domain, "locked package domain"))
        object.__setattr__(self, "name", _require_name(self.name, "locked package"))
        object.__setattr__(self, "version", normalise_version(self.version))

    @property
    def slot(self) -> Tuple[Domain, str]:
        return (self.domain, self.name)


@dataclass(frozen=True)
class ResolutionRequest:
    """Complete solver input.

    ``host_platform`` is the platform of the build machine; ``target_platform``
    is the platform of the produced image.  ``candidates`` is the finite
    universe of package versions.  ``lock`` is the previous lock used only by
    the "minimise re-versioned overlapping slots" objective, not as a hard
    constraint.
    """

    roots: Tuple[RootRequirement, ...]
    candidates: Tuple[PackageVersion, ...]
    host_platform: str
    target_platform: str
    lock: Tuple[LockedPackage, ...] = ()

    def __post_init__(self) -> None:
        roots = tuple(self.roots)
        for root in roots:
            if not isinstance(root, RootRequirement):
                raise TypeError("roots must be RootRequirement instances")
        candidates = tuple(self.candidates)
        seen_versions = set()
        for candidate in candidates:
            if not isinstance(candidate, PackageVersion):
                raise TypeError("candidates must be PackageVersion instances")
            slot = (candidate.name, candidate.version)
            if slot in seen_versions:
                raise ValueError(
                    f"duplicate candidate {candidate.name}@{candidate.version}"
                )
            seen_versions.add(slot)
        lock = tuple(self.lock)
        seen_slots = {}
        for entry in lock:
            if not isinstance(entry, LockedPackage):
                raise TypeError("lock entries must be LockedPackage instances")
            previous = seen_slots.get(entry.slot)
            if previous is not None and previous != entry.version:
                raise ValueError(
                    f"conflicting locked versions for {entry.domain}:{entry.name}: "
                    f"{previous} and {entry.version}"
                )
            seen_slots[entry.slot] = entry.version
        for label, platform in (
            ("host_platform", self.host_platform),
            ("target_platform", self.target_platform),
        ):
            if not isinstance(platform, str) or not platform:
                raise ValueError(f"{label} must be a non-empty string")
        object.__setattr__(self, "roots", roots)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "lock", lock)


@dataclass(frozen=True)
class Selection:
    """One resolved slot."""

    domain: Domain
    name: str
    version: Version

    @property
    def slot(self) -> Tuple[Domain, str]:
        return (self.domain, self.name)


@dataclass(frozen=True)
class ChainLink:
    """One hop of a dependency chain.

    The first hop of a chain is a root requirement: ``package`` and
    ``version`` are ``None`` and ``kind`` is ``None``.
    """

    domain: Domain
    package: Optional[str]
    version: Optional[Version]
    kind: Optional[DependencyKind]
    target_domain: Domain
    target: str
    constraint: VersionRange


def render_link(link: ChainLink) -> str:
    if link.package is None:
        return f"root[{link.domain}]: require {link.target} {link.constraint.describe()}"
    return (
        f"{link.domain}:{link.package}@{link.version} -[{link.kind}]-> "
        f"{link.target_domain}:{link.target} {link.constraint.describe()}"
    )


def render_chain(chain: Sequence[ChainLink]) -> str:
    return " -> ".join(render_link(link) for link in chain)


@dataclass(frozen=True)
class ConflictReport:
    """Explanation of an unsatisfiable request.

    ``chains`` contains one dependency chain per constraint that participates
    in the contradiction.  Every chain starts at a root requirement and ends
    with a constraint on ``domain``/``package``.
    """

    domain: Domain
    package: str
    reason: str
    chains: Tuple[Tuple[ChainLink, ...], ...]

    def render(self) -> str:
        lines = [f"conflict on {self.domain}:{self.package}: {self.reason}"]
        for chain in self.chains:
            lines.append(f"  {render_chain(chain)}")
        return "\n".join(lines)


@dataclass(frozen=True)
class ResolutionResult:
    """Solver output.  ``failure`` is ``None`` exactly when a solution exists."""

    selections: Tuple[Selection, ...]
    changed_locked_slots: int
    failure: Optional[ConflictReport] = None

    @property
    def ok(self) -> bool:
        return self.failure is None

    def selection_for(self, domain: Domain, name: str) -> Optional[Selection]:
        domain = _coerce_domain(domain, "domain")
        for selection in self.selections:
            if selection.domain is domain and selection.name == name:
                return selection
        return None
