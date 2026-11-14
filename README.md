# Parser regression summary (SEC-20261114-028)

Compares a **confirmed baseline** export of structured test results with the
**current build** export and summarises, per comparable sample group:

* new failures,
* fixed samples,
* persistent failures,
* unstable samples (pass + fail inside one group),
* samples that were not executed (all attempts skipped),
* samples present in the baseline but absent from the current export,
* content that only appears under a different parse mode / environment,
* duplicate-attempt conflicts that are held for confirmation.

Every emitted row carries the sample/content summary, parse mode, build
identifier, environment, attempt numbers, failure categories and a timeout
origin label. Python 3 standard library only — no pip, venv, pytest or network
access.

## Entry points

| Purpose | Command |
| --- | --- |
| Human/machine summary | `/usr/local/bin/python3 app.py --baseline BASE.json --current CUR.json` |
| Machine-readable JSON | `/usr/local/bin/python3 app.py --baseline BASE.json --current CUR.json --format json` |
| Write to a file | `/usr/local/bin/python3 app.py --baseline BASE.json --current CUR.json --output summary.md` |
| Offline verification | `/usr/local/bin/python3 tools/local_verify.py` |

`app.py` options:

| Option | Meaning |
| --- | --- |
| `--baseline PATH` | confirmed previous version's result export (required) |
| `--current PATH` | this build's structured test results (required) |
| `--format markdown\|json` | output format, default `markdown` |
| `--output PATH` | write to a file instead of stdout |
| `--exit-on-new-failure` | exit 1 when new failures exist (default: 0, since a summary still succeeded) |

Exit codes: `0` summary produced, `1` summary produced with `--exit-on-new-failure`
and new failures present, `2` unreadable/invalid input or write failure. The
per-run count line (`summary: new_failures=... fixed=... persistent=... unstable=...
not_executed=... needs_confirmation=...`) goes to stderr so it never corrupts
markdown/JSON on stdout.

## Input format

Both inputs are one UTF-8 JSON document describing exactly **one build**:

```json
{
  "build_id": "build-2026-11-14",
  "source": "current-build-export",
  "results": [
    {
      "sample_name": "attr-quote",
      "content": "<a href='x'>",
      "parse_mode": "lenient",
      "environment": "linux",
      "attempt": 1,
      "status": "failed",
      "failure_category": "parse:unterminated_attribute"
    }
  ]
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `build_id` | yes | one non-blank build identifier for the whole document |
| `source` | no | free-text provenance note shown in the header |
| `results` | yes | non-empty JSON array |
| `results[].sample_name` | yes | display name; may change between builds |
| `results[].content` | no | the sample's payload; drives rename-tolerant matching |
| `results[].content_hash` | no | `sha256:<hex>` or bare 64-hex digest; validated against `content` when both are present |
| `results[].parse_mode` | yes | comparison scope |
| `results[].environment` | yes | comparison scope |
| `results[].attempt` | yes | integer `>= 1` |
| `results[].status` | yes | `passed`, `failed` or `skipped` (case/whitespace insensitive) |
| `results[].failure_category` | failed only | required for `failed`, forbidden for other statuses |
| `results[].summary` | no | explicit display summary overriding content/name |

Unknown fields at any level are rejected rather than ignored, so a typo such as
`statuss` cannot silently turn a failed sample into a pass.

## Output semantics

Rows are grouped by **content identity + normalised parse mode + normalised
environment** (case and surrounding whitespace are normalised; spelling is
preserved for display). Only records in the same identity/mode/environment
group are compared.

Sample identity, in priority order:

1. `content_hash` (`sha256:` + digest),
2. `content` (SHA-256 of the payload),
3. `sample_name` only when neither is exported (legacy exports; marked
   `identity_basis: "name"`).

Because identity is content-based, an unchanged sample that was renamed still
matches its baseline entry; the row records both names and `renamed: true`.
Changing content under the same name creates a new sample, reported as a new
failure plus a missing baseline row.

Group outcomes:

| Group outcome | Rule |
| --- | --- |
| `passed` | every executed attempt passed |
| `failed` | at least one attempt failed and none passed |
| `unstable` | at least one pass **and** at least one fail (a single pass + a single fail counts) |
| `not_executed` | every attempt was `skipped` |

Consequences:

* `skipped` samples are counted under `not_executed`; stable passes are counted
  separately in `counts.stable_passes` and never include skip-only groups.
* A group with duplicate attempt numbers is emitted under
  `needs_confirmation` with `status: pending_confirmation`, no outcome, and the
  conflicting entries; it is never counted as new/fixed/persistent/unstable.
* A group whose content exists in the baseline only under another
  mode/environment is emitted under `scope_mismatch` and is not compared; the
  baseline scope is retained as `scope_evidence`. When the baseline group is the
  one that cannot be compared (its current counterpart was skipped or held for
  confirmation), it is reported from the baseline side with the current scope
  recorded in `current_scope_evidence`. Only when a current-side scope-mismatch
  row already describes the same content is the baseline row folded into it
  (`suppressed_groups`), so no content can disappear unaccounted.
* Groups absent from the current export appear under `missing_from_current`
  with `build_id: null`, `attempts: []`, `timeout: not_applicable`.

Timeout handling: `failure_category` values are compared as sets. A category
whose leading token is `timeout` (`timeout`, `timeout:30s`, `Timeout/Read`,
`timeout.5s`) belongs to the timeout family. Each failing row reports
`timeout` as one of:

| Value | Meaning |
| --- | --- |
| `new_timeout` | current group has a timeout, baseline did not |
| `pre_existing_timeout` | both current and baseline have a timeout |
| `timeout_resolved` | baseline had a timeout, current does not |
| `no_timeout` | neither side has a timeout |
| `not_applicable` | no current attempts (row is baseline-only) |

`new_failure_categories` lists categories present now but absent from the
baseline, so a newly appeared non-timeout error class inside a group that
already timed out is visible separately (see `mixed-timeout` in the fixtures).

Report sections, in order: `new_failures`, `fixed`, `persistent_failures`,
`unstable`, `not_executed`, `missing_from_current`, `scope_mismatch`,
`needs_confirmation`. Rows inside a section are sorted by parse mode,
environment, sample name and identity, so repeated runs are byte-stable.

### JSON output

`--format json` is the same structure the Markdown renderer consumes: a `baseline`
and `current` block (build id, source, file path, group/attempt counts),
`compared_groups`, `suppressed_groups`, `accounted_groups`, `counts`, and
`sections` keyed by section name. Each section row is an object with (at least)
`sample`, `summary`, `parse_mode`, `build_id`, `baseline_build_id`,
`environment`, `attempts`, `baseline_attempts`, `attempt_details`,
`failure_categories`, `baseline_failure_categories`, `new_failure_categories`,
`pre_existing_failure_categories`, `timeout`, `current_outcome`,
`baseline_outcome`, `scope_evidence`, `current_scope_evidence`, `renamed`,
`identity` and `identity_basis`. Field names are stable and the document is
sorted, so it can be diffed between runs.

## Delivered sample fixtures

`fixtures/baseline.json` (`base-2026-11-01`) and `fixtures/current.json`
(`build-2026-11-14`) are synthetic, offline exports built to exercise every
required rule:

| Case | Baseline | Current | Expected result |
| --- | --- | --- | --- |
| `brand-new` | absent | failed `parse:unknown_construct` | new failure |
| `flaky-net` | passed | failed `timeout:60s` | new failure, `new_timeout` |
| `latency-probe` | passed | failed `timeout:120s` | new failure, `new_timeout` |
| `skip-to-fail` | skipped | failed `parse:syntax` | new failure (baseline unexecuted) |
| `catastrophic-regex` | failed `parse:stack_overflow` | passed | fixed |
| `attr-quote`, `crlf-normalize`, `utf8-bom` | failed | failed same category | persistent |
| `mixed-timeout` | failed `timeout:30s` | failed `timeout:30s` + `parse:out_of_memory` | persistent; new category separated |
| `nested-comment` | pass + fail | pass + fail | unstable |
| `skip-me` | skipped | skipped | not executed (not a stable pass) |
| `stable-sample` | 2 passes | 2 passes | stable pass (counted, no row) |
| `old-name-sample` → `new-name-sample` | passed | passed, same content | matched by content, not new/missing |
| `dup-sample` | passed | attempt 1 twice (pass + fail) | needs confirmation, unclassified |
| `scope-test` | failed under `strict` | passed under `lenient` | scope mismatch, not compared |
| `removed-sample` | passed | absent | missing from current export |

Expected totals: new failures 4, fixed 1, persistent 4, unstable 1,
not executed 1, missing 1, scope mismatch 1, needs confirmation 1,
stable passes 2, newly passing 1 (13 compared groups).

## Verification

`tools/local_verify.py` (run with `/usr/local/bin/python3`) does three things:

1. Discovers the suite via `unittest.defaultTestLoader.discover("tests",
   top_level_dir=root)`, asserts `countTestCases() >= 1`, then **actually runs**
   it. It fails on zero tests, an all-skipped run, any failure/error, or a
   discovered-vs-run count mismatch — unittest's "0 tests => exit 0" is never
   treated as success. `tests/test_local_verify_guard.py` proves this guard by
   feeding it synthetic empty, all-skipped, failing and passing suites.
2. Runs the delivered sample/boundary assertions case by case (`C1`–`C12`),
   printing each case's input, expected value and actual value.
3. Prints a summary and exits 0 only when both parts are fully green.

Current result: 81 discovered and executed tests, 0 skipped, 0 failures,
0 errors; 18/18 delivered checks pass; runtime ~0.3 s.

### Coverage map (requirement → test / sample case)

| Requirement | Where it is exercised |
| --- | --- |
| new failures | `C1`, `test_new_failures_are_named`, `test_all_attempts_failed_is_failed_not_unstable` |
| fixed | `C2`, `test_fixed_is_named`, `test_rename_with_unchanged_content_matches_baseline` |
| persistent failures | `C3`, `test_persistent_are_named`, `test_failure_from_unstable_baseline_counts_as_persistent` |
| unstable (single pass + single fail) | `C4`/`C4b`, `test_single_pass_and_single_fail_is_unstable` |
| not executed vs stable pass | `C5`/`C5b`, `test_not_executed_is_counted_separately_from_stable_pass` |
| comparable only in same mode + environment | `C8`/`C8b`, `ComparabilityRuleTests` (incl. skipped/conflict counterexamples) |
| rename matched to original baseline | `C6`, `test_rename_matches_original_baseline_entry` |
| duplicate attempt conflicts held | `C7`/`C7b`, `ConflictRuleTests` |
| row fields (summary/mode/build/env/attempt/category) | `C10`, `test_all_rows_carry_required_fields` |
| pre-existing timeout vs new error class | `C9`/`C9b`, `TimeoutRuleTests` |
| local result entry point | `C11`, `CliTests` |
| rejecting ambiguous input | `C12`, `RejectedExportTests` |
| verifier refuses empty/all-skipped suites | `test_local_verify_guard.py` |

Group accounting is enforced by `AccountingInvariantTests`: emitted rows plus
stable passes plus newly passing plus suppressed baseline rows must equal every
group present in either export (18/18 for the delivered fixtures).

## Limitations

* Direction of comparison is fixed: baseline is "before", current is "after".
* Builds must be exported as separate documents; a single mixed-build file is
  rejected.
* Content identity is exact (byte-level SHA-256). Whitespace- or
  encoding-insensitive sample identity is not implemented, so a sample whose
  content differs only in insignificant ways is treated as a new sample.
* Duplicate attempt conflicts are reported, never auto-resolved.
* The wrapper only covers the delivered fixtures and unit suite; it does not
  integrate with any real CI system, remote service or database. Fixtures are
  synthetic and hand-authored, not captured from a live pipeline.
