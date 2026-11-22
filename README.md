# SLI Adjudicator — windowed release gating

A pure-Python 3.11 module that turns a stream of noisy request observations into
a per-window **release verdict** (`allow` / `reject` / `wait`) using
lower/upper bounds on the *bad-request ratio*.

One user request may be seen many times — at the edge entry, downstream, in the
receipt stream, and on retries — but it must contribute **exactly one sample** to
the denominator.

## Model in one paragraph

Each request carries a request id, a **first-entry time**, a logical attempt
number and an observation version. A request is placed into the fixed-size
window that contains its **first-entry** time and judged with the **rule that was
in force at that entry time** (allowed latency, whether a final receipt is
required, and the user deadline). Every repeated sighting of the same request is
folded into one sample: within the *valid (highest) version* a final receipt
outranks a partial success message; a request whose result is not confirmed
before its user deadline is **pending** (未决). Denominating over **all in-window
requests**, the bad-ratio is bounded by treating every pending sample as a
success (lower bound) or as a failure (upper bound):

| condition                   | verdict          |
| --------------------------- | ---------------- |
| no samples                  | `wait` (等证据)  |
| upper bound ≤ threshold     | `allow`  (可放)  |
| lower bound > threshold     | `reject` (拒绝)  |
| otherwise                   | `wait`   (等证据)|

Late corrections may adjust the success / failure / pending counts and leave an
incremental audit-log entry **as long as the window has not been sealed**. Once
sealed, a window keeps its original verdict forever; later corrections are
recorded but not applied.

## Public API

Everything below is importable directly from `sli_adjudicator`:

```python
from sli_adjudicator import (
    Adjudicator, Rule, Observation, Correction,
    evaluate, compute_bounds, to_fraction,
    Counts, Decision, LogEntry, Bounds,
    VERDICT_ALLOW, VERDICT_REJECT, VERDICT_WAIT,
    OUTCOME_SUCCESS, OUTCOME_FAILURE, OUTCOME_PENDING,
    STATUS_SUCCESS, STATUS_FAILURE,
    STREAM_EDGE, STREAM_DOWNSTREAM, STREAM_RECEIPT,
)
```

### `Rule(effective_from, allowed_latency_ms, requires_receipt, user_deadline_ms)`

Immutable. Selected per request by comparing `effective_from` with the request's
**entry** time (inclusive lower bound; latest rule ≤ entry wins).

### `Observation(request_id, entry_time, attempt, version, stream, status, …)`

One sighting. Optional fields: `latency_ms` (falls back to
`(observed_at − entry_time)·1000`), `receipt_present`, `observed_at` (defaults to
`entry_time`, must not precede it). `stream ∈ {edge, downstream, receipt}`,
`status ∈ {success, failure}`.

### `Correction(request_id, entry_time, attempt, version, status, …)`

A late, authoritative repair. Optional `result_at` records when the truth became
confirmable: if it falls after the entry rule's user deadline the sample is
demoted to *pending*. When `result_at` is omitted the correction is taken as
authoritative and the deadline gate is not applied.

### `Adjudicator(rules, window_size_s, threshold, allowed_lateness_s=0.0, auto_seal=True)`

| method                                   | purpose                                                      |
| ---------------------------------------- | ------------------------------------------------------------ |
| `ingest(record) -> list[LogEntry]`       | ingest one `Observation`/`Correction`; returns appended log   |
| `ingest_many(records)`                   | ingest an iterable                                            |
| `decision(wid) -> Decision`              | current decision for a window id                              |
| `decision_for_time(entry_time)`          | decision for the window containing `entry_time`              |
| `decisions() -> list[Decision]`          | all known windows, ordered                                    |
| `rule_at(entry_time) -> Rule`            | rule in force at an entry time                                |
| `window_of(entry_time) -> (wid, start, end)` | window geometry                                          |
| `request_outcome(request_id) -> str?`     | `success`/`failure`/`pending`, or `None`                      |
| `advance_watermark(t)` / `seal_window(wid)` | drive or force sealing                                    |
| `log()` / `timeline(request_id)` / `seal_entries(wid?)` | audit trail                                 |
| `threshold`, `window_size_s`, `watermark` | read-only config                                            |

`threshold` may be an `int`, a `Fraction`, an exact numeric `string`
(`"0.05"`, `"1/20"`) or a `float`. **Prefer a string/Fraction at a decision
boundary** — a `float` such as `0.1` is the exact binary value `0.1…`, not
`1/10`. `to_fraction` documents this.

`Decision` exposes `verdict`, `lower_bound`, `upper_bound`, `total`, `success`,
`failure`, `pending`, `threshold`, `sealed`, `window_id/start/end`, and
`has_samples`. `LogEntry` is the incremental delta: `cause`
(`observation`/`correction`/`seal`), `applied`, `reason`,
`prev_outcome`/`new_outcome`, `delta_success/failure/pending`, `counts`,
`lower_bound`/`upper_bound`, `verdict_before/after`, `event_time`, `watermark`.

### Free functions

* `compute_bounds(success, failure, pending) -> Bounds` — exact `Fraction` bounds.
* `evaluate(success, failure, pending, threshold) -> (verdict, Bounds)`.

## The replay: failure → retry success → late correction

`python -m sli_adjudicator` (or `--csv`) runs `sli_adjudicator.scenarios.
replay_failure_retry_correction()`, which builds a real `Adjudicator` and reads
every value back from it. Configuration: window size 10 s, target window
`[10, 20)`, threshold `0.05`, allowed lateness 3 s (window seals when the
watermark reaches 23.0), rule = allowed 500 ms / receipt required / deadline 4 s.
Nine confirmed successes already sit in the window, so the target request is the
only lever.

| event time | what happens                                        | sample        | bounds          | verdict  |
| ---------: | --------------------------------------------------- | ------------- | --------------- | -------- |
| 12.2       | edge reports **failure** for `req-42`               | failure       | [0.100, 0.100]  | `reject` |
| 13.0       | retry (attempt 1) succeeds in 200 ms, **no receipt** | pending       | [0.000, 0.100]  | `wait`   |
| 20.5       | **late correction** confirms timely success + receipt| success       | [0.000, 0.000]  | `allow`  |
| 24.0       | window-2 traffic pushes watermark past 23 → **seal** | success       | [0.000, 0.000]  | `allow`  |
| 24.5       | further correction for the sealed window             | not applied   | [0.000, 0.000]  | `allow`  |

So the request lands in **window 1** (`[10, 20)`) and its verdict flips at three
distinct moments: `reject` at event time **12.2**, `wait` at **13.0**, and
`allow` at **20.5** — before the window seals at event time **24.0**. The last
correction is recorded with `applied=False, reason="window sealed"` and does not
change the sealed verdict.

## Reproduce

From the workspace root (Python 3.11.6, standard library only, no installs):

```sh
# full test suite
python3 -m unittest discover -s tests -t . -v

# the live replay (text or CSV)
python3 -m sli_adjudicator
python3 -m sli_adjudicator --csv
```

## Verification scope

The `tests/` suite (89 tests, `tests/test_*.py`, discoverable from the workspace
root) covers:

* **Normal** — bound arithmetic, window assignment, rule-at-entry, classification,
  correction application, verdict transitions.
* **Boundary** — empty window → `wait`; upper `==` threshold → `allow`;
  lower `==` threshold → `wait`; lower `>` threshold → `reject`; exact latency
  `==` allowed; observation exactly at the user deadline; thresholds `0` and `1`;
  exact-vs-float decimal thresholds.
* **Interleaved / out-of-order** — the same window assembled from random
  permutations of edge/downstream/receipt/retry records yields identical counts
  and log deltas (seeded, 25 shuffles); later-versioned records arriving first.
* **Fault recovery** — a late correction adjusts an open window; a correction
  after sealing is logged with `applied=False` and the sealed verdict is
  retained even when it would otherwise flip.

**Not covered:** real network ingestion, persistence/durability of the log across
processes, clock-skew handling, and very large window/rule sets (no performance
benchmarks were run). Those are outside this task's synthetic-only scope.
