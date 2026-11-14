# SEC-20261114-009 — Account + source two-layer sliding-window quota adjudicator

An embeddable, in-process admission controller for a login edge service. Each
login attempt is judged against **two independent rolling windows** — one keyed
by the canonical account name, one keyed by the source address — plus
request-id deduplication. A request is admitted only if *both* layers have
room; if either is full, neither layer is charged.

* Language/runtime: Python 3 standard library only. No pip, no venv, no pytest,
  no third-party packages, no network.
* Entry point: `app.py`. Package: `quota/`. Tests: `tests/`. Verification:
  `tools/local_verify.py`.

## Quick start

```sh
# Adjudicate one request (prints JSON; exit 0 = allow, 2 = deny)
/usr/local/bin/python3 app.py decide \
    --config fixtures/config.json --request fixtures/request_allow.json

# Replay an ordered batch against one in-memory adjudicator
/usr/local/bin/python3 app.py batch \
    --config fixtures/config.json --requests fixtures/batch.json

# Injected-clock demonstrations (window boundary, retry, last-unit race, ...)
/usr/local/bin/python3 app.py demo

# Full local verification (unit suite + CLI sample/boundary assertions)
/usr/local/bin/python3 tools/local_verify.py
```

`/usr/local/bin/python3` is the interpreter pinned for this task; `python3` on
`PATH` resolves to the same 3.11 build.

## Embedding API

```python
from quota import LayerConfig, QuotaAdjudicator, QuotaConfig

adjudicator = QuotaAdjudicator(QuotaConfig(
    account=LayerConfig(limit=5, window_seconds=60),
    source=LayerConfig(limit=20, window_seconds=30),
    dedup_ttl_seconds=30,
))

decision = adjudicator.adjudicate(
    now=clock.now(),          # caller supplies time; no hidden wall clock
    tenant="tenant-a",
    account="alice",          # must already be canonical (see below)
    source="203.0.113.7",
    request_id="login-abc",
)
decision.allowed            # bool
decision.blocked_layers     # () on allow; ("account",), ("source",) or both
decision.retry_at           # earliest instant a retry can succeed, or None
decision.deduplicated       # True for a request-id replay
```

`QuotaAdjudicator.adjudicate(...)` is the whole contract:

* **Both layers or nothing.** Both windows are measured; the request is
  admitted only when both are under their limits. A denial records no event in
  either window, so a failed attempt never consumes capacity.
* **Rolling, not fixed buckets.** A request admitted at `t` counts over
  `[t, t + window_seconds)` and frees its slot at exactly `t + window_seconds`.
* **Request-id deduplication.** Within `dedup_ttl_seconds` (measured from the
  first decision), replaying `(tenant, request_id)` returns the first decision
  verbatim, marked `deduplicated=True`, and charges nothing — allows *and*
  denials are sticky. With `dedup_ttl_seconds = 0` dedup is disabled.
* **Parameter change = conflict.** Replaying an id with a different canonical
  account or source raises `RequestIdConflict`, which reports both the original
  and the incoming identity. The conflicting call charges nothing.
* **Per-tenant state.** Account buckets, source buckets, dedup entries and the
  clock high-water mark are all keyed by tenant; one tenant can neither spend
  nor block another's quota. The same request id under two tenants is two
  independent decisions.
* **Concurrency.** One internal `threading.Lock` covers measurement, recording,
  and dedup insertion, so two threads racing for the last unit of a layer
  produce exactly one admission. The decision objects are immutable.
* **Clock discipline.** `now` must be a finite number `>= 0`. For a given
  tenant, a *new* decision earlier than that tenant's latest decision raises
  `ClockRegression` instead of corrupting the sliding order. Replays and other
  tenants are not affected.
* **Retry advice.** A denial's `retry_at` is the maximum of the blocked layers'
  release instants, i.e. the first instant at which *all* blocking layers have
  room again. Each layer also reports its own `retry_at`.

State hygiene: `sweep(now)` drops expired window events and dedup entries;
`snapshot()` returns a diagnostic view. Neither affects accounting.

## Identity rules

* `account` must arrive **already canonical**. `quota.canonical_account_name`
  (trim + casefold) is provided for the login edge to normalize before calling;
  a non-canonical spelling is rejected with `ValidationError` rather than being
  silently folded, so callers cannot accidentally keep two counters for one
  account. `require_canonical_account` enforces this.
* `source` must be an IPv4/IPv6 literal; it is normalized to compressed form
  (`2001:0DB8::1` → `2001:db8::1`, IPv4-mapped forms normalized). Hostnames,
  CIDR blocks, IPv6 zone ids, and leading-zero IPv4 (`010.0.0.1`) are rejected —
  the last specifically so that two spellings cannot split a bucket.
* `tenant` and `request_id` must be non-empty strings without control
  characters or surrounding whitespace.

## CLI

All subcommands print exactly one JSON object to stdout.

### `decide`

```
app.py decide --config CONFIG.json --request REQUEST.json
```

`REQUEST.json`:

```json
{"tenant": "tenant-a", "account": "alice", "source": "203.0.113.7",
 "request_id": "login-001", "now": 0.0}
```

Each `decide` invocation is a fresh process with fresh state, so it always
adjudicates a first request. Use `batch` for stateful sequences.

### `batch`

```
app.py batch --config CONFIG.json --requests REQUESTS.json
```

`REQUESTS.json` is a list of request objects (or `{"requests": [...]}`),
evaluated in order against one adjudicator. Output:

```json
{"results": [ ...per-request decision or error, each with "index"... ],
 "allowed": 3, "denied": 3, "errors": 0}
```

### `demo`

Runs six self-checking, injected-clock scenarios and prints each scenario's
`requirement`, individual `checks` (expected vs actual), and `passed`. Covers:
window boundary (`t` denied / `t+window` allowed), retry advice and retrying at
it, the two-layer gate with no charge on denial, dedup replay and TTL expiry,
two threads racing for the last unit, and tenant isolation.

### Config file

```json
{"account": {"limit": 2, "window_seconds": 10},
 "source":  {"limit": 3, "window_seconds": 20},
 "dedup_ttl_seconds": 10}
```

`limit` must be an integer `>= 1`; `window_seconds` must be finite `> 0`;
`dedup_ttl_seconds` must be finite `>= 0`. Unknown or missing keys are
rejected.

## Output semantics

| Field | Meaning |
| --- | --- |
| `decision` | `"allow"` or `"deny"` |
| `allowed` | boolean mirror of `decision` |
| `reason` | `admitted`, `<layer>_rate_limited`, or `<a>_and_<b>_rate_limited` |
| `blocked_layers` | layers at their limit, in order `account`, `source` |
| `retry_at` | earliest instant all blocked layers admit again (`null` on allow) |
| `layers[]` | per layer: `limit`, `window_seconds`, `used`, `remaining`, `blocked`, `retry_at` |
| `decided_at` | clock value of the decision (first decision's value on replay) |
| `deduplicated` | `true` if this call replayed a stored decision |
| `first_decided_at` | timestamp of the original decision |
| `error` | `invalid_input`, `request_id_conflict`, `clock_regression`, or `io_error` |

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | allow (or all-allow batch, or passing demo/verification) |
| 1 | invalid input, IO error, or a failed demo/verification |
| 2 | deny (or a batch containing at least one deny) |
| 4 | a request-id conflict occurred |
| 5 | a clock regression occurred |

When a batch mixes outcomes, the highest-priority code wins: error (4/5) >
deny (2) > allow (0).

## Requirement traceability

Each stated requirement maps to a concrete test or sample with a stable
`case_id`. Unit-test ids are written as
`file::Class::method` (run them directly with
`python3 -m unittest tests.test_engine.TwoLayerGateTests.test_both_layers_must_have_room`);
CLI/demo ids are the `case_id` values in `verification_report.json`. No claim
below rests on the aggregate "suite passed" bit alone — each has its own
asserting case.

| # | Requirement | Stable case id(s) | Expected |
| --- | --- | --- | --- |
| 1 | Embeddable in-process adjudicator taking caller-supplied time, tenant, canonical account, source, request id | `tests/test_engine.py::TwoLayerGateTests::test_both_layers_must_have_room`; `cli-allow` | allow with both layers at 1 |
| 2 | Each layer has its own rolling window and limit | `tests/test_engine.py::SlidingWindowTests::test_sliding_not_fixed_bucket`; `tests/test_engine.py::SlidingWindowTests::test_boundary_is_half_open` | slot still held at `t+window-ε`; freed at `t+window`; sliding, not bucket-reset |
| 3 | A request is admitted only when both layers have room | `tests/test_engine.py::TwoLayerGateTests::test_both_layers_must_have_room`; `demo-two-layer-gate` | third request denied on `source` only |
| 4 | A denial charges neither layer | `tests/test_engine.py::TwoLayerGateTests::test_denial_charges_neither_layer`; `tests/test_engine.py::TwoLayerGateTests::test_denied_account_budget_is_intact_on_other_source`; `demo-two-layer-gate` | denied account `used == 0`; source stays at limit |
| 5 | Same tenant + same request id inside the TTL reuses the first decision | `tests/test_engine.py::DedupTests::test_replay_returns_first_decision_without_charging`; `tests/test_engine.py::DedupTests::test_replayed_denial_stays_denied`; `demo-dedup-replay` | `deduplicated=true`, `decided_at` unchanged, `used` unchanged |
| 6 | Changed parameters are a conflict | `tests/test_engine.py::DedupTests::test_changed_parameters_conflict`; `tests/test_engine.py::DedupTests::test_changed_source_conflicts`; `tests/test_engine.py::DedupTests::test_source_aliases_do_not_conflict`; `cli-request-id-conflict` | `RequestIdConflict` / `error: request_id_conflict`, exit 4; valid aliases replay |
| 7 | Tenants count independently | `tests/test_engine.py::TenantIsolationTests::test_account_and_source_buckets_are_per_tenant`; `demo-tenant-isolation` | tenant-b admitted while tenant-a is exhausted |
| 8 | Concurrent calls respect the limit | `tests/test_engine.py::ConcurrencyTests::test_race_for_last_account_unit`; `tests/test_engine.py::ConcurrencyTests::test_race_for_last_source_unit`; `tests/test_engine.py::ConcurrencyTests::test_many_workers_never_exceed_limit`; `demo-last-unit-race` | exactly 1 of 2, and exactly 3 of 16, admitted |
| 9 | A denial reports the triggering layer and earliest retry instant | `tests/test_engine.py::TwoLayerGateTests::test_retry_at_is_max_over_blocked_layers`; `tests/test_engine.py::TwoLayerGateTests::test_retry_after_both_layers_release`; `cli-deny-retry-advice` | `blocked_layers`, `retry_at` = max release instant |
| 10 | Injected clock demonstrates window boundary | `demo-window-boundary`; `cli-demo-scenarios` | `t=9.5` deny / `t=10.0` allow |
| 11 | Injected clock demonstrates retry | `demo-retry-advice` | retry half a unit early denied; at `retry_at` allowed |
| 12 | Injected clock demonstrates two requests racing for the last unit | `demo-last-unit-race` | one allow, one deny, no exceptions |

Regenerate all of the above with the command in the Verification section; the
report records the per-case input, expected value, and actual output. The last
run of this workspace is checked in as `verification_report.json`:
`discovered_test_count = 75`, `executed_not_skipped = 75`, `skipped = 0`,
`failed_cases = []`, `elapsed_seconds ≈ 5.4`, `within_time_budget = true`.

`tools/local_verify.py` also re-runs the sample and boundary claims through the
real CLI (`cli-*` case ids), so a requirement is evidenced by both its unit case
and an end-to-end CLI observation where one exists. Claims with no CLI case
(e.g. concurrency, clock regression) rest on their unit cases; they are not
inferred from the suite-level pass bit.

## Verification

`tools/local_verify.py` is the single acceptance command. It:

1. Discovers tests with `unittest.defaultTestLoader.discover("tests")` and
   refuses to proceed if `countTestCases() < 1`.
2. Runs the suite and counts tests that actually executed and were not skipped;
   zero executed, all-skipped, a runner exception, or any failure/error exits
   non-zero.
3. Replays the delivered sample/boundary assertions through the real `app.py`
   CLI in subprocesses, recording input, expected, and actual per case.
4. Enforces an 80-second budget and prints a JSON report with a stable
   `case_id` per check.

`tests/test_local_verify.py` also drives the wrapper against synthetic
zero-test, all-skipped, and failing suites to prove it fails in those modes
rather than passing vacuously.

## Limitations

* **In-process only.** State lives in the adjudicator object; there is no
  cross-process or cross-host coordination. Multi-instance deployments need a
  shared backend (e.g. Redis) and are out of scope.
* **Caller-supplied clock.** The adjudicator enforces monotonicity per tenant
  and within a window, but if the clock itself is wrong the decisions are
  wrong. `SystemClock` is `time.monotonic()`-based.
* **Local fixtures only.** No network, credentials, email, or external systems
  are contacted; all inputs are synthetic and offline.
* **Bounded memory by sweep.** Windows, empty buckets, and dedup entries are
  trimmed only as requests arrive or `sweep(now)` is called; an idle tenant
  holds its last state until then. Without a periodic `sweep`, cardinality grows
  with distinct accounts/sources/request ids seen. The per-tenant clock
  high-water mark is retained for the life of the adjudicator (one float per
  tenant).
* **Account normalization is upstream.** The adjudicator requires canonical
  names rather than guessing; unicode casefolding subtleties belong to the
  login edge that owns account identity.
