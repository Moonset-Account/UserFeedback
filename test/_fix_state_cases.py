"""
Concrete state-model cases C01-C04 for ``resolve_fix_versions``.

Each ``run_cXX`` function only exercises suggestion computation through
``pip_audit._fix.resolve_fix_versions`` with in-memory service stubs; nothing
is installed, downloaded or modified. The functions are shared by the pytest
tests in ``test/test_fix.py`` and by ``tools/local_verify.py`` so that both
entry points execute the same real assertions.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from packaging.version import Version

from pip_audit._fix import FixVersion, ResolvedFixVersion, SkippedFixVersion
from pip_audit._service import ResolvedDependency, SkippedDependency

from test._fix_state_model import (
    CheckRecorder,
    GeneratedChain,
    Outcome,
    OutcomeKinds,
    ReferenceTrace,
    VersionCatalog,
    catalog_to_rows,
    divergence,
    faulty_oracle_outcome,
    generate_chain,
    initial_result_map,
    reduce_chain,
    reference_resolve,
    reference_run,
    replay_chain,
    run_sut,
    scenario_c01_catalog,
    scenario_c02_catalogs,
    scenario_c03_deps,
)

# A fixed, fully replayable seed range; every chain is derived deterministically.
RESOLVED_SEEDS = tuple(range(20))
NO_CANDIDATE_SEEDS = tuple(range(20, 40))


def _strictly_increasing(versions: Sequence[Version]) -> bool:
    """
    Return whether every adjacent pair moves to a strictly higher version.
    """

    return all(later > earlier for earlier, later in zip(versions, versions[1:]))


def _outcome_json(outcome: Outcome) -> dict[str, Any]:
    """
    Convert an ``Outcome`` into JSON-serializable evidence.
    """

    return {
        "kind": outcome.kind,
        "version": str(outcome.version) if outcome.version is not None else None,
        "skip_reason": outcome.skip_reason,
    }


def _trace_json(trace: ReferenceTrace) -> dict[str, Any]:
    """
    Convert an expected reference trace into JSON-serializable evidence.
    """

    return {
        "package": trace.package,
        "start": str(trace.start),
        "queries": [str(version) for version in trace.queries],
        "state_messages": list(trace.state_messages),
        "steps": [
            {
                "checked_version": str(step.checked_version),
                "vuln_ids": list(step.vuln_ids),
                "candidate": str(step.candidate) if step.candidate is not None else None,
                "reason": step.reason,
            }
            for step in trace.steps
        ],
        "outcome": _outcome_json(trace.outcome),
    }


def _check_trace_against_sut(
    recorder: CheckRecorder,
    label: str,
    catalog: VersionCatalog,
    start: Version,
) -> dict[str, Any]:
    """
    Run SUT and independent model for one dependency and cross-check them.
    """

    trace = reference_resolve(catalog, start)
    result_map = initial_result_map(catalog, start)
    sut = run_sut({catalog.package: catalog}, result_map)

    recorder.check(sut.error is None, f"{label}: SUT raises no service error", str(sut.error))
    recorder.equal(
        sorted(sut.outcomes),
        [catalog.package],
        f"{label}: exactly one suggestion produced",
    )
    observed = sut.outcomes[catalog.package]
    recorder.check(
        divergence(trace.outcome, observed) is None,
        f"{label}: SUT outcome matches independent state model",
        divergence(trace.outcome, observed) or "",
    )

    observed_queries = sut.service.calls_for(catalog.package)
    recorder.equal(
        [str(version) for version in observed_queries],
        [str(version) for version in trace.queries],
        f"{label}: query order matches model query order",
    )
    recorder.check(
        _strictly_increasing(observed_queries),
        f"{label}: every service query moves to a strictly higher version",
        f"queries={[str(version) for version in observed_queries]}",
    )
    if trace.queries:
        last_queried = trace.queries[-1]
        recorder.equal(
            len(catalog.vulns_at(last_queried)),
            0,
            f"{label}: querying stops on a version without vulnerabilities",
        )
    recorder.equal(
        sut.state_actor.messages,
        list(trace.state_messages),
        f"{label}: state messages follow the expected checking sequence",
    )
    return {
        "expected_trace": _trace_json(trace),
        "observed_outcome": _outcome_json(observed),
        "observed_queries": [str(version) for version in observed_queries],
        "state_messages": list(sut.state_actor.messages),
    }


# ---------------------------------------------------------------------------
# C01: all current vulnerabilities eliminated, then new candidates are checked
# ---------------------------------------------------------------------------


def run_c01(recorder: CheckRecorder) -> dict[str, Any]:
    """
    Model the "fix current vulns -> discover a new vuln -> keep checking" loop.
    """

    package, start, catalog = scenario_c01_catalog()
    trace = reference_resolve(catalog, start)

    # Independent structural expectations of the finite catalog itself.
    recorder.equal(
        sorted(vuln.id for vuln in catalog.vulns_at(start)),
        ["V-A", "V-B"],
        "C01: start version carries both initial vulnerabilities",
    )
    first_candidate = trace.steps[0].candidate
    recorder.equal(str(first_candidate), "1.2.0", "C01: highest earliest fix candidate is 1.2.0")
    recorder.check(
        first_candidate is not None
        and not {vuln.id for vuln in catalog.vulns_at(first_candidate)} & {"V-A", "V-B"},
        "C01: advancing eliminates all current vulnerabilities",
    )
    recorder.equal(
        [vuln.id for vuln in catalog.vulns_at(Version("1.2.0"))],
        ["V-C"],
        "C01: the candidate introduces a brand-new vulnerability V-C",
    )

    evidence = _check_trace_against_sut(recorder, "C01", catalog, start)
    recorder.equal(
        evidence["expected_trace"]["queries"],
        ["1.2.0", "1.3.0"],
        "C01: model queries 1.2.0 then 1.3.0",
    )
    recorder.equal(
        evidence["expected_trace"]["outcome"]["kind"],
        OutcomeKinds.RESOLVED,
        "C01: final suggestion is a resolved fix",
    )
    recorder.equal(
        evidence["expected_trace"]["outcome"]["version"],
        "1.3.0",
        "C01: suggested clean version is 1.3.0",
    )
    return {
        "case": "C01",
        "package": package,
        "start": str(start),
        "catalog_rows": catalog_to_rows(catalog),
        **evidence,
    }


# ---------------------------------------------------------------------------
# C02: prereleases, interleaving, dead ends, skipped and clean dependencies
# ---------------------------------------------------------------------------


def run_c02(recorder: CheckRecorder) -> dict[str, Any]:
    """
    Cover prerelease ordering, interleaved multi-vuln progress and dead ends.
    """

    catalogs = scenario_c02_catalogs()
    evidence: dict[str, Any] = {}

    # Prerelease chain: 1.0.0 -> 1.1.0a1 -> 1.1.0 (PEP 440 prerelease ordering).
    pre = catalogs["beta-pre"]
    recorder.check(
        Version("1.1.0a1") < Version("1.1.0"),
        "C02: prerelease candidate sorts below its final release",
    )
    pre_trace = reference_resolve(pre, Version("1.0.0"))
    recorder.equal(
        [str(version) for version in pre_trace.queries],
        ["1.1.0a1", "1.1.0"],
        "C02: prerelease chain advances through the prerelease candidate",
    )
    evidence["prerelease"] = _check_trace_against_sut(
        recorder, "C02 prerelease", pre, Version("1.0.0")
    )

    # Interleaved multi-vulnerability chain.
    interleaved = catalogs["gamma-multi"]
    inter_trace = reference_resolve(interleaved, Version("2.0.0"))
    recorder.equal(
        [str(version) for version in inter_trace.queries],
        ["2.2.0", "2.3.0", "2.4.0"],
        "C02: interleaved vulnerabilities advance through 2.2.0, 2.3.0, 2.4.0",
    )
    recorder.equal(
        [list(step.vuln_ids) for step in inter_trace.steps],
        [["V-X", "V-Y"], ["V-Y2"], ["V-Z"]],
        "C02: each state records the actively interleaved vulnerability set",
    )
    evidence["interleaved"] = _check_trace_against_sut(
        recorder, "C02 interleaved", interleaved, Version("2.0.0")
    )

    # Dead end: a vulnerability without any higher fix candidate is skipped.
    dead = catalogs["delta-dead"]
    dead_result_map = initial_result_map(dead, Version("3.0.0"))
    dead_sut = run_sut({dead.package: dead}, dead_result_map)
    dead_trace = reference_resolve(dead, Version("3.0.0"))
    recorder.check(dead_sut.error is None, "C02: dead end is a skip, not a service failure")
    recorder.equal(
        dead_trace.outcome.kind,
        OutcomeKinds.SKIPPED_NO_CANDIDATE,
        "C02: model classifies the dead end as missing-candidate skip",
    )
    dead_outcome = dead_sut.outcomes[dead.package]
    recorder.equal(
        dead_outcome.kind,
        OutcomeKinds.SKIPPED_NO_CANDIDATE,
        "C02: SUT emits a skipped fix version rather than inventing a candidate",
    )
    recorder.equal(
        dead_outcome.skip_reason,
        dead_trace.outcome.skip_reason,
        "C02: skip reason names the unfixable vulnerability",
    )
    recorder.equal(
        dead_sut.service.calls_for(dead.package),
        [],
        "C02: no service query is made when no higher candidate exists",
    )
    raw_dead = dead_sut.fix_versions_raw[0]
    recorder.check(
        isinstance(raw_dead, SkippedFixVersion) and raw_dead.is_skipped(),
        "C02: raw production object is a SkippedFixVersion",
    )
    evidence["dead_end"] = {
        "catalog_rows": catalog_to_rows(dead),
        "expected_trace": _trace_json(dead_trace),
        "observed_outcome": _outcome_json(dead_outcome),
        "observed_queries": [],
    }

    # Originally clean dependency: no suggestion and no queries at all.
    clean = catalogs["epsilon-clean"]
    clean_dep = ResolvedDependency(name=clean.package, version=Version("4.0.0"))
    clean_sut = run_sut({clean.package: clean}, {clean_dep: []})
    recorder.equal(clean_sut.outcomes, {}, "C02: clean dependency yields no suggestion")
    recorder.equal(
        clean_sut.service.calls_for(clean.package),
        [],
        "C02: clean dependency is never sent to the service",
    )
    evidence["clean_dependency"] = {
        "catalog_rows": catalog_to_rows(clean),
        "observed_outcome": None,
        "observed_queries": [],
    }

    # Skipped dependency: excluded before any service interaction.
    skipped_dep = SkippedDependency(name="zeta-skipped", skip_reason="audit skipped")
    skipped_sut = run_sut(
        {"zeta-skipped": clean},
        {
            skipped_dep: [
                catalogs["delta-dead"]
                .vulns_at(Version("3.0.0"))[0]
                .to_result()
            ]
        },
    )
    recorder.equal(
        skipped_sut.outcomes,
        {},
        "C02: skipped dependency produces no fix version",
    )
    recorder.equal(
        skipped_sut.service.calls,
        [],
        "C02: skipped dependency never reaches the service stub",
    )
    evidence["skipped_dependency"] = {"observed_queries": [], "observed_outcome": None}

    return {"case": "C02", "evidence": evidence}


# ---------------------------------------------------------------------------
# C03: query ordering, stop condition and per-dependency state isolation
# ---------------------------------------------------------------------------


def run_c03(recorder: CheckRecorder) -> dict[str, Any]:
    """
    Check strict ascending queries, clean-version stopping and state isolation.
    """

    catalogs, result_map = scenario_c03_deps()
    sut = run_sut(catalogs, result_map)
    recorder.check(sut.error is None, "C03: multi-dependency run raises no service error")

    resolved_deps = [
        ("beta-pre", Version("1.0.0")),
        ("gamma-multi", Version("2.0.0")),
        ("delta-dead", Version("3.0.0")),
    ]
    traces = reference_run(catalogs, resolved_deps)

    for package, start in resolved_deps:
        trace = traces[package]
        observed = sut.outcomes.get(package)
        recorder.check(
            observed is not None and divergence(trace.outcome, observed) is None,
            f"C03: {package} outcome is independent and matches its own model",
            divergence(trace.outcome, observed) if observed else "missing outcome",
        )
        queries = sut.service.calls_for(package)
        recorder.check(
            _strictly_increasing(queries),
            f"C03: {package} queries are strictly ascending",
            f"queries={[str(version) for version in queries]}",
        )
        recorder.equal(
            [str(version) for version in queries],
            [str(version) for version in trace.queries],
            f"C03: {package} query sequence matches its model trace",
        )
        if trace.queries:
            recorder.equal(
                len(catalogs[package].vulns_at(trace.queries[-1])),
                0,
                f"C03: {package} stops querying once its version is clean",
            )

    # Clean and skipped dependencies never touch the service.
    recorder.equal(
        sut.service.calls_for("epsilon-clean"),
        [],
        "C03: clean dependency contributes no queries",
    )
    recorder.equal(
        sut.service.calls_for("zeta-skipped"),
        [],
        "C03: skipped dependency contributes no queries",
    )
    recorder.check(
        "epsilon-clean" not in sut.outcomes and "zeta-skipped" not in sut.outcomes,
        "C03: clean and skipped dependencies contribute no suggestions",
    )

    # State messages must form one contiguous block per dependency, each block
    # matching that dependency's independent model trace exactly.
    blocks: dict[str, list[int]] = {}
    for index, message in enumerate(sut.state_actor.messages):
        owner = next(
            (package for package, _ in resolved_deps if f"for {package}," in message),
            None,
        )
        recorder.check(
            owner is not None,
            "C03: every state message names a known dependency",
            message,
        )
        if owner is not None:
            blocks.setdefault(owner, []).append(index)
    for package, indices in blocks.items():
        recorder.equal(
            indices,
            list(range(indices[0], indices[-1] + 1)),
            f"C03: {package} state updates form one contiguous block",
        )
        block_messages = [sut.state_actor.messages[index] for index in indices]
        recorder.equal(
            block_messages,
            list(traces[package].state_messages),
            f"C03: {package} state block is isolated from other dependencies",
        )

    # Every raw emitted object is a production FixVersion; nothing is installed.
    recorder.check(
        all(isinstance(item, FixVersion) for item in sut.fix_versions_raw),
        "C03: only fix-version suggestions are emitted (no installation side effects)",
    )

    return {
        "case": "C03",
        "per_dependency": {
            package: {
                "start": str(start),
                "expected_trace": _trace_json(traces[package]),
                "observed_outcome": _outcome_json(sut.outcomes[package]),
                "observed_queries": [
                    str(version) for version in sut.service.calls_for(package)
                ],
            }
            for package, start in resolved_deps
        },
        "state_messages": list(sut.state_actor.messages),
        "all_service_calls": [[name, str(version)] for name, version in sut.service.calls],
    }


# ---------------------------------------------------------------------------
# C04: generated short chains, divergence reduction and evidence
# ---------------------------------------------------------------------------


def _chain_evidence(chain: GeneratedChain) -> dict[str, Any]:
    """
    Collect replayable catalog, suggestion, skip reason and call evidence.
    """

    sut, trace = replay_chain(chain)
    observed = sut.outcomes.get(chain.package)
    return {
        "seed": chain.seed,
        "mode": chain.mode,
        "package": chain.package,
        "start": str(chain.start),
        "catalog_rows": chain.rows(),
        "expected_outcome": _outcome_json(chain.expected),
        "observed_outcome": _outcome_json(observed) if observed is not None else None,
        "observed_queries": [
            str(version) for version in sut.service.calls_for(chain.package)
        ],
        "model_queries": [str(version) for version in trace.queries],
    }


def _is_subsequence(earlier: Sequence[Version], later: Sequence[Version]) -> bool:
    """
    Return whether ``earlier`` is an order-preserving subsequence of ``later``.
    """

    cursor = 0
    for version in earlier:
        while cursor < len(later) and later[cursor] != version:
            cursor += 1
        if cursor == len(later):
            return False
        cursor += 1
    return True


def run_c04(recorder: CheckRecorder) -> dict[str, Any]:
    """
    Generate short chains, verify them, then reduce them without changing
    ordering or turning missing candidates into service errors.
    """

    chains = [generate_chain(seed, "resolved") for seed in RESOLVED_SEEDS]
    chains.extend(generate_chain(seed, "no_candidate") for seed in NO_CANDIDATE_SEEDS)
    recorder.equal(len(chains), 40, "C04: 40 deterministic short chains are generated")

    samples: list[dict[str, Any]] = []
    for chain in chains:
        label = f"C04 seed={chain.seed} mode={chain.mode}"
        # Generator catalog invariants, including strict version ordering.
        recorder.equal(
            chain.catalog.versions,
            sorted(chain.catalog.versions),
            f"{label}: catalog versions stay strictly ordered",
        )
        sut, trace = replay_chain(chain)
        recorder.check(
            sut.error is None,
            f"{label}: no service error during replay",
            str(sut.error),
        )
        observed = sut.outcomes.get(chain.package)
        recorder.check(observed is not None, f"{label}: one outcome is produced")
        if observed is not None:
            recorder.check(
                divergence(chain.expected, observed) is None,
                f"{label}: SUT matches the independent model",
                divergence(chain.expected, observed) or "",
            )
        queries = sut.service.calls_for(chain.package)
        recorder.check(
            _strictly_increasing(queries),
            f"{label}: all queries strictly ascend",
            f"queries={[str(version) for version in queries]}",
        )
        recorder.equal(
            [str(version) for version in queries],
            [str(version) for version in trace.queries],
            f"{label}: query evidence matches the model trace",
        )
        if chain.mode == "resolved":
            recorder.equal(
                observed.kind if observed else None,
                OutcomeKinds.RESOLVED,
                f"{label}: resolved chain yields a concrete suggestion",
            )
            recorder.equal(
                len(chain.catalog.vulns_at(queries[-1])),
                0,
                f"{label}: resolved chain stops on a clean version",
            )
        else:
            recorder.equal(
                observed.kind if observed else None,
                OutcomeKinds.SKIPPED_NO_CANDIDATE,
                f"{label}: dead-end chain yields a missing-candidate skip",
            )
            recorder.check(
                observed is not None and observed.skip_reason is not None,
                f"{label}: skip reason is recorded",
            )
        samples.append(_chain_evidence(chain))

    # Reduction: shrink every chain while preserving its divergence category
    # relative to the model, and never accepting a service error.
    reduced_samples: list[dict[str, Any]] = []
    for chain in chains:
        original_versions = list(chain.catalog.versions)

        def predicate(candidate: GeneratedChain, _chain: GeneratedChain = chain) -> bool:
            candidate_sut, candidate_trace = replay_chain(candidate)
            if candidate_sut.error is not None:
                return False
            observed_outcome = candidate_sut.outcomes.get(candidate.package)
            if observed_outcome is None:
                return False
            if divergence(candidate_trace.outcome, observed_outcome) is not None:
                return False
            return observed_outcome.kind == _chain.expected.kind

        reduced = reduce_chain(chain, predicate)
        label = f"C04 reduce seed={chain.seed}"
        recorder.check(
            len(reduced.catalog.versions) <= len(original_versions),
            f"{label}: reduction never grows the chain",
        )
        recorder.check(
            _is_subsequence(reduced.catalog.versions, original_versions),
            f"{label}: reduction preserves original version ordering",
        )
        reduced_sut, reduced_trace = replay_chain(reduced)
        recorder.check(
            reduced_sut.error is None,
            f"{label}: reduction never replaces the problem with a service error",
        )
        reduced_outcome = reduced_sut.outcomes.get(reduced.package)
        recorder.check(
            reduced_outcome is not None
            and reduced_outcome.kind == chain.expected.kind,
            f"{label}: divergence category survives reduction",
        )
        if chain.mode == "no_candidate":
            recorder.check(
                reduced_outcome is not None
                and reduced_outcome.kind == OutcomeKinds.SKIPPED_NO_CANDIDATE,
                f"{label}: missing-candidate skip is not converted into a service exception",
            )
        reduced_samples.append(
            {
                "seed": chain.seed,
                "mode": chain.mode,
                "original_versions": [str(version) for version in original_versions],
                "reduced_versions": [
                    str(version) for version in reduced.catalog.versions
                ],
                "reduced_rows": reduced.rows(),
                "reduced_outcome": _outcome_json(reduced_trace.outcome),
            }
        )

    # Demonstrate explaining a divergence from the shortest possible chain
    # using a deliberately faulty oracle that claims every chain resolves.
    demo_chain = generate_chain(NO_CANDIDATE_SEEDS[0], "no_candidate")

    def divergence_predicate(candidate: GeneratedChain) -> bool:
        candidate_sut, _ = replay_chain(candidate)
        if candidate_sut.error is not None:
            return False
        observed_outcome = candidate_sut.outcomes.get(candidate.package)
        return (
            observed_outcome is not None
            and divergence(faulty_oracle_outcome(candidate), observed_outcome) is not None
        )

    shortest = reduce_chain(demo_chain, divergence_predicate)
    shortest_sut, shortest_trace = replay_chain(shortest)
    shortest_outcome = shortest_sut.outcomes[shortest.package]
    recorder.equal(
        shortest_outcome.kind,
        OutcomeKinds.SKIPPED_NO_CANDIDATE,
        "C04: shortest divergent chain still shows the missing-candidate skip",
    )
    recorder.equal(
        faulty_oracle_outcome(shortest).kind,
        OutcomeKinds.RESOLVED,
        "C04: faulty oracle still claims a resolution on the shortest chain",
    )
    recorder.check(
        _is_subsequence(shortest.catalog.versions, demo_chain.catalog.versions),
        "C04: shortest chain preserves the original ordering",
    )
    recorder.check(
        len(shortest.catalog.versions) <= len(demo_chain.catalog.versions),
        "C04: shortest chain is no larger than the generated chain",
    )

    # Final no-installation guarantee: only FixVersion suggestion objects ever
    # leave the production API, and every service call is a catalog lookup.
    for chain in chains:
        sut, _ = replay_chain(chain)
        recorder.check(
            all(
                isinstance(item, (ResolvedFixVersion, SkippedFixVersion))
                for item in sut.fix_versions_raw
            ),
            f"C04 seed={chain.seed}: outputs are suggestion objects only, nothing is installed",
        )
        for name, version in sut.service.calls:
            recorder.check(
                version in chain.catalog.entries,
                f"C04 seed={chain.seed}: call evidence resolves inside the finite catalog",
                f"call={(name, str(version))}",
            )

    return {
        "case": "C04",
        "seed_ranges": {
            "resolved": list(RESOLVED_SEEDS),
            "no_candidate": list(NO_CANDIDATE_SEEDS),
        },
        "samples": samples,
        "reduced_samples": reduced_samples,
        "shortest_divergence_chain": {
            "rows": shortest.rows(),
            "versions": [str(version) for version in shortest.catalog.versions],
            "model_and_sut_outcome": _outcome_json(shortest_trace.outcome),
            "faulty_oracle_outcome": _outcome_json(faulty_oracle_outcome(shortest)),
            "skip_reason": shortest_outcome.skip_reason,
        },
    }


CASE_RUNNERS = {
    "C01": run_c01,
    "C02": run_c02,
    "C03": run_c03,
    "C04": run_c04,
}

CASE_DESCRIPTIONS = {
    "C01": (
        "Finite-catalog state model: eliminate all current vulns, "
        "then keep checking new candidates"
    ),
    "C02": (
        "Prereleases, interleaved vulns, no-higher-candidate skips, "
        "skipped and clean deps"
    ),
    "C03": (
        "Strict ascending query order, clean-version stopping and "
        "per-dependency state isolation"
    ),
    "C04": "Generated short version chains with ordering-preserving divergence reduction",
}
