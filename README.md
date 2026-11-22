# merge_store — deterministic in-memory MERGE for relation stores

A small, dependency-free Python 3.11 library that executes a SQL-like `MERGE`
statement against an in-memory relation store where every target row carries a
**stable row id**.

The central guarantee, from the task statement:

> One source record first rewrites a target row's match key; later source
> records must still find that row **by the original table**.

## Why it exists

A naive `MERGE` re-indexes the target between source rows, so once an early
source row rewrites the column another row is matched on, later source rows
silently re-bind to a different object (or miss it entirely). This library
makes the binding rule explicit and verifiable:

* **Snapshot matching.** Every source row is matched against the target table
  *as of statement start*. A row keeps its stable `row_id` even after an
  earlier source row rewrites the columns used to match it.
* **Ordinary equality, NULL-aware.** A comparison where either side of any
  match column is `NULL` (Python `None`) is **not** a match — not even
  `NULL = NULL`.
* **Ordered rules.** Update / delete / insert / skip rules are declared in
  order; the first rule whose condition side (matched / not matched) and
  predicate apply wins, per source row.
* **Whole-statement rejection.** If more than one source row would *truly*
  update or delete the same target row, the entire statement is rejected and
  the table is left untouched. Source rows diverted by a **skip** rule do not
  count toward that conflict and do **not** fall through to an insert.
* **Validate-then-apply.** All actions are applied to a projection which must
  pass a final uniqueness check on the match columns before anything commits.
  On any conflict nothing commits.
* **Inspectable results.** Changes are returned with before/after values,
  ordered by source order then row id. Both the statement-start match and the
  final-key validation can be checked from the returned outcome or the
  conflict locations.

## Requirements

* Python **3.11.6** (standard library only; no third-party packages).
* No network, credentials, or external services are used.

## Layout

```
merge_store/
  __init__.py    public API re-exports, __version__
  model.py       TargetRow, TargetTable, Rule, Match, Change, Conflict,
                 MergeOutcome, error types
  engine.py      execute_merge, MergeStatement, RelationStore, default_insert_row_id
tests/
  support.py     shared imports / small helpers
  test_normal.py         happy-path update/insert/delete, store wrapper
  test_boundaries.py     NULL semantics, empty inputs, composite keys, validation
  test_interleaved.py    snapshot binding, multiplicity, final-key, atomicity,
                         ordering  (the acceptance-critical scenarios)
  test_invariants.py     randomized snapshot invariant, determinism, larger batch
```

## Public API

### Data types (`merge_store.model`)

* `TargetRow(row_id, values)` — stable `row_id` (hashable, non-`None`) plus a
  column mapping. `.key(columns)` returns the match-key tuple (missing column →
  `None`).
* `TargetTable(rows)` — ordered, immutable. Rows may be `TargetRow`s or
  `(row_id, values)` pairs; row ids must be unique.
  Helpers: `.as_dict()`, `.row_ids`, `.row(id)`, `.key_of(id, columns)`,
  `.from_pairs(...)`, `len()`, iteration.
* `Rule(action, when=None, assign=None, predicate=None, name="")`
  * `action ∈ {"update", "delete", "insert", "skip"}`
  * `when ∈ {"matched", "not_matched"}`; defaults to matched for update/delete
    and not_matched for insert. **Required** for `skip`.
  * `assign` (update/insert): a `{column: value}` mapping, or a callable
    `(source_values, target_values) -> mapping`; individual mapping values may
    be callables `(source, target) -> value`. For `insert`, `target_values` is
    always `None` and an omitted `assign` copies the source row.
  * `predicate`: optional `(source_values, target_values_or_None) -> bool`.
* `Match` — per source row: `source_index`, `source_key`, `matched`,
  `target_row_id`, `target_key`, `target_values_before`, `rule_index`,
  `rule_name`, `rule_action`, `effective_action`, `reason`,
  `candidate_row_ids`. (`effective_action` may be `"none"` when no rule
  applies, or `"error"` for ambiguous binding — see conflicts.)
* `Change` — one applied action: `source_index`, `action`, `target_row_id`,
  `before`, `after`, `key_before`, `key_after`, `source_values`.
* `Conflict` — `kind`, `message`, `key`, `row_ids`, `source_indices`,
  `details`. Kinds: `multiple_modifying_source_rows`, `duplicate_final_key`,
  `duplicate_target_key`.
* `MergeOutcome` — `committed`, `target` (committed table, or the untouched
  input on rejection), `projected` (the would-be table, always available),
  `changes` (only when committed), `matches`, `conflicts`. Helpers:
  `.ok`, `.change_for(i)`, `.match_for(i)`, `.conflict_kinds()`.

### Errors

* `MergeError(ValueError)` — base.
* `MergeConfigError(MergeError)` — malformed statement/input (bad rule
  combination, duplicate row ids, unhashable match key, non-mapping source
  row, ...). Raised as an exception, never silently ignored.
* `MergeConflictError(MergeError)` — only when `raise_on_conflict=True`; carries
  `.outcome`.

### Functions / classes

```python
execute_merge(source_rows, target, match_columns, rules, *,
              insert_row_id=None, raise_on_conflict=False) -> MergeOutcome

MergeStatement(match_columns, rules).execute(target, source_rows, *,
              insert_row_id=None, raise_on_conflict=False) -> MergeOutcome

RelationStore(table).merge(source_rows, match_columns, rules, *,
              insert_row_id=None, raise_on_conflict=False) -> MergeOutcome
```

`insert_row_id(source_index, values) -> row_id` is an optional factory for
newly inserted rows. The default is deterministic
(`("merge_insert", source_index)`) and collision-avoiding: if the produced id is
already taken it is suffixed (`(id, 1)`, `(id, 2)`, ...).

`RelationStore.merge` commits atomically: it replaces its table only when the
outcome committed; otherwise the table is byte-for-byte unchanged.

## Semantics in detail

1. **Statement-start snapshot.** The target is indexed once from the input
   `TargetTable`. Source rows are all matched against that index. A source row
   inserted by this statement never joins the lookup, so a later source row
   cannot match a row created earlier in the same statement.
2. **NULL keys.** Any match key containing `None` is never indexable and never
   matches; such source rows go to the `not_matched` side
   (`reason="null_key_never_matches"`). A missing column counts as `None`.
   NULL keys also do not participate in final uniqueness (mirroring SQL
   `UNIQUE`, where NULLs are distinct).
3. **Ambiguity.** If a source key matches *more than one* target row (a
   pre-existing duplicate), that is a `duplicate_target_key` conflict and the
   statement is rejected. Duplicates on keys the statement never binds are not
   reported.
4. **Multiplicity.** Counting per target row over source rows whose
   *effective action* is update or delete. `> 1` ⇒ one
   `multiple_modifying_source_rows` conflict per row, listing every source
   index and the actions. Skip rows are excluded, and skip never becomes an
   insert.
5. **Final uniqueness.** After applying all uncontested actions to a
   projection, the match columns must be unique across the projected table for
   any key group that the statement touched. A clash is a `duplicate_final_key`
   conflict naming every row in the group (including untouched rows sharing the
   key) and the source indices responsible.
6. **Ordering.** `changes` are sorted by `source_index` then a total order over
   `target_row_id` (works across heterogeneous id types). `conflicts` are
   sorted deterministically.

## Reproduce / run the tests

From the workspace root:

```bash
# Full suite (either invocation works)
python3 -m unittest discover -s tests -t .
python3 -m unittest discover

# A single module, verbose
python3 -m unittest tests.test_interleaved -v
```

Expected: all tests pass (`Ran 55 tests ... OK` on the snapshot above).

## What the tests verify

* **Normal path** (`test_normal.py`): update matched + insert unmatched; mixed
  update/delete/insert with ordered predicates; before/after values; deterministic
  and collision-safe insert ids; store commits only on success.
* **Boundaries** (`test_boundaries.py`): `NULL` source key never matches a
  `NULL` target row; NULL in a composite key; missing column ⇒ NULL; empty
  source / empty target; composite-key exact matching; rule validation
  (update without assign, delete with assign, wrong side, skip without side,
  unknown action/when, empty/duplicate match columns, bad row ids, unhashable
  keys, non-mapping source rows); no applicable rule ⇒ `"none"`; skip leaves
  the row alone and does not insert.
* **Interleaved / acceptance** (`test_interleaved.py`):
  * the first source row rewrites a match key and a later source row still
    binds the **same** original row (reported by `source_key`/`target_key` and
    the shared `target_row_id`), and the double modification rejects the whole
    statement with the table untouched;
  * a source row on the *new* key does not see the rename (it is not matched);
  * an inserted row does not join the lookup for later rows;
  * two updates / two deletes / update+delete on one target reject; two skips
    beside one true update do **not** conflict; distinct targets do not;
  * final-key clashes from renames and from colliding inserts are rejected and
    located; a delete in the same statement can legitimately resolve a would-be
    clash;
  * `raise_on_conflict` carries the full outcome; rejected statements leave the
    store unchanged; changes ordered by source order (then row id).
* **Invariants** (`test_invariants.py`): a randomized check that every matched
  row bound its statement-start key; duplicate target keys are flagged rather
  than silently picked; untouched duplicate keys do not block unrelated
  statements; repeated runs are identical; a larger mixed batch produces the
  expected committed state and action log.

## Verification scope and limitations

* All tests above were **actually executed** in this workspace on Python
  3.11.6 via `unittest`; results are reported as run, not pre-generated.
* Verified: the scenarios listed above, including the fault-recovery /
  boundary / interleaving cases the task calls out (snapshot binding after a key
  rewrite, NULL non-matching, multi-modifier rejection with rollback, skip
  exclusion, final-key validation).
* **Not** verified / out of scope: persistence, concurrency across threads or
  processes (the engine is single-threaded and in-memory), SQL parsing or a
  textual `MERGE` dialect, and performance at scale. `Expression`/predicate
  semantics are whatever the supplied Python callables do.
