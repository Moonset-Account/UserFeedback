# Local verification: cooperative cancellation of the lazy audit flow

This directory contains the replayable local verification harness for the
cooperative-cancellation work in `pip_audit/_audit.py`. Everything here uses
in-memory synthetic dependency sources, vulnerability-service stubs and
controllable cancellation tokens. There is no network access, no cloud
service, no real package installation and no credential handling.

## What changed in the API

`pip_audit/_audit.py` adds two public names and one optional parameter:

- `CancellationToken` — a simple cooperative token. It starts uncancelled;
  any holder calls `cancel()`, and the auditor polls `is_cancelled()` at
  dependency boundaries. Cancellation is sticky.
- `AuditCancelledError` — raised when the token is observed as cancelled.
  It is intentionally **not** a subclass of
  `pip_audit._service.interface.ServiceError`, so callers can distinguish
  three outcomes:
  - normal completion (`StopIteration` / exhausted iterator),
  - active cancellation (`AuditCancelledError`),
  - original service failure (`ServiceError` and subclasses, propagated
    unchanged).
- `Auditor.audit(source, cancellation_token=None)` — new optional keyword.
  When `None` (the default), the call behaves exactly as before, including
  the dry-run drain and the vulnerability de-duplication algorithm.

### Checkpoints and guarantees

- The token is checked **before pulling the next dependency** and **before
  publishing each result** to the caller.
- Already published results are retained. After cancellation the dependency
  source is not advanced further and any not-yet-delivered result is
  withheld (the in-flight `next` that produced it is allowed to finish).
- Cancellation is cooperative: a `next` already in flight — including work
  inside a custom `VulnerabilityService.query_all` generator — is never
  forcibly interrupted. No such interruption is promised.
- On cancellation, service error, or early `close()` of the outer
  generator, the internally held iterators that support `close` (the
  `query_all` result iterator, then the dependency-source iterator, in
  reverse acquisition order / LIFO) are closed.
- Exceptions raised while closing are logged and suppressed when an error
  is already in flight (including the `GeneratorExit` induced by generator
  close), so teardown errors never overwrite the original exception.
- Dry-run mode checks the token before each dependency it drains, so it can
  stop while consuming the source.
- The alias-based vulnerability de-duplication algorithm is unchanged.

## Commands

Run from the workspace root with the prepared virtual environment
(`python` resolves to `.venv/bin/python`):

```sh
# List the available cases
python tools/local_verify.py --list

# Run one case (prints one JSON record)
python tools/local_verify.py --case C01

# Run several or all cases
python tools/local_verify.py --case C01,C02
python tools/local_verify.py
```

Exit codes:

- `0` — every selected case ran at least one assertion and all assertions
  passed, with no skipped assertions or unexpected errors.
- `1` — at least one assertion failed, a case raised an unexpected
  exception, or a case executed zero assertions.
- `2` — bad arguments (e.g. an unknown case id) or no cases selected.

Each printed record contains `case`, `assertions` (the actual number of
assertions executed), `passed` / `failed` / `skipped`, any failure
messages, an `unexpected_error` field, and a `summary` with the synthetic
seed and the observed/expected event trace. Records deliberately contain
only synthetic package names (`pkg-a` …) and synthetic vulnerability ids;
no credentials, raw sensitive samples or private account data are emitted.

The same behavior is also covered by the project pytest suite:

```sh
python -m pytest test/test_audit_cancellation.py test/test_audit.py -q
```

## Replayable samples (what each case asserts)

All cases share the synthetic seed of three dependencies
`pkg-a`, `pkg-b`, `pkg-c` (each `ResolvedDependency` at version `1.0.0`),
an access-recording lazy source that emits events such as
`specs:next:pkg-a` / `specs:close`, and service stubs recording
`service:query*` events.

- **C01 — default ordering and published results.** Without a token, the
  source is pulled lazily and strictly in order; published results carry
  the stub's `VULN-PKG-*` ids; full consumption closes the source
  iterator. An explicit but never-cancelled token preserves the same
  order.
- **C02 — cancellation boundaries.** (A) a pre-cancelled audit raises
  `AuditCancelledError` without ever pulling the source and closes the
  iterator; (B) cancelling from inside a custom `query_all` while a `next`
  is in flight lets that `next` complete, withholds the result, and stops
  without a second source pull; (C) cancelling after one published result
  retains that result and raises at the next boundary without advancing
  the source. Also asserts `AuditCancelledError` is not a `ServiceError`.
- **C03 — close order and exception preservation.** (A) early caller
  `close()` closes the service iterator before the source iterator (LIFO)
  with nothing escaping; (B) when both closes fail during an early close,
  both teardown errors are logged/suppressed in LIFO order and do not
  replace the close; (C) a mid-stream `ServiceError("upstream outage")`
  propagates verbatim even though both internal iterators fail on close;
  (D) cancellation with failing closes still surfaces
  `AuditCancelledError`.
- **C04 — dry-run, custom generators, dedup.** Dry-run without a token
  drains the source, logs `would have audited 3 packages`, and closes the
  iterator; pre-cancelled and mid-consumption dry-runs stop at the
  boundary; a custom `query_all` generator completes its in-flight `next`
  and is then closed; an iterator lacking `close()` is tolerated; the
  alias-overlapping PYSEC/CVE pair still collapses to one PYSEC-prioritized
  result with the unioned alias set.

## Boundaries not covered by this harness

- **Thread/process safety of the token.** `CancellationToken` is a plain
  flag with no locking. Cross-thread signaling happens to work for a
  boolean flag in CPython, but cross-thread waits, timeouts and
  thread-safety guarantees are not tested or promised.
- **Forcible interruption.** The contract explicitly does not interrupt a
  `next`/`query_all` call already executing. A service that blocks
  indefinitely inside one `next` cannot be stopped cooperatively; that
  behavior is documented rather than tested with real blocking I/O.
- **Real services and network behavior.** The concrete PyPI/OSV/ESMS
  services, connection errors, retries and caching are not exercised; only
  in-memory stubs implementing the same interfaces are used.
- **Real dependency sources.** `pip list`, requirement files, pyproject and
  pylock sources (including virtualenv creation and resolver behavior) are
  outside these cases.
- **CLI integration.** `pip_audit/_cli.py` does not construct or expose a
  token yet; cancellation is an API-level capability. End-to-end CLI
  cancellation signaling is not wired up or tested here.
- **Async/cancellation scopes.** No `asyncio`/task-cancellation
  integration is provided; the token is synchronous and poll-based.
