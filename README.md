# SEC-20261114-042 — fixed-length digest comparison trace regression

A small teaching implementation of a fixed-length byte-digest comparison, plus
a regression check that records **which byte positions were read and which
comparison steps ran**. Unit tests already prove the boolean result is right;
this project adds evidence that the *comparison path* does not change with the
position of the difference.

Everything is Python 3 standard library only. No pip, venv, pytest, network, or
external services are used.

## Layout

| Path | Role |
| --- | --- |
| `app.py` | CLI entry point (`audit`, `length-check`) |
| `digest_compare.py` | The comparator + trace types + length validation |
| `trace_audit.py` | Written full-scan spec, per-case audit records, report formatting |
| `synthetic_cases.py` | Deterministic offline fixtures (baseline + one per byte) |
| `reference_impls.py` | Deliberate early-return probe used only by tests |
| `tests/` | unittest suite (discovered by the wrapper) |
| `tools/local_verify.py` | Local verification gate (the required entry point) |

## Comparison semantics

`compare_digests(left, right, expected_length)`:

1. **Length validation first.** If either input is not exactly
   `expected_length` bytes, it raises `DigestLengthError` — independently, not
   folded into a `False` result. Both wrong sides are reported together in
   `actual_lengths`. `expected_length < 1` raises `ValueError`.
2. **Full scan.** For each position `p` in `0..length-1`, in order: read
   `left[p]`, read `right[p]`, xor, accumulate. The boolean is derived only after
   the loop, so the trace length is `4 * length` for every case.

`compare_digests_with_trace(...)` returns `(equal, ComparisonTrace)`. A
`ComparisonTrace` stores an ordered list of `(kind, position)` steps — kinds:
`read_left`, `read_right`, `xor`, `accumulate`. **No byte values are stored**, so
traces and reports never contain digest material.

## Input format

Inputs are synthetic, in-memory byte strings generated deterministically from
SHA-256 of a stable label (`synthetic_cases.derive_bytes`). No files, sockets,
or external calls.

Default case set (`build_cases(16)`):

- `same` — two copies of the baseline digest.
- `diff-00` … `diff-15` — baseline vs. baseline with byte `p` XOR `0x01`.

Boundary set (`boundary_cases()`): `boundary-same` and `boundary-diff-00`, both
length 1. `case_id` values are stable across runs.

## Output semantics

`python3 app.py audit [--length N]` prints, per group and per case:

```
case_id=diff-03 length=16 trace_difference=<summary> result_check=<ok|wrong-boolean> status=<PASS|FAIL>
```

then a per-group line `cases=.. passed=.. failed=.. changed_case_ids=..`. The
trace difference summary is either `none` or something like
`step_count 64->4; first_divergence=step 4 (one trace is a prefix)`. Output
contains only digest length, trace differences and status — **no digest bytes
and no digest hex**. Exit 0 only when every case passes.

`python3 app.py length-check` shows each wrong-length sample being rejected with
`DigestLengthError` and the observed length map.

## Local verification entry point

```
/usr/local/bin/python3 tools/local_verify.py [--timeout SECONDS]
```

The gate:

- runs `unittest.defaultTestLoader.discover("tests")` and checks
  `countTestCases() >= 1`;
- actually runs the suite and requires **executed (non-skipped) >= 1**;
- exits non-zero on zero discovered, all skipped, any failure/error, or any
  failing delivered sample — it does not inherit unittest's zero-test exit 0;
- additionally executes the delivered sample/boundary assertions (booleans,
  traces vs. the written spec, and the four length-guard samples) and prints
  each actual value next to its expected status;
- has a hard finite time budget (`--timeout`, default 75 s, SIGALRM).

Exit codes: `0` pass, `1` test/sample failure, `2` insufficient evidence
(zero tests / nothing executed), `3` timeout.

## Regression guarantee

`trace_audit.expected_signature(length)` is built from the written spec
independently of the comparator. If the comparison is later changed to return
early at the first difference, `app.py audit` fails and names the affected
groups in `changed_case_ids`, e.g. `changed_case_ids=diff-00,…,diff-14` — every
position except the last, since a difference on the last byte still scans all
bytes. `tests/test_digest_compare.py:EarlyReturnRegressionTests` asserts exactly
that set, and the wrapper's own samples fail so the gate cannot pass.

The suite also includes `tests/test_verify_gate.py` and
`tests/test_app_cli.py`, which prove the gate maps zero-test, all-skipped,
failure and sample-mismatch evidence to non-zero exits, and that boolean-only
evidence cannot mask a shortened comparison path.

## Requirement → evidence map

Actual values below come from the recorded runs of
`tools/local_verify.py` and `python3 app.py audit`.

| # | Requirement | Case / test id | Input | Assertion | Actual | Expected |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Fixed-length comparison returns equality | `ResultTests.test_identical_digest_reports_equal`, sample `per-position/same/boolean` | length 16, identical bytes | `compare_digests(...) is True` | `True` | `True` |
| 2 | A difference at each possible position is unequal | `test_each_single_position_difference_reports_unequal`, samples `per-position/diff-00…diff-15/boolean` | length 16, byte `p` flipped | `compare_digests(...) is False` for every `p` | `False` (16/16) | `False` |
| 3 | Wrong-length input errors independently | `LengthValidationTests.*`, samples `length-guard/{left-too-short,right-too-short,left-empty,both-too-long}` | 15/15, 16/15, 0/16, 17/17 bytes | `DigestLengthError`, not a boolean | `DigestLengthError lengths={'left': 15}` etc.; 4/4 PASS | dedicated error |
| 4 | Length check precedes any read | `test_length_is_checked_before_any_byte_is_read` | 15-byte exploding object | no `__getitem__` call before raise | raises `DigestLengthError` | same |
| 5 | Record read positions and steps | `TraceTests.test_trace_records_every_read_position`, sample `per-position/*/trace` | length 16 | left/right read positions `0..15`, `step_count == 64` | `(0,…,15)` twice, `steps=64` | same |
| 6 | Trace path invariant across difference positions | `test_trace_shape_is_identical_across_difference_positions` | 17 case groups | one unique signature | 1 signature | 1 |
| 7 | Trace matches independent written spec | `test_every_case_matches_written_full_scan_spec` | all groups | `trace_difference=none` | `none` (19/19 samples) | `none` |
| 8 | Boundary (length 1) cases | `ResultTests.test_boundary_single_byte_cases`, `boundary/*` | 1 byte | equal ⇒ `True`, diff ⇒ `False` | `True` / `False` | same |
| 9 | Report shows only length/trace/status | `ReportSafetyTests.*`, CLI output | full audit run | no digest bytes or hex in stdout | no digest material found | none leaked |
| 10 | Early return is caught and named | `EarlyReturnRegressionTests.*`, `test_audit_fails_when_comparison_returns_early` | patched early-return comparator, length 4 & 16 | exit 1; `changed_case_ids=diff-00,…,diff-14` (all but last) | exit 1; 15 changed ids at length 16 | named groups |
| 11 | Gate rejects thin evidence | `GateDecisionTests`, `WrapperGatePathTests` | 0 discovered / 0 executed / failure / bad sample | non-zero exit, specific reason | `zero_tests_discovered`, `no_actual_tests_executed`, `unittest_failures_or_errors`, `sample_assertion_failure` | non-zero |

Evidence not covered (deliberately not claimed as verified):

- True CPU-level constant-time behaviour — out of scope for a Python teaching
  example; only the recorded logical path is asserted.
- Any network/identity/cloud system behaviour — none is contacted or simulated
  beyond local in-process bytes.

## Limitations

- This is a **teaching example**, not a production constant-time primitive.
  Python-level bytecode, allocation and interpreter behaviour are not
  controlled; do not use it to protect real secrets.
- The trace models the intended algorithm; it does not prove true
  constant-time execution at the CPU level.
- Fixtures are synthetic and offline by design; there is no TLS, KMS or
  production digest source in scope.
- The wrapper verifies the claims its samples and suite encode. Claims not
  covered by a real test or sample remain unverified and are not claimed here.
