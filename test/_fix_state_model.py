"""
State-model test support for `pip_audit._fix.resolve_fix_versions`.

This module is a test-only helper (no ``test_`` prefix, so pytest does not
collect it directly). It provides:

* a finite version catalog describing, per package version, the
  vulnerabilities present and their ordered fix candidates;
* a recording vulnerability service stub backed by that catalog, which
  records every query and refuses to make non-monotonic or unknown queries;
* an independent expected state machine that re-derives the suggestion
  process without importing the production implementation;
* deterministic short-version-chain generation and delta-debugging style
  reduction that preserves version ordering and divergence categories;
* a small assertion recorder shared by the pytest tests and
  ``tools/local_verify.py``.

Nothing here installs, upgrades, downloads or modifies any package: the only
production surface exercised is ``resolve_fix_versions`` and the
``VulnerabilityService`` / ``AuditState`` interfaces.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from packaging.version import Version

from pip_audit._fix import resolve_fix_versions
from pip_audit._service import (
    Dependency,
    ResolvedDependency,
    ServiceError,
    SkippedDependency,
    VulnerabilityResult,
    VulnerabilityService,
)
from pip_audit._state import AuditState

# ---------------------------------------------------------------------------
# Finite version catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VulnSpec:
    """
    A catalog-level vulnerability: an identifier and its ordered fix versions.
    """

    id: str
    fix_versions: tuple[Version, ...]

    def to_result(self) -> VulnerabilityResult:
        """
        Convert the catalog entry into the production ``VulnerabilityResult``.
        """

        return VulnerabilityResult(
            id=self.id,
            description=f"synthetic description for {self.id}",
            fix_versions=list(self.fix_versions),
            aliases=set(),
        )


@dataclass(frozen=True)
class VersionCatalog:
    """
    A finite, ordered catalog of package versions and their vulnerabilities.

    ``entries`` maps a version to the vulnerabilities present at that version.
    The catalog is the sole source of truth for the recording service stub:
    querying a version that is not in the catalog raises a synthetic
    ``ServiceError`` rather than inventing data.
    """

    package: str
    entries: dict[Version, tuple[VulnSpec, ...]]

    @property
    def versions(self) -> list[Version]:
        """
        Return the catalog versions in strict ascending PEP 440 order.
        """

        return sorted(self.entries)

    def vulns_at(self, version: Version) -> tuple[VulnSpec, ...]:
        """
        Return the vulnerabilities recorded at ``version`` (empty if clean).
        """

        return self.entries.get(version, ())

    def results_at(self, version: Version) -> list[VulnerabilityResult]:
        """
        Return production ``VulnerabilityResult`` objects for ``version``.
        """

        return [spec.to_result() for spec in self.vulns_at(version)]

    def validate(self) -> None:
        """
        Validate catalog invariants used by every scenario and generator.

        Every fix candidate must exist in the catalog and must be strictly
        higher than the version carrying the vulnerability; fix candidate
        lists must be ordered, de-duplicated and free of prerelease ordering
        surprises.
        """

        known = set(self.entries)
        for version, vulns in self.entries.items():
            seen_ids: set[str] = set()
            for vuln in vulns:
                if vuln.id in seen_ids:
                    raise ValueError(f"duplicate vulnerability {vuln.id} at {version}")
                seen_ids.add(vuln.id)
                fixes = list(vuln.fix_versions)
                if fixes != sorted(set(fixes)):
                    raise ValueError(
                        f"fix versions for {vuln.id} at {version} must be strictly ordered"
                    )
                for fix in fixes:
                    if fix not in known:
                        raise ValueError(
                            f"fix version {fix} for {vuln.id} is missing from the catalog"
                        )
                    if fix <= version:
                        raise ValueError(
                            f"fix version {fix} for {vuln.id} must be greater than {version}"
                        )


def build_catalog(package: str, rows: dict[str, list[tuple[str, list[str]]]]) -> VersionCatalog:
    """
    Build a ``VersionCatalog`` from a compact, replayable string mapping.

    Each row maps a version string to a list of ``(vuln_id, [fix, ...])``
    tuples, e.g. ``{"1.0.0": [("V-A", ["1.2.0"])]}``.
    """

    entries: dict[Version, tuple[VulnSpec, ...]] = {}
    for raw_version, raw_vulns in rows.items():
        version = Version(raw_version)
        entries[version] = tuple(
            VulnSpec(vuln_id, tuple(Version(fix) for fix in fixes))
            for vuln_id, fixes in raw_vulns
        )
    catalog = VersionCatalog(package=package, entries=entries)
    catalog.validate()
    return catalog


def catalog_to_rows(catalog: VersionCatalog) -> dict[str, list[tuple[str, list[str]]]]:
    """
    Serialize a catalog back into the compact string mapping used by builders.
    """

    return {
        str(version): [
            (vuln.id, [str(fix) for fix in vuln.fix_versions]) for vuln in catalog.vulns_at(version)
        ]
        for version in catalog.versions
    }


def initial_result_map(
    catalog: VersionCatalog, start: Version
) -> dict[Dependency, list[VulnerabilityResult]]:
    """
    Build the initial audit-result mapping consumed by ``resolve_fix_versions``.
    """

    dep = ResolvedDependency(name=catalog.package, version=start)
    return {dep: catalog.results_at(start)}


# ---------------------------------------------------------------------------
# Recording service stub and recording state actor
# ---------------------------------------------------------------------------


class UnknownCatalogQuery(ServiceError):
    """
    Raised when the stub is asked about a version absent from the catalog.

    It deliberately subclasses ``ServiceError`` so that tests can assert that
    a "no candidate" skip is never silently turned into a service failure.
    """


class NonMonotonicQuery(AssertionError):
    """
    Raised when the stub sees a query that does not strictly move upwards.
    """


class RecordingService(VulnerabilityService):
    """
    A vulnerability service stub backed by one catalog per package name.

    Every ``query`` call is recorded as a ``(package, version)`` pair. The
    stub additionally enforces, independently of the production code, that
    queries for one package move to strictly higher versions and never repeat.
    """

    def __init__(self, catalogs: dict[str, VersionCatalog]) -> None:
        """
        Create a stub from a mapping of package name to version catalog.
        """

        for catalog in catalogs.values():
            catalog.validate()
        self._catalogs = catalogs
        self.calls: list[tuple[str, Version]] = []
        self._last_seen: dict[str, Version] = {}

    def query(self, spec: Dependency) -> tuple[Dependency, list[VulnerabilityResult]]:
        """
        Record the query and return the catalog vulnerabilities for the spec.
        """

        if not isinstance(spec, ResolvedDependency):
            raise UnknownCatalogQuery(f"cannot query unresolved dependency {spec}")
        previous = self._last_seen.get(spec.name)
        if previous is not None and spec.version <= previous:
            raise NonMonotonicQuery(
                f"query for {spec.name} moved from {previous} to non-higher {spec.version}"
            )
        self._last_seen[spec.name] = spec.version
        self.calls.append((spec.name, spec.version))

        catalog = self._catalogs.get(spec.name)
        if catalog is None or spec.version not in catalog.entries:
            raise UnknownCatalogQuery(
                f"version {spec.version} of {spec.name} is not in the finite catalog"
            )
        return spec, catalog.results_at(spec.version)

    def calls_for(self, package: str) -> list[Version]:
        """
        Return the ordered query versions recorded for one package.
        """

        return [version for name, version in self.calls if name == package]


class RecordingStateActor:
    """
    A minimal ``AuditState`` member that captures every state message.
    """

    def __init__(self) -> None:
        """
        Initialize an empty message log.
        """

        self.messages: list[str] = []

    def update_state(self, message: str, logs: Optional[str] = None) -> None:
        """
        Record a state update (the ``logs`` argument is intentionally unused).
        """

        self.messages.append(message)

    def initialize(self) -> None:
        """
        No initialization is needed for the recording actor.
        """

    def finalize(self) -> None:
        """
        No finalization is needed for the recording actor.
        """


def recording_audit_state(actor: RecordingStateActor) -> AuditState:
    """
    Wrap a recording actor in a production ``AuditState`` instance.
    """

    return AuditState(members=[actor])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Independent expected state model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenceStep:
    """
    One transition of the independent expected state machine.
    """

    checked_version: Version
    vuln_ids: tuple[str, ...]
    candidate: Optional[Version]
    reason: str


@dataclass(frozen=True)
class ReferenceTrace:
    """
    The full expected trajectory for one initially vulnerable dependency.
    """

    package: str
    start: Version
    steps: tuple[ReferenceStep, ...]
    queries: tuple[Version, ...]
    state_messages: tuple[str, ...]
    outcome: "Outcome"


@dataclass(frozen=True)
class Outcome:
    """
    A normalized resolution outcome shared by the SUT and the reference model.
    """

    kind: str
    version: Optional[Version] = None
    skip_reason: Optional[str] = None

    @property
    def is_resolved(self) -> bool:
        """
        Whether the outcome is a concrete fix version suggestion.
        """

        return self.kind == OutcomeKinds.RESOLVED


class OutcomeKinds:
    """
    String constants for the normalized outcome categories.
    """

    RESOLVED = "resolved"
    SKIPPED_NO_CANDIDATE = "skipped_no_candidate"
    SERVICE_ERROR = "service_error"


def reference_resolve(
    catalog: VersionCatalog,
    start: Version,
    initial_vulns: Optional[Sequence[VulnSpec]] = None,
) -> ReferenceTrace:
    """
    Re-derive the expected fix-resolution trajectory from the catalog.

    This is an explicit state machine over immutable states. It does not call
    the production implementation; it only reads the catalog so that the
    production loop can be checked against an independently written model.

    At every state the machine (1) records which version is being checked and
    which vulnerabilities are active, (2) picks, for each active
    vulnerability, its first fix candidate above the current version, (3)
    moves to the maximum of those candidates and (4) loads the new version's
    catalog vulnerabilities. It stops on a clean version and reports
    ``skipped_no_candidate`` when a vulnerability has no higher fix version.
    """

    if start not in catalog.entries:
        raise ValueError(f"start version {start} is not in the catalog")

    current = start
    active: list[VulnSpec] = (
        list(initial_vulns) if initial_vulns is not None else list(catalog.vulns_at(start))
    )
    steps: list[ReferenceStep] = []
    queries: list[Version] = []
    messages: list[str] = []
    visited: set[tuple[Version, tuple[str, ...]]] = set()

    while active:
        active_ids = tuple(sorted(vuln.id for vuln in active))
        state_key = (current, active_ids)
        if state_key in visited:
            # A well-formed catalog can never trigger this; it guards against
            # accidental cycles in test data.
            raise AssertionError(f"reference model cycled at {current} with {active_ids}")
        visited.add(state_key)

        messages.append(
            f"Resolving fix version for {catalog.package}, checking {current}"
        )

        candidates: list[Version] = []
        missing: Optional[VulnSpec] = None
        for vuln in active:
            higher = [fix for fix in vuln.fix_versions if fix > current]
            if not higher:
                missing = vuln
                break
            candidates.append(higher[0])

        if missing is not None:
            reason = (
                f"failed to fix dependency {catalog.package} ({start}), unable to find fix "
                f"version for vulnerability {missing.id}"
            )
            steps.append(
                ReferenceStep(
                    checked_version=current,
                    vuln_ids=active_ids,
                    candidate=None,
                    reason="no higher fix candidate",
                )
            )
            return ReferenceTrace(
                package=catalog.package,
                start=start,
                steps=tuple(steps),
                queries=tuple(queries),
                state_messages=tuple(messages),
                outcome=Outcome(
                    OutcomeKinds.SKIPPED_NO_CANDIDATE, skip_reason=reason
                ),
            )

        next_version = max(candidates)
        steps.append(
            ReferenceStep(
                checked_version=current,
                vuln_ids=active_ids,
                candidate=next_version,
                reason="advance to highest earliest fix candidate",
            )
        )

        # The equivalent of the service query in the production loop.
        queries.append(next_version)
        current = next_version
        active = list(catalog.vulns_at(current))

    return ReferenceTrace(
        package=catalog.package,
        start=start,
        steps=tuple(steps),
        queries=tuple(queries),
        state_messages=tuple(messages),
        outcome=Outcome(OutcomeKinds.RESOLVED, version=current),
    )


# ---------------------------------------------------------------------------
# Production SUT execution
# ---------------------------------------------------------------------------


@dataclass
class SutRun:
    """
    The recorded evidence of one ``resolve_fix_versions`` invocation.
    """

    outcomes: dict[str, Outcome]
    fix_versions_raw: list[Any]
    service: RecordingService
    state_actor: RecordingStateActor
    error: Optional[BaseException] = None


def _dependency_outcome(raw: Any) -> Outcome:
    """
    Normalize a production ``FixVersion`` object into an ``Outcome``.
    """

    if raw.is_skipped():
        return Outcome(
            OutcomeKinds.SKIPPED_NO_CANDIDATE,
            skip_reason=getattr(raw, "skip_reason"),
        )
    return Outcome(OutcomeKinds.RESOLVED, version=getattr(raw, "version"))


def run_sut(
    catalogs: dict[str, VersionCatalog],
    result_map: dict[Dependency, list[VulnerabilityResult]],
) -> SutRun:
    """
    Run the production ``resolve_fix_versions`` against recording stubs.

    The function only consumes the generator; no package is installed or
    modified. A service-level failure is captured as a normalized outcome so
    callers can distinguish it from a genuine lack of candidates.
    """

    service = RecordingService(catalogs)
    actor = RecordingStateActor()
    state = recording_audit_state(actor)
    try:
        raw = list(resolve_fix_versions(service, result_map, state))
    except ServiceError as exc:
        return SutRun(
            outcomes={},
            fix_versions_raw=[],
            service=service,
            state_actor=actor,
            error=exc,
        )

    outcomes: dict[str, Outcome] = {}
    for fix in raw:
        outcomes[fix.dep.name] = _dependency_outcome(fix)
    return SutRun(
        outcomes=outcomes,
        fix_versions_raw=raw,
        service=service,
        state_actor=actor,
    )


def reference_run(
    catalogs: dict[str, VersionCatalog],
    deps: Sequence[tuple[str, Version]],
) -> dict[str, ReferenceTrace]:
    """
    Run the independent model for the given resolved dependencies.
    """

    traces: dict[str, ReferenceTrace] = {}
    for package, start in deps:
        traces[package] = reference_resolve(catalogs[package], start)
    return traces


def divergence(expected: Outcome, observed: Outcome) -> Optional[str]:
    """
    Compare two normalized outcomes; return ``None`` when they agree.
    """

    if expected.kind != observed.kind:
        return f"category differs: expected {expected.kind}, observed {observed.kind}"
    if expected.kind == OutcomeKinds.RESOLVED and expected.version != observed.version:
        return f"fix version differs: expected {expected.version}, observed {observed.version}"
    if (
        expected.kind == OutcomeKinds.SKIPPED_NO_CANDIDATE
        and expected.skip_reason != observed.skip_reason
    ):
        return (
            "skip reason differs: "
            f"expected {expected.skip_reason!r}, observed {observed.skip_reason!r}"
        )
    return None


# ---------------------------------------------------------------------------
# Deterministic short-version-chain generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneratedChain:
    """
    A generated, fully replayable short version chain.
    """

    seed: int
    mode: str
    package: str
    start: Version
    catalog: VersionCatalog
    expected: Outcome

    def rows(self) -> dict[str, list[tuple[str, list[str]]]]:
        """
        Return the compact replayable catalog rows for this chain.
        """

        return catalog_to_rows(self.catalog)


def _version_label(index: int, prerelease: bool) -> str:
    """
    Build a deterministic PEP 440 version label from a catalog index.
    """

    major = index
    if prerelease:
        # Prerelease sits immediately below the next final release, e.g.
        # index 2 -> "2.0.0a1" which sorts below "2.0.0".
        return f"{major}.0.0a1"
    return f"{major}.0.0"


def generate_chain(seed: int, mode: str) -> GeneratedChain:
    """
    Generate a short synthetic chain deterministically from ``seed``.

    ``mode`` is either ``"resolved"`` (the chain ends on a clean version and
    new vulnerabilities are discovered at intermediate candidates) or
    ``"no_candidate"`` (the last active vulnerability has no higher fix
    candidate). Both modes may include prerelease versions; all data is
    synthetic and the output is fully described by the seed.
    """

    if mode not in ("resolved", "no_candidate"):
        raise ValueError(f"unknown chain mode {mode!r}")

    rng = random.Random(seed)
    package = f"genpkg-{seed}"
    length = rng.randint(4, 6)
    # Ensure the generated chain contains at least one prerelease version.
    prerelease_indices = set(rng.sample(range(1, length), k=rng.randint(1, min(2, length - 1))))
    labels = [_version_label(index, index in prerelease_indices) for index in range(length)]
    versions = [Version(label) for label in labels]
    if versions != sorted(set(versions)):
        raise AssertionError("generator produced non-ordered versions")
    start = versions[0]

    rows: dict[str, list[tuple[str, list[str]]]] = {label: [] for label in labels}

    if mode == "resolved":
        # Each initial vulnerability targets a distinct later version; the
        # highest target becomes the first candidate and must leave at least
        # one version above it so a brand-new vulnerability can be discovered
        # there, with the final version always clean so the loop stops there.
        pool = list(range(2, length - 1))
        rng.shuffle(pool)
        target_count = min(rng.randint(1, 2), len(pool))
        target_indices = sorted(pool[:target_count])
        for vuln_index, target_index in enumerate(target_indices):
            vuln_id = f"VULN-{seed}-{vuln_index}"
            rows[str(start)].append((vuln_id, [labels[target_index]]))

        first_candidate_index = max(target_indices)
        new_count = rng.randint(1, 2)
        for vuln_index in range(new_count):
            new_id = f"VULN-{seed}-new-{vuln_index}"
            rows[labels[first_candidate_index]].append((new_id, [labels[-1]]))
        catalog = build_catalog(package, rows)
        trace = reference_resolve(catalog, start)
        if trace.outcome.kind != OutcomeKinds.RESOLVED:
            raise AssertionError("resolved-mode generator did not produce a resolved chain")
        return GeneratedChain(
            seed=seed,
            mode=mode,
            package=package,
            start=start,
            catalog=catalog,
            expected=trace.outcome,
        )

    # no_candidate mode: the start version carries a vulnerability whose fix
    # candidate exists, but the reached version has a vulnerability with no
    # higher candidate, forcing a skip rather than a service failure.
    first_target_index = rng.randint(2, length - 1)
    rows[str(start)].append((f"VULN-{seed}-0", [labels[first_target_index]]))
    rows[labels[first_target_index]].append((f"VULN-{seed}-dead", []))
    catalog = build_catalog(package, rows)
    trace = reference_resolve(catalog, start)
    if trace.outcome.kind != OutcomeKinds.SKIPPED_NO_CANDIDATE:
        raise AssertionError("no_candidate-mode generator did not produce a skipped chain")
    return GeneratedChain(
        seed=seed,
        mode=mode,
        package=package,
        start=start,
        catalog=catalog,
        expected=trace.outcome,
    )


# ---------------------------------------------------------------------------
# Chain reduction (delta debugging) that preserves divergence categories
# ---------------------------------------------------------------------------


def removable_versions(chain: GeneratedChain) -> list[Version]:
    """
    Return catalog versions that can be removed without breaking references.

    The start version and every version referenced as a fix candidate must be
    retained; everything else is a removable atom.
    """

    pinned: set[Version] = {chain.start}
    for vulns in chain.catalog.entries.values():
        for vuln in vulns:
            pinned.update(vuln.fix_versions)
    return [version for version in chain.catalog.versions if version not in pinned]


def _rebuild(chain: GeneratedChain, kept: Sequence[Version]) -> GeneratedChain:
    """
    Rebuild a chain after restricting its catalog to ``kept`` versions.
    """

    kept_set = set(kept)
    rows: dict[str, list[tuple[str, list[str]]]] = {}
    for version in chain.catalog.versions:
        if version not in kept_set:
            continue
        row: list[tuple[str, list[str]]] = []
        for vuln in chain.catalog.vulns_at(version):
            row.append((vuln.id, [str(fix) for fix in vuln.fix_versions]))
        rows[str(version)] = row
    catalog = build_catalog(chain.package, rows)
    trace = reference_resolve(catalog, chain.start)
    return GeneratedChain(
        seed=chain.seed,
        mode=chain.mode,
        package=chain.package,
        start=chain.start,
        catalog=catalog,
        expected=trace.outcome,
    )


def reduce_chain(
    chain: GeneratedChain,
    predicate: Callable[[GeneratedChain], bool],
) -> GeneratedChain:
    """
    Delta-debug a chain while ``predicate`` keeps holding.

    The reducer only deletes whole version atoms, so the surviving versions
    keep their original PEP 440 ordering. A predicate that pins the outcome
    category (and rejects service errors) therefore cannot turn a missing
    candidate problem into a service exception or vice versa.
    """

    atoms = removable_versions(chain)
    current = chain
    if not predicate(current):
        raise ValueError("reduce_chain predicate does not hold for the initial chain")

    while atoms:
        # Delta debugging: try coarse chunks first, halving down to single
        # atoms. Any accepted reduction restarts the shrinking pass.
        chunk_size = max(1, len(atoms) // 2)
        progressed = False
        while chunk_size >= 1 and not progressed:
            index = 0
            while index < len(atoms):
                chunk = set(atoms[index : index + chunk_size])
                kept = [version for version in current.catalog.versions if version not in chunk]
                try:
                    candidate = _rebuild(current, kept)
                    holds = predicate(candidate)
                except (ServiceError, ValueError, KeyError, AssertionError):
                    holds = False
                if holds:
                    current = candidate
                    atoms = [atom for atom in atoms if atom not in chunk]
                    progressed = True
                    break
                index += chunk_size
            if not progressed:
                chunk_size //= 2
        if not progressed:
            break
    return current


# ---------------------------------------------------------------------------
# Faulty oracle used to demonstrate divergence reduction
# ---------------------------------------------------------------------------


def faulty_oracle_outcome(chain: GeneratedChain) -> Outcome:
    """
    A deliberately wrong oracle used only to demonstrate reduction.

    It claims that every chain resolves to the catalog's highest version.
    For ``no_candidate`` chains this contradicts both the real SUT and the
    independent model, producing a category divergence that the reducer must
    shrink to a shortest explanatory chain.
    """

    return Outcome(OutcomeKinds.RESOLVED, version=chain.catalog.versions[-1])


def replay_chain(chain: GeneratedChain) -> tuple[SutRun, ReferenceTrace]:
    """
    Replay one chain through both the SUT and the independent model.
    """

    result_map = initial_result_map(chain.catalog, chain.start)
    sut = run_sut({chain.package: chain.catalog}, result_map)
    trace = reference_resolve(chain.catalog, chain.start)
    return sut, trace


# ---------------------------------------------------------------------------
# Fixed hand-written scenarios (C01-C03)
# ---------------------------------------------------------------------------


def scenario_c01_catalog() -> tuple[str, Version, VersionCatalog]:
    """
    Catalog for the "new vulnerability after all current ones are fixed" case.

    At 1.0.0 two vulnerabilities (V-A, V-B) are active. Advancing to the
    highest shared candidate (1.2.0) eliminates both but reveals V-C, whose
    fix candidate 1.3.0 is finally clean.
    """

    catalog = build_catalog(
        "alpha",
        {
            "1.0.0": [("V-A", ["1.1.0"]), ("V-B", ["1.2.0"])],
            "1.1.0": [],
            "1.2.0": [("V-C", ["1.3.0"])],
            "1.3.0": [],
        },
    )
    return "alpha", Version("1.0.0"), catalog


def scenario_c02_catalogs() -> dict[str, VersionCatalog]:
    """
    Hand-written catalogs covering prereleases, interleaving and dead ends.
    """

    prerelease = build_catalog(
        "beta-pre",
        {
            "1.0.0": [("V-PRE", ["1.1.0a1"])],
            "1.1.0a1": [("V-PRE2", ["1.1.0"])],
            "1.1.0": [],
        },
    )
    interleaved = build_catalog(
        "gamma-multi",
        {
            "2.0.0": [("V-X", ["2.1.0"]), ("V-Y", ["2.2.0"])],
            "2.1.0": [("V-X2", ["2.3.0"])],
            "2.2.0": [("V-Y2", ["2.3.0"])],
            "2.3.0": [("V-Z", ["2.4.0"])],
            "2.4.0": [],
        },
    )
    dead_end = build_catalog(
        "delta-dead",
        {
            "3.0.0": [("V-DEAD", [])],
        },
    )
    clean = build_catalog(
        "epsilon-clean",
        {
            "4.0.0": [],
            "4.1.0": [],
        },
    )
    return {
        prerelease.package: prerelease,
        interleaved.package: interleaved,
        dead_end.package: dead_end,
        clean.package: clean,
    }


def scenario_c03_deps() -> tuple[
    dict[str, VersionCatalog], dict[Dependency, list[VulnerabilityResult]]
]:
    """
    Build a multi-dependency input map with one skipped dependency included.
    """

    catalogs = scenario_c02_catalogs()
    result_map: dict[Dependency, list[VulnerabilityResult]] = {}
    for package, start in (
        ("beta-pre", Version("1.0.0")),
        ("gamma-multi", Version("2.0.0")),
        ("delta-dead", Version("3.0.0")),
    ):
        dep = ResolvedDependency(name=package, version=start)
        result_map[dep] = catalogs[package].results_at(start)

    clean_dep = ResolvedDependency(name="epsilon-clean", version=Version("4.0.0"))
    result_map[clean_dep] = []
    skipped_dep = SkippedDependency(name="zeta-skipped", skip_reason="audit skipped")
    result_map[skipped_dep] = [
        VulnerabilityResult(
            id="V-IGNORED",
            description="must never be queried because the dependency is skipped",
            fix_versions=[Version("9.9.9")],
            aliases=set(),
        )
    ]
    return catalogs, result_map


# ---------------------------------------------------------------------------
# Assertion recorder shared by pytest and tools/local_verify.py
# ---------------------------------------------------------------------------


@dataclass
class CheckRecorder:
    """
    Record assertions so the same checks serve pytest and the local verifier.

    When ``raise_on_fail`` is true (pytest mode) a failed check raises
    ``AssertionError`` immediately. Otherwise (verifier mode) the failure is
    recorded and execution continues, with the full tally emitted as JSON.
    """

    raise_on_fail: bool = True
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, condition: bool, label: str, detail: str = "") -> None:
        """
        Record one boolean assertion under a human-readable ``label``.
        """

        ok = bool(condition)
        self.checks.append((label, ok, detail))
        if not ok and self.raise_on_fail:
            suffix = f": {detail}" if detail else ""
            raise AssertionError(f"{label}{suffix}")

    def equal(self, actual: Any, expected: Any, label: str) -> None:
        """
        Assert equality and capture both sides in the failure detail.
        """

        self.check(
            actual == expected,
            label,
            f"expected={expected!r} observed={actual!r}",
        )

    @property
    def passed(self) -> int:
        """
        Number of recorded passing assertions.
        """

        return sum(1 for _, ok, _ in self.checks if ok)

    @property
    def failed(self) -> int:
        """
        Number of recorded failing assertions.
        """

        return sum(1 for _, ok, _ in self.checks if not ok)

    def labels(self) -> list[str]:
        """
        Return all recorded assertion labels in execution order.
        """

        return [label for label, _, _ in self.checks]

    def failures(self) -> list[dict[str, str]]:
        """
        Return structured records for every failed assertion.
        """

        return [
            {"label": label, "detail": detail}
            for label, ok, detail in self.checks
            if not ok
        ]
