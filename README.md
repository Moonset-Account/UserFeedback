# hierpost — hierarchical integer post-processing for noisy frequency tables

`hierpost` turns a set of **already noised** aggregate measurements into a
publishable integer table that satisfies two hard rules at once:

* every published value is a **non-negative integer**, and
* for every internal node, the **children's counts sum exactly to the parent's
  count** (so the table is additive level by level), and the root equals the
  **allowed public root total**.

Subject to those constraints it **minimises the sum of weighted squared errors**
over exactly the nodes that carry a measurement. Nodes without a measurement
(missing / 缺测 branches) still receive counts and stay additive, but never add
to the error. Ties are broken by the **lexicographically smallest vector of leaf
counts**, with leaves ordered by `leaf_index`.

The module only ever consumes three things: noised measurements, per-node
positive integer weights, and the public root total. It has **no access to
individual-level records** and never reconstructs them.

## Public API

```python
from hierpost import Node, Hierarchy, HierarchyError, solve, solve_spec

spec = {
    "root_total": 200,
    "nodes": {
        # node_id: {parent, weight, noisy, leaf_index}
        "root":       {"parent": None,  "weight": 1, "noisy": 205},
        "urban":      {"parent": "root","weight": 3, "noisy": 195},
        "urban_east": {"parent": "urban","weight": 2, "noisy": -3, "leaf_index": 0},
        "urban_west": {"parent": "urban","weight": 2, "noisy": -7, "leaf_index": 1},
        "rural":      {"parent": "root"},          # missing branch
        "rural_north":{"parent": "rural", "leaf_index": 2},
        "rural_south":{"parent": "rural", "leaf_index": 3},
    },
}

result = solve_spec(spec)

result.counts         # {"root": 200, "urban": 144, ...}
result.total_cost     # 31544  (sum of weighted squared errors)
result.leaf_vector    # (74, 70, 0, 56)  in leaf_index order
result.level_totals   # {0: 200, 1: 200, 2: 200}
result.measured_cost  # per-node cost breakdown, measured nodes only
```

| Name | Meaning |
| --- | --- |
| `Node(node_id, parent, weight=None, noisy=None, leaf_index=None)` | One hierarchy node. `noisy=None` means missing/缺测. |
| `Hierarchy(nodes, root_total)` | Validates the tree, weights and root total. |
| `Hierarchy.from_dict(spec)` | Builds a `Hierarchy` from the plain-dict form above. |
| `HierarchyError` | Raised for an inconsistent hierarchy description. |
| `solve(hierarchy)` | Solve a `Hierarchy`; returns a `PostprocessResult`. |
| `solve_spec(spec)` | `solve(Hierarchy.from_dict(spec))`. |
| `PostprocessResult` | `counts`, `total_cost`, `leaf_vector`, `level_totals`, `measured_cost`. |

### Input rules (validated)

* Exactly one root (`parent is None`); the structure is a connected tree.
* Unique non-empty string ids; every `parent` names an existing node.
* `weight` is a **positive integer**; a node with a measurement **must** carry
  one. A missing node may also carry a weight (it is validated but unused).
* `noisy` is an **integer** and may be negative.
* `root_total` is a **non-negative integer**.
* `leaf_index` is supplied for **all leaves or none** of them; values are unique
  non-negative integers. When omitted, leaves are ordered by sorted node id.

### Output guarantees

`solve` re-checks the final table before returning: every count is a
non-negative integer, the root equals `root_total`, every internal count equals
the sum of its children, and the reported `total_cost` equals both the sum of
`measured_cost` and an independent recomputation. A failed check raises
`AssertionError` rather than publishing an invalid table.

## How it works

For a subtree rooted at `v`, `F_v(t)` is the minimum weighted squared error in
that subtree subject to `x_v == t`. Leaves use the closed form
`w * (t - noisy)**2` (or `0` when missing). An internal node's table is the
**min-plus convolution** of its children's tables plus its own term. Counts are
bounded by `root_total` (everything is non-negative and sums upward), so each
table covers `0 .. root_total`. States carry the lexicographically smallest
**whole-hierarchy** leaf vector attaining the optimal cost; lexicographic order
is invariant under adding a fixed non-negative vector, so keeping the smallest
per state is safe when states combine. Cost is `O(root_total**2 * L)` per edge
(`L` = leaves); at `root_total = 200` typical trees solve in well under a second.

Internal counts are then recomputed as subtree sums of the published leaf
counts, so additivity holds by construction.

## Running

No third-party dependencies; Python 3.11.6.

```bash
# full test-suite
python3 -m unittest discover -s tests -v

# the reproducible worked example (prints the table, total cost, level totals)
python3 -m hierpost.example
```

## Verification scope

Verified locally with synthetic aggregate data only; no external data was read.

* **Worked example** (`hierpost/example.py`, `tests/test_example.py`): root
  total 200, negative noisy readings (`urban_east = -3`, `urban_west = -7`,
  `urban = 195`, `root = 205`) and a fully missing branch (`rural` and both of
  its leaves). Expected optimum `total_cost = 31544`,
  `leaf_vector = (74, 70, 0, 56)`, `level_totals = {0: 200, 1: 200, 2: 200}`
  — and it matches exhaustive enumeration of every valid table.
* **Validation** (`tests/test_model.py`): single/multiple roots, duplicate ids,
  unknown parents, disconnected cycles, non-positive/boolean/float weights,
  float measurements, measured-node-without-weight, partial/duplicate
  `leaf_index`, and non-integer/negative `root_total`.
* **Constraints** (`tests/test_solver.py`): non-negativity, root total,
  parent = sum(children), `total_zero`, all-missing, missing-internal nodes,
  negative noise clamped to non-negative counts, level totals on complete and
  partial levels.
* **Optimality and tie-break** (`tests/test_solver.py`): DP vs exhaustive
  enumeration on several small shapes; symmetric ties resolve to the
  lexicographically smallest leaf vector; `leaf_index` (not node id) governs
  the order.
* **Randomized** (`tests/test_randomized.py`): 400 generated small trees, all
  400 cross-checked against brute force — constraints hold and the DP
  cost/vector equal the exhaustive optimum every time.

All of the above was executed locally with `python3 -m unittest discover -s tests
-v`; the run completed **40 tests, OK** in roughly 9 seconds on Python 3.11.6.
Results are reproduced from actual assertions, not hand-estimated.
