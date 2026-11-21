# joinplan — left-deep equi-join planner (Python 3.11.6)

Compiles an equi-join query over up to **seven** tables into a left-deep
execution plan that minimises total bytes transferred. It is a purely local
infrastructure component: no network access, no external targets, only local
synthetic fixtures.

## What it does

Input describes a cluster and a join graph:

* number of cluster nodes,
* each table's size in bytes and its initial partition key (optional),
* join edges as `Table.column` pairs on both endpoints,
* an output-byte estimate for every *connected* set of two or more tables,
* an optional per-node broadcast limit.

The planner enumerates only **left-deep** plans and only extends the left side
with a table that shares a join edge with it (no cross products). Two physical
strategies are costed per step:

| strategy | cost | partition property of the result |
| --- | --- | --- |
| broadcast right table | `right_bytes × (nodes − 1)`; rejected if `right_bytes > broadcast_limit` | inherits the left side's partitioning |
| repartition on the join key | sum of the bytes of whichever side is *not* already partitioned on that key | inherits the equivalence class of the join key |

Columns proven equal by a join predicate form an equivalence class; a later
join on any member of that class reuses the existing partitioning for free.
The goal is minimum total transfer bytes, with a per-step and cumulative cost
breakdown. Among equally optimal plans any one is acceptable (ties are broken
deterministically).

Because the search state is `(table set, equivalence classes, partitioning)`
rather than just the cheapest plan per table set, a slightly more expensive
intermediate that is already partitioned on the *next* join key can win
globally.

## Entry point

```sh
# from this workspace
python3 -m joinplan plan query.json
python3 -m joinplan plan -            # read JSON from stdin
```

Exit codes: `0` plan produced, `2` invalid input, `3` valid input but no
left-deep plan exists.

As a library:

```python
from joinplan import load_query, plan_query

plan = plan_query(load_query("query.json"))
print(plan.total_cost)
for step in plan.steps:
    print(step.index, step.method, step.cost, step.cumulative_cost)
```

### Input format

```json
{
  "nodes": 4,
  "broadcast_limit": 1000000,
  "tables": [
    {"name": "A", "bytes": 2000, "partition_key": "k"},
    {"name": "B", "bytes": 1000}
  ],
  "edges": [{"left": "A.k", "right": "B.k"}],
  "outputs": [{"tables": ["A", "B"], "bytes": 5000}]
}
```

Validation rejects, among others: more than seven tables, unknown fields,
duplicate tables/edges/estimates, negative or non-integer sizes, boolean sizes,
self-join edges, edges to unknown tables, disconnected join graphs, and output
estimates that do not exactly cover the connectable table sets.

### Output format

```json
{
  "total_cost": 1000,
  "result_tables": ["A", "B"],
  "result_bytes": 5000,
  "partition_columns": ["A.k", "B.k"],
  "steps": [
    {
      "index": 0, "method": "repartition",
      "left_tables": ["A"], "right_table": "B",
      "left_key": "A.k", "right_key": "B.k",
      "left_transfer": 0, "right_transfer": 1000,
      "cost": 1000, "cumulative_cost": 1000,
      "result_tables": ["A", "B"], "result_bytes": 5000,
      "result_partition_columns": ["A.k", "B.k"]
    }
  ]
}
```

## Verification

```sh
python3 -m unittest discover -s tests -v
```

All tests run real assertions (no placeholders). Coverage:

* `tests/test_parse.py` — accepted inputs (including the 7-table maximum and a
  zero broadcast limit) and rejected inputs (8 tables, duplicates, self-join,
  unknown table, disconnected graph, missing/extra output estimates, bad types).
* `tests/test_planner.py` — an acceptable optimum with exact step costs;
  rejected alternatives (broadcast over the limit, unconnected extension);
  boundary/interleaved cases: the broadcast limit boundary flips the method,
  and a deliberately costlier first step that yields a next-key-partitioned
  intermediate wins globally and is asserted to beat the greedy route.
* `tests/test_differential.py` — 200 random synthetic query graphs check the DP
  optimum against an independent exhaustive enumerator (`tests/reference_planner.py`),
  plus invariants on which bytes each method may move.
* `tests/test_cli.py` — the real `python -m joinplan plan` entry point via
  subprocess: file/stdin input, JSON plan output, exit codes 0/2, diagnostics.

`tests/reference_planner.py` re-derives the cost model independently of
`joinplan.planner`; disagreements surface as cost mismatches.
