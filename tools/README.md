# Local verification: `resolve_fix_versions` state-model cases

This directory contains the local verification entry point for the
state-model tests of `pip_audit/_fix.py::resolve_fix_versions`. The tests
themselves live in `test/test_fix.py` and share their implementation with the
verifier through two test-only helper modules:

- `test/_fix_state_model.py` — finite version catalog, recording service
  stub, independent expected state machine, deterministic short-chain
  generator, ordering-preserving chain reducer and assertion recorder;
- `test/_fix_state_cases.py` — the concrete cases `C01`–`C04` with all real
  assertions and replayable evidence.

The wrapper is a run entry point, not a standalone qualification: audits
should still read the code and real logs, and anything that cannot be
evidenced remains unknown.

## What is exercised (actual APIs)

Only suggestion computation is exercised; no package is installed, upgraded,
downloaded or modified, and no network service is contacted.

- Production entry point: `pip_audit._fix.resolve_fix_versions(service, result, state)`.
- Production value objects: `ResolvedDependency`, `SkippedDependency`,
  `VulnerabilityResult`, `ResolvedFixVersion`, `SkippedFixVersion`.
- Service interface: a `VulnerabilityService` subclass (`RecordingService`)
  backed by an in-memory `VersionCatalog`. It records every
  `(package, version)` query and independently rejects unknown or
  non-monotonic queries (`UnknownCatalogQuery`, a `ServiceError` subclass, and
  `NonMonotonicQuery`).
- State interface: `AuditState` with a recording member
  (`RecordingStateActor`) that captures every `update_state` message.
- Independent model: `reference_resolve` re-derives the expected trajectory
  from the catalog without calling production code, emitting an immutable
  `ReferenceTrace` (checked versions, active vulnerability ids, candidates,
  expected queries, state messages and normalized `Outcome`).

## Commands

Use the workspace virtual environment (Python 3.11 here):

```bash
# List the available cases
.venv/bin/python -I tools/local_verify.py --list

# Run one case
.venv/bin/python -I tools/local_verify.py --case C01

# Run several or all cases
.venv/bin/python -I tools/local_verify.py --case C01,C02
.venv/bin/python -I tools/local_verify.py --case all

# Equivalent pytest entry point (same assertions)
.venv/bin/python -m pytest test/test_fix.py -v
```

Each case prints one JSON record containing:

- `case` — the case identifier;
- `assertions` — actual `passed` / `failed` / `skipped` / `total` counts;
- `status` — `passed`, `failed`, `zero_assertions`, `unexpected_exception`,
  `missing_entry_point` or `unknown_case`;
- `failures` — labels and expected/observed details of failed assertions;
- `evidence` — synthetic catalog rows, seeds, expected versus observed
  outcomes, query order and state-message summaries.

Exit status is non-zero when any assertion fails, an entry point is missing,
a case is unimplemented, zero assertions ran, or an entire group was skipped.
Printing a success message alone is never treated as success. The output
contains only synthetic package names and synthetic vulnerability ids; no
credentials, real samples or private account data are emitted.

## Case coverage

- **C01 — eliminate current vulnerabilities, then keep checking new
  candidates.** A finite catalog (`alpha`, versions `1.0.0`–`1.3.0`) starts
  with two vulnerabilities (`V-A`, `V-B`). The highest earliest fix candidate
  `1.2.0` eliminates both but reveals a new vulnerability `V-C`; the loop
  continues to the clean `1.3.0`. The independent state model's trace is
  checked against the SUT outcome, query order and state messages.
- **C02 — prereleases, interleaving, dead ends, skips and clean deps.**
  - prerelease chain `1.0.0 -> 1.1.0a1 -> 1.1.0` (PEP 440 ordering);
  - interleaved multi-vulnerability chain `2.0.0 -> 2.2.0 -> 2.3.0 -> 2.4.0`
    with the active vulnerability set changing at every state;
  - a vulnerability with no higher fix candidate, which must become a
    `SkippedFixVersion` with the production skip reason and zero service
    queries — never a service exception;
  - a dependency that was originally clean and a dependency skipped during
    auditing: both produce no suggestions and no service calls.
- **C03 — query order, stopping condition and state isolation.** Across five
  dependencies, every recorded query moves to a strictly higher version,
  querying stops exactly on a clean version, per-dependency suggestions match
  per-dependency model traces, and state updates form one contiguous,
  correctly named block per dependency.
- **C04 — generated short chains and divergence reduction.** Forty
  deterministic chains (seeds `0–19` resolved, `20–39` no-candidate; the seed
  fully determines the catalog) are generated with prereleases and newly
  discovered vulnerabilities, replayed through SUT and model, then reduced by
  delta debugging. Reduction only removes whole version atoms, so surviving
  versions keep their original ordering and a missing-candidate skip can never
  be converted into a service error. A deliberately faulty oracle (claiming
  every chain resolves) is reduced to a shortest chain that still explains the
  category divergence, including the recorded skip reason.

## Replayable samples

Catalogs are plain JSON-able string mappings, so any sample in the verifier
output can be rebuilt directly:

```python
from test._fix_state_model import build_catalog, reference_resolve
from packaging.version import Version

rows = {
    "1.0.0": [["V-A", ["1.1.0"]], ["V-B", ["1.2.0"]]],
    "1.1.0": [],
    "1.2.0": [["V-C", ["1.3.0"]]],
    "1.3.0": [],
}
catalog = build_catalog("alpha", rows)
trace = reference_resolve(catalog, Version("1.0.0"))
print(trace.outcome)          # Outcome(kind='resolved', version=<Version('1.3.0')>)
print([str(v) for v in trace.queries])  # ['1.2.0', '1.3.0']
```

Generated chains are replayable from their seed:

```python
from test._fix_state_model import generate_chain, replay_chain

chain = generate_chain(20, "no_candidate")
sut_run, model_trace = replay_chain(chain)
print(chain.rows())  # fully describes the finite catalog
```

## Uncovered boundaries

The following are intentionally out of scope for this quality-assurance
change and remain unverified by these cases:

- real network services (OSV, PyPI, Ecosystems), caching, retries and
  service-specific error mapping — the stub only models the local catalog;
- actual package installation, virtual environments, dependency collection
  and any `DependencySource.fix` behavior;
- alias merging / `VulnerabilityResult.create` identifier prioritization and
  `published` timestamp handling — the synthetic results use empty alias sets;
- non-PEP-440 or unparseable version strings and catalogs containing
  duplicate, unsorted or non-existent fix candidates (the catalog builder
  rejects these up front, so such malformed inputs are not modeled);
- infinite resolution loops: well-formed catalogs guarantee strict upward
  movement, and the reference model contains a defensive cycle guard rather
  than a loop test;
- concurrency, cancellation and CLI/spinner rendering of `AuditState`;
- behavior of `resolve_fix_versions` when a concrete service raises
  `ServiceError` mid-resolution is represented only by the stub's
  `UnknownCatalogQuery`; the reducer asserts this category is never
  substituted for a genuine missing-candidate skip, but real-service
  propagation paths are not exercised.
