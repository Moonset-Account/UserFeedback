# Consistent Query Answering over Minimum-Cost Deletion Repairs

A self-contained Python 3.11 module that, given a small inconsistent database,
answers queries in a way that holds across **every minimum-cost repair**.

- **Input**: a few small tables with stable row ids and *positive* deletion
  costs, composite unique keys, mandatory and optional foreign keys, and
  explicit denial rules ("these two rows may not coexist").
- **Repair**: delete input rows only.  First make every constraint hold, then
  minimise total deletion cost.
- **Query**: an equality-join with a projection, evaluated under **set**
  semantics.
- **Output**: each derivable projection tuple is classified as
  - `certain`    — derived in *every* minimum-cost repair,
  - `contingent` — derived in some but not all,
  - `absent`     — derived in none,
  together with a witness for the claim:
  - `support`   — an optimal repair that *does* derive the tuple, plus one
    derivation inside it,
  - `exclusion` — an optimal repair that does *not* derive the tuple
    (`certain` tuples have none).

The module also exposes per-row status (`required` / `possible` / `never`) and
lets the caller **pin a fact** (a row must survive, or must be deleted) and
re-ask.

## Why this is more than "run the query on each repair"

Components are solved independently, but

1. a query may **join across components**, and
2. one projected tuple may have **many derivations**.

So a tuple can be `certain` even though *no single derivation* survives every
repair — different optimal repairs reach the same tuple through different rows.
The engine therefore decides certainty over the Cartesian product of component
repairs, tracking which derivations are still alive, rather than checking a
single derivation.

The headline example (`consistent_query/examples.py`,
`conflicting_groups_problem`) is exactly this: two key groups `L` and `R`, each
with two rows sharing `k = 1`. Every optimal repair deletes one `L` row and one
`R` row (cost 2), so **no row is required**. Yet the query

```sql
SELECT L.label, R.code FROM L, R WHERE L.k = R.k
```

returns `('L', 'C')` in **all four** optimal repairs — `certain` — using a
different `(L, R)` pair in each. Projecting `L.v, R.v` instead yields four
`contingent` tuples.

## Model semantics

- **Row ids** are stable strings, unique within a table; the pair
  `RowKey(table, rid)` identifies a row.  Costs must be positive integers.
- **Unique keys** (single or composite) forbid two rows with equal key values.
  Key columns may not contain `None` (rejected when the table is built).
- **Foreign keys** target a declared unique key of the parent table.
  - *Mandatory*: every child value must be non-null and match a surviving
    parent.
  - *Optional*: follows SQL `MATCH SIMPLE` — if any child column is `None` the
    row is unconstrained; otherwise a matching surviving parent is required.
  - Because the input may hold several parent rows with the same key value, a
    foreign key is satisfied when **at least one** matching parent survives (any
    valid repair keeps at most one anyway).
- **Denial rules** are a conjunction of column equalities between a row of the
  left table and a row of the right table; equal values (including
  `None == None`) mean the pair may not coexist.  For a rule whose two sides
  name the same table, only pairs of *distinct* rows are compared.
- **Query**: `tables` (each joined at most once), a conjunction of column
  equalities, and an ordered projection.  `None` never joins (SQL-style);
  output is a set of tuples, so duplicates collapse.

## Public API

```python
from consistent_query import (
    Row, Table, ForeignKey, Denial, Problem, Query, RowKey,
    analyse, keep_row, delete_row, Limits,
    RepairLimitExceeded, InfeasiblePins, ModelError,
    oracle, is_valid_repair, repair_cost,
)
```

Constructing an instance:

```python
L = Table(name="L", columns=("k", "label", "v"),
          keys=(("k",),),
          rows=(Row("l1", {"k": 1, "label": "L", "v": "x"}),
                Row("l2", {"k": 1, "label": "L", "v": "y"})))
R = Table(name="R", columns=("k", "code", "v"),
          keys=(("k",),),
          rows=(Row("r1", {"k": 1, "code": "C", "v": "x"}),
                Row("r2", {"k": 1, "code": "C", "v": "y"})))
problem = Problem(tables=(L, R))
query = Query(tables=("L", "R"),
              equalities=((("L", "k"), ("R", "k")),),
              projection=(("L", "label"), ("R", "code")))
report = analyse(problem, query)
```

`analyse(problem, query, pins=(), limits=None)` returns an `AnalysisReport`
with:

| attribute / method        | meaning                                                      |
|---------------------------|--------------------------------------------------------------|
| `optimal_cost`            | minimum total deletion cost                                  |
| `optimal_repair_count`    | number of distinct optimal repairs                           |
| `components`              | per-component report (cost, all optimal repairs, rows)       |
| `candidates`              | one `CandidateVerdict` per derivable tuple                   |
| `certain_answers` / `contingent_answers` / `absent_answers` | tuples by status |
| `required_rows` / `possible_rows` | `RowKey` sets: in every / at least one optimal repair |
| `row_status(key)`         | `"required"`, `"possible"` or `"never"`                      |
| `verdict(answer)`         | the `CandidateVerdict` for one tuple                         |

Each `CandidateVerdict` carries `status`, `derivation_count`,
`viable_derivation_count`, `support`, `exclusion`, and `sample_derivations`.

Pinning and re-asking:

```python
report = analyse(problem, query, pins=[keep_row(("L", "l1"))])
report.verdict(("L", "C")).status      # still "certain"
report.row_status(("L", "l1"))         # "required"
```

Pins accept `Pin` objects, `(row, keep_bool)` pairs, or
`(row, "keep"|"delete")` pairs.  Pinning mutually exclusive rows raises
`InfeasiblePins`; contradictory or unknown pins raise `ModelError`.

`Limits` (all default on) guard the exhaustive search: `max_component_rows`,
`max_repairs_per_component`, `max_derivations`, `max_certainty_nodes`.  Exceeding
one raises `RepairLimitExceeded` rather than returning an approximate answer.

## Reproducing and verifying

No third-party dependencies; run from the workspace root with Python 3.11.6.

```bash
# 1. Full test suite (unittest; discoverable from the root)
python3 -m unittest discover -v

# 2. Demo: engine verdicts, with witnesses
python3 -m consistent_query

# 3. Demo: engine verdicts side by side with the independent oracle
python3 -m consistent_query --oracle
```

## Verification scope

The entry point `consistent_query.verify.oracle` is an **independent**
brute-force oracle.  It shares no solving code with the engine: it re-derives
every constraint directly from the `model` declarations, enumerates all
survivor subsets, keeps the minimum-cost valid ones, and recomputes each
tuple's status from first principles.  `is_valid_repair` and `repair_cost` let
tests re-check any witness the engine emits.

What the tests cover (`tests/test_*.py`):

| test file                        | coverage                                                        |
|----------------------------------|-----------------------------------------------------------------|
| `test_model.py`                  | validation: positive int costs, key columns, FK target keys, arity, duplicate ids, query shape |
| `test_scenarios.py`              | the two-key-group example, three statuses, mandatory/optional FK + denial |
| `test_certainty_semantics.py`    | certainty that needs *multiple* derivations; the naive single-derivation rule would be wrong |
| `test_random_crosscheck.py`      | 120 seeded random key/denial instances vs. the oracle (cost, all optimal repairs, all statuses, witness validity) |
| `test_random_fk.py`              | 150 seeded random mandatory/optional-FK instances vs. the oracle |
| `test_limits_and_pins.py`        | limits firing, pin validation, infeasibility, derivation counts |

Every randomised instance asserts that the engine's whole set of optimal
repairs equals the oracle's, that every answer's status matches, and that each
reported support/exclusion witness is itself a valid repair at the optimal
cost.

### Explicit non-goals / not exercised

- Only **deletions** are considered; no insertions or value updates.
- The engine is exact but exponential in the worst case (it keeps *all* optimal
  repairs to reason about certainty).  Above the `Limits` it raises instead of
  approximating.
- Queries are equality joins with projection only: no aggregation, negation,
  or comparison operators beyond `=`.
- No concurrency or persistence layer; every call is a pure function of its
  arguments.

## Layout

```
consistent_query/
  model.py     # Row / Table / ForeignKey / Denial / Query / Problem + validation
  engine.py    # component decomposition, exact repairs, derivation + certainty search
  verify.py    # independent brute-force oracle
  examples.py  # the worked scenarios
  __main__.py  # CLI demo
tests/
  test_*.py
```
