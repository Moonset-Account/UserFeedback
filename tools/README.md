# Local verification: RFC 5849 parameter normalisation

This directory contains a local verification harness for the change to
`oauthlib/oauth1/rfc5849/signature.py::normalize_parameters`.

Everything here runs offline against the workspace source and synthetic
fixtures. It does not contact the network, install packages, or print raw
parameter values or credentials.

## What changed

`normalize_parameters(params)` still returns the RFC 5849 §3.4.1.3.2
normalised parameter string, and still:

1. percent-encodes each name and value **first**,
2. sorts on the **encoded** name and value (ascending byte order),
3. joins with `=` and `&`, keeping duplicate pairs and empty values.

The implementation now:

- walks `params` once, so a one-shot iterator is accepted;
- memoises encoded names/values in a **per-call** dict capped by
  `ESCAPE_CACHE_LIMIT` (256 entries), so repeated fields are encoded once and
  the table cannot grow without bound; the dict is dropped when the call
  returns — no module-level or cross-request cache exists;
- formats `name=value` in place, replacing the previous intermediate list of
  tuples plus a second list of formatted parts.

Non-`str` names/values are passed straight to `utils.escape`, preserving the
existing `ValueError` type and message.

## Actual API

| Symbol | Meaning |
| --- | --- |
| `oauthlib.oauth1.rfc5849.signature.normalize_parameters(params) -> str` | Entry point. `params` is any iterable of `(name, value)` pairs, consumed exactly once. |
| `oauthlib.oauth1.rfc5849.signature.ESCAPE_CACHE_LIMIT` | `int`, the per-call memo capacity (256). |

Accepted inputs: list/tuple of 2-tuples, generator, or any other single-pass
iterable of 2-tuples. Dicts are **not** accepted as pairs (unchanged from
before: iterating a dict yields keys, not pairs).

## Commands

Use the workspace interpreter only:

```sh
.venv/bin/python tools/local_verify.py --list
.venv/bin/python tools/local_verify.py --case C01
.venv/bin/python tools/local_verify.py --case C01 C02 C03 C04
.venv/bin/python tools/local_verify.py --case C01,C02      # comma form works too
```

Each selected case emits one JSON object per line on stdout plus a human
summary on stderr. Exit codes:

- `0` — every selected case passed;
- `1` — any assertion failed, a case ran zero assertions, or a case was
  entirely skipped;
- `2` — unknown `--case` id.

JSON fields: `case`, `title`, `seed`, `assertions`, `passed`, `failed`,
`skipped`, `status`, `sample_or_seed` (seed plus structural notes such as
pair counts and an output fingerprint), and `failures` (expected/observed
summaries).

## Cases

- **C01** — compares against an independent reference that encodes with
  `urllib.parse.quote(safe="~")` and then sorts. Includes a vector where
  encode-then-sort differs from sort-then-encode (`k@` vs `k0`), verifies
  duplicate multiplicity with a `Counter`, and checks empty values keep `=`.
- **C02** — Unicode names/values, pre-existing `%XX`, literal `+`, and `~_.-`.
  Asserts `%` is re-encoded (no decode round), `+` becomes `%2B` rather than a
  space, and non-ASCII names sort by encoded bytes. Five malformed inputs are
  checked to raise the same type and message as the original implementation.
- **C03** — accepts a one-shot iterator (exactly one traversal). A counting
  `utils.escape` replacement proves 200 duplicated pairs collapse to 10
  encodes; a 1200-pair all-unique input records the memo size each call and
  asserts it never exceeds the cap; a repeat-call check shows no cross-call
  reuse; a canary value is checked absent from module globals and live dicts
  after the call.
- **C04** — benchmarks a 4000-pair highly duplicated input and a 3000-pair
  all-unique input, comparing time (best of 11) and peak allocation
  (`tracemalloc`, best of 5) against the verbatim original algorithm. Also
  loads and runs `tests/oauth1/rfc5849/test_signatures.py` and requires no
  failures/errors.

## Reproducible samples

All fixtures are generated from `random.Random(5849)` (`SEED` in
`tools/local_verify.py`), so re-running reproduces the same inputs. To inspect
a fixture yourself:

```sh
.venv/bin/python - <<'PY'
import random
rng = random.Random(5849)
names = ["user", "tag", "filter", "sort", "include", "group"]
values = [f"value-{i} with space & odd=%chars" for i in range(12)]
duplicated = [(rng.choice(names), rng.choice(values)) for _ in range(4000)]
print(len(duplicated), len({n for n, _ in duplicated}))
PY
```

The original signature tests can be run directly:

```sh
.venv/bin/python -m unittest tests.oauth1.rfc5849.test_signatures -v
```

Full oauth1 suite:

```sh
.venv/bin/python -m unittest discover -s tests/oauth1 -q
```

## Boundaries and what is *not* covered

- **Benchmark thresholds are machine- and interpreter-dependent.** C04 asserts
  loose bounds (duplicated < 0.60x time, < 0.90x peak; unique < 1.60x time,
  <= 1.10x peak) measured on CPython 3.11.6 (macOS). Different hardware,
  `-O`, or PyPy may shift ratios; the assertions are guards, not proofs.
- **`tracemalloc` reports Python-level allocations only.** Native allocator
  use, RSS, and runaway memory under adversarial sizes are not measured.
- **Retention check is targeted, not a heap audit.** `scan_for_value` looks in
  module-level containers of `signature` and live dicts; it does not prove the
  absence of every possible reference.
- **Concurrency is not exercised.** The memo is per-call local, so no shared
  state exists, but no threaded stress test is included.
- **Scope is `normalize_parameters` only.** `collect_parameters`,
  `signature_base_string`, the HMAC/RSA/PLAINTEXT sign and verify paths, and
  the oauth2/openid modules are untouched; pre-existing oauth2 test failures in
  this environment come from the missing `blinker` dependency and are unrelated.
- **Not measured here:** `str` subclasses bypass the memo (they are encoded by
  `utils.escape` directly); behaviour for very large single strings; cache
  effectiveness when the number of distinct fields exceeds the cap.
- The harness is a running entry point, not a standalone certificate. Reading
  the code and the emitted JSON is still required.
