# Streaming median window operator (Python 3.11.6, stdlib only)

A push-based window operator for partitioned, time-ordered records. Each output
row keeps the input row number and order. The operator is forward-only and its
resident row state is bounded by the rows of the active window; values are held
in a treap so a single insert/removal never re-sorts the window, and structural
churn (insertions / deletions / rotations / probes) is reported to the caller.

## Semantics

For every input record `(partition, time=t, value)`:

* candidate rows are those in the **same partition** with time in the closed
  window `[t - W, t]`;
* **all rows whose time equals `t` are removed** from the candidate set, so the
  reported median is over values with `t - W <= time <= t - 1`;
* non-null numeric values only; `NULL` values occupy a row and produce an
  output row, but never contribute to a median;
* an odd count returns the middle value, an even count the arithmetic mean of
  the two middle values, and an **empty window returns `NULL`** (`None`);
* crossing into another partition discards all state (window restarts);
* every output carries the 1-based global input row number, so order and
  numbering survive batching.

Because a row at time `t` never sees another row at time `t`, the answer for a
given row does not depend on how a batch boundary splits the equal-time group.
Two batches that share time `t` and contain equal values therefore produce
identical output no matter where they are cut. This is tested exhaustively.

## Public API

Package: `median_window` (importable from the workspace root).

### `MedianWindow(window, seed=0, retain_results=False, contiguous_partitions=False)`

* `window` – `W >= 0`, integer. Rejects non-int / negative / bool.
* `seed` – treap priority seed; affects only tree shape and churn counters,
  never a reported median.
* `retain_results` – when true the operator remembers emitted `WindowRow`s in
  `.results`. Default `False` keeps the operator's own memory bounded by the
  active window (send results to your sink instead).
* `contiguous_partitions` – default `False`: every partition change restarts.
  `True`: strict mode, a partition key that reappears after being closed raises
  `OrderingError`. Strict mode stores one key per partition, never rows.

Methods and attributes:

| member | meaning |
| --- | --- |
| `push(partition, time, value) -> WindowRow` | consume one record; returns its output row immediately |
| `push_many(records) -> list[WindowRow]` | consume `(partition, time, value)` tuples/lists |
| `push_batches(batches) -> list[WindowRow]` | consume an iterable of batches; boundaries may fall anywhere |
| `finish() -> tuple[WindowRow, ...]` | close the stream (idempotent); returns retained history |
| `stats() -> WindowStats` | snapshot of state and structural churn |
| `results` | tuple of retained rows (empty unless `retain_results=True`) |
| `row_number`, `active_size`, `pending_size`, `non_null_size`, `window` | counters |

`WindowRow` (frozen dataclass): `row_number` (1-based global arrival index),
`partition`, `time`, `value`, `median`.

`WindowStats` (NamedTuple): `rows_in`, `rows_out`, `insertions`, `deletions`,
`rotations`, `merge_steps`, `probes`, `kth_queries`, `active_size`,
`pending_size`, `resident_rows`, `max_resident`, `non_null_size`,
`partitions_seen`, `rebuilds` (always `0`), `max_window_rows`.

Errors: `WindowError(ValueError)` for bad configuration/records;
`OrderingError(WindowError)` for a time that moves backwards within a partition
(or a closed partition returning in strict mode).

### Module-level helpers

* `run_stream(records, window, seed=0) -> list[WindowRow]` – convenience wrapper
  that retains history.
* `iter_medians(records, window, seed=0)` – lazy generator of medians only.
* `reference_output(records, window) -> list` – brute-force O(n·w) reference
  used by the tests; not on any hot path.

### `OrderedMultiset` (`median_window.treap`)

Order-statistic multiset with `add`, `discard`, `clear`, `kth`, `median`,
`size`, and observable `insertions`, `deletions`, `rotations`, `merge_steps`,
`probes`, `kth_queries`. Duplicate values share a node with a multiplicity
count. All operations are expected O(log n) and touch one root-to-leaf path.

### CLI

```bash
python3 -m median_window --window 2 samples/tiny.jsonl
python3 -m median_window --window 2 --stats samples/tiny.jsonl
```

Input: JSONL, each line `{"partition":..., "time":..., "value":...}` or a
3-element array. Output: one JSON object per row in input order with keys
`row`, `partition`, `time`, `value`, `median` (`null` for NULL). `--stats`
writes a JSON summary to stderr. Input errors exit with status 2.

## Algorithm and state bound

* Rows are grouped by timestamp. Rows of the *current* timestamp sit in a small
  `pending` list; they are flushed into the ordered structure only when a larger
  timestamp arrives (they can never be in their own window).
* On every timestamp advance, rows older than `t - W` are dropped **before**
  they ever enter the structure, and older rows already in it are removed.
* Therefore `resident_rows = |active| + |pending| <=` number of input rows whose
  time lies in `[t - W, t]`, and the ordered structure holds at most the
  non-null values of that window (`max_window_rows` records the high-water mark).
* Each insert/removal is one treap path descent plus at most O(log n) rotations;
  `rebuilds` stays 0 and no operation sorts or scans the whole window.

## Reproduction

Requires exactly Python 3.11.6, standard library only. Run from the workspace
root:

```bash
python3 --version                              # Python 3.11.6
python3 -m unittest                            # discovers tests/test_*.py
python3 -m unittest discover -s tests -t . -v  # same, explicit
python3 demo.py                                # live synthetic demo + self-check
python3 -m median_window --window 2 samples/tiny.jsonl
```

`tests/__init__.py` is present so `tests` is an importable package and default
discovery from the workspace root finds `tests/test_*.py`.

## Verification scope

Tests are in `tests/test_window.py`, `tests/test_treap.py`, `tests/test_cli.py`,
`tests/test_demo.py` (the last also wires the module doctests in). Coverage by
category:

* **Normal** – hand-computed odd/even medians, duplicates in the window,
  negative times and values, float values, multi-partition runs keeping row
  numbers and order.
* **Boundary** – empty window, `W = 0`, the inclusive edge `t - W`, eviction
  just past the edge, all rows sharing one timestamp, partition reset, invalid
  window/record/seed types, NaN rejection, NULL rows not counted, push after
  `finish`, malformed batch tuples, partition-reappearance rules.
* **Interleaved** – batch split at every possible position of a stream including
  inside equal-time duplicate groups, many partitions, randomized cross-check of
  the operator (two seeds) against `reference_output`, single-row batches,
  resident-row equality with the hand-maintained active window at every push,
  and churn checks (no rebuilds, one path per operation).

Run results (this workspace): `python3 -m unittest` → **53 tests, all pass**.
`python3 demo.py` on its default synthetic stream (6,000 rows, 3 partitions,
`W = 4`) prints `streaming == ref : True`, `batch splits agree: True`, and
`split inside equal-time run ... : True`.

Observation limits (not claimed): the tests validate correctness against a
brute-force reference and check structural counters as proxies for the
path-bounded work; they do not benchmark wall-clock latency, and no network,
external data, or non-synthetic dataset was used. Numbers in this README come
from real runs above; anything not executed is not asserted.
