# row_expand — demand-driven nested list expansion

`row_expand` is a pure-Python (3.11.6) pull-based operator that expands one
input row into the cross product of several possibly-null list sources. It is
the vector-executor primitive for a chain of `LATERAL` unnest/`generate_series`
levels where a later level may depend on the current element of an earlier one.

* **Inner** levels drop a branch when their list is `NULL`/empty.
* **Outer** levels keep exactly one padded branch for a `NULL`/empty list.
* Each level has its **own ordinal column**.
* Rows are produced **on demand** (`next_row` / `next_batch`); nothing is
  materialised up front.
* A **cursor** can be captured after any emitted row and resumed later against
  the same input snapshot, without rebuilding the Cartesian product.

## Running the tests

```sh
python3 -m unittest discover -s tests -v
```

No third-party dependencies are used.

## Public API

Everything below is exported from `row_expand`:

```python
from row_expand import (
    Level, RowExpander, RowCursor, Snapshot, Cursor, CursorError, PADDED_ORDINAL,
)
```

### `Level`

```python
Level(
    name,                 # output column holding this level's current element
    source,               # column name (str) or callable (row, bindings) -> iterable | None
    mode="inner",         # "inner" or "outer"
    ordinal_name=None,    # defaults to f"{name}_ordinal"
    ordinal_start=1,      # ordinal of the first element
)
```

* `source` as a **string** names an input column whose value must be a
  re-iterable list (or a re-iterable iterable / `None`). A one-shot iterator in
  a column is rejected, because a cursor could not be replayed.
* `source` as a **callable** receives `(row, bindings)` and returns an iterable
  or `None`. `bindings` maps every *shallower* level name to that level's
  current element, so a later level can reference an earlier one. The callable
  is re-invoked on every pass, so it must be reproducible (returning a fresh
  generator each call is fine).
* `mode="inner"`: no elements → the branch is dropped.
* `mode="outer"`: no elements → one branch with the element bound to `None`.

### `RowExpander`

```python
exp = RowExpander(levels)          # list/tuple of Level (or equivalent mappings)
cursor = exp.expand(rows)          # fresh cursor from the start
cursor = exp.expand(rows, saved)   # resume after a saved Cursor
cursor = exp.resume(rows, saved)   # alias for expand(rows, saved)
exp.levels                         # tuple[Level, ...]
exp.value_columns                  # tuple of element column names
exp.ordinal_columns                # tuple of ordinal column names
```

`rows` may be a factory callable returning a fresh iterator, a re-iterable
container, or a `Snapshot`.

### `RowCursor`

```python
cur.next_row()      -> dict            # raises StopIteration when exhausted
cur.next_batch(n)   -> list[dict]      # up to n rows (exactly n unless done)
cur.cursor()        -> Cursor          # boundary AFTER the last emitted row
cur.close()                            # release live source generators
cur.emitted, cur.done                  # progress introspection
next(cur)           -> dict            # iterator protocol
```

Every output row is a new `dict` that copies the input row's columns and adds,
per level, `level.name` and `level.ordinal_name`.

### `Cursor` and `Snapshot`

* `Cursor(row_index, positions, done, snapshot_token)` is a frozen value
  object. `positions is None` means "before the first row of `row_index`";
  otherwise it holds one entry per level: `-1` = outer-padded branch,
  `>= 0` = zero-based element position.
* `Snapshot(rows, token=None)` wraps a re-iterable source or factory and
  reports its `token`. If both cursors and snapshots carry a token, resuming a
  cursor against a different snapshot raises `CursorError`.

`CursorError` (a `ValueError`) is raised for cursors that do not fit the
snapshot or the expander's level shape.

## Semantics

For each input row the levels form an odometer, outermost level slowest:

```
FROM t
LEFT JOIN LATERAL <level 0> AS l0 ON TRUE
LEFT JOIN LATERAL <level 1> AS l1 ON TRUE   -- bindings: l0
...
```

A deeper level is refilled eagerly only when a shallower level moves, and its
element iterator is pulled lazily one item at a time — so the product is never
materialised. When an inner level is empty for every sibling of an outer level,
the whole input row produces no output (which necessarily requires inspecting
the outer elements, but still yields no Cartesian rows).

### Ordinals

| situation                    | element | ordinal                    |
|------------------------------|---------|----------------------------|
| real element at position `i` | value   | `ordinal_start + i` (int)  |
| outer-padded branch          | `None`  | `PADDED_ORDINAL` (`None`)  |

Therefore a genuine `None` *inside* a list (integer ordinal `>= ordinal_start`)
is distinguishable from the `None` synthesised by an outer level (`None`
ordinal). Two positions holding the **same value** always receive distinct
ordinals and are expanded independently.

## Resource & laziness guarantees

* Opening a cursor pulls nothing.
* `next_batch(k)` pulls only enough source items to satisfy `k` rows.
* `cursor()` is O(depth): it records per-level positions and input row index.
* Resuming replays positionally from the snapshot start: input rows are
  re-iterated to the saved row index and each level is advanced to its saved
  position, so cost is bounded by the saved position, not by the full product.
* `close()` closes any live source iterators/generators.

## Verification scope

The suite (`tests/test_row_expand.py`, 56 tests) covers:

* inner vs outer semantics, including real-`None` vs padded-`None` distinction;
* lateral generators that read a previous level's current element;
* identical values at different positions expanded independently, and across
  input rows without sharing iterators;
* custom `ordinal_name` / `ordinal_start`;
* fixed-size batching that reassembles the full stream;
* a cursor captured at **every** output boundary and resumed to reproduce the
  exact remaining stream (mid-row, after a padded branch, across input-row
  boundaries, start and end);
* a randomized differential test (60 random level trees, each resumed at every
  boundary) cross-checked against an independent eager reference model;
* laziness: a 1,000,000-item source yields the first three rows while pulling
  only three items; a 10¹² mental product is not materialised; a cursor
  capture is O(depth);
* validation errors (bad modes, duplicate/colliding names, missing columns,
  one-shot iterators, non-mapping rows, mismatched cursor shapes/tokens).

Evidence for the laziness claims comes from counters attached to the synthetic
sources, asserted in `TestLaziness`. The "million-item list, first three rows"
scenario is exercised exactly as described in the task.
