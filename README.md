# Loop-free next-hop update planner

Given two forwarding tables — the **old** table, currently deployed, and the
**new** table we must reach — this tool computes a schedule that migrates every
node to its new next hop in the **fewest possible rounds**, while guaranteeing
that no node is ever stuck in a forwarding loop, even while a round is only
partially applied.

## The problem

- There is one egress, written `"OUT"`. It is the destination, not a router.
- Every other node has exactly one next hop: either `"OUT"` or another node.
- Both the old and the new table route **every** node to `"OUT"`.
- At most **9** nodes change their next hop.
- The controller pushes changes in **rounds**. Within a round the commands may
  take effect in **any order**; the next round is only sent after the whole
  round is acknowledged.
- Therefore a round is only acceptable if, for **every subset** of that round
  (the changes that "have already landed"), the mixed table still sends every
  node to the egress.

The classic hazard: if the old table sends `A` through `B`, and the new table
sends `B` through `A`, installing both at once is unsafe. Whatever order the
two commands land in, one of them creates the loop `A -> B -> A`. The fix is to
first point `A` straight at the egress (`A -> OUT`), and only then move `B` onto
`A`. That is a two-round schedule: round 1 `[A]`, round 2 `[B]`.

## Model of delivery

For a table `T`, start at a node and follow next hops. The node is *delivered*
if this walk reaches `"OUT"`. A node that revisits a node before reaching
`"OUT"` is in a **forwarding loop** and is counted as unreachable. A table is
**valid** (safe) when every node is delivered. `solve` requires both the old and
the new table to be valid and to cover the same node set.

## Algorithm

Every intermediate table is determined solely by *which changed nodes currently
carry their new next hop*. With at most 9 changed nodes there are at most
`2**9 = 512` such tables. The solver:

1. Validates both tables and computes the changed-node set (≤ 9).
2. Precomputes, for all `2**k` committed-node subsets, whether that mixed table
   is loop-free.
3. Runs a breadth-first search over committed-node subsets. A round of nodes is
   a legal BFS edge only if **every** subset of those nodes, layered on the
   current state, is loop-free.
4. Returns the shortest path from "nothing committed" to "everything
   committed"; BFS guarantees the **minimum number of rounds**.

Because the search is exhaustive over states, the round count is provably
minimal (validated against an independent brute-force partition search in the
test suite).

## Public API

```python
from flowupdate import solve, verify_schedule, check_round, load_problem

old = {"A": "B", "B": "OUT"}
new = {"A": "OUT", "B": "A"}

schedule = solve(old, new)          # Schedule(rounds=(('A',), ('B',)), ...)
schedule.round_count                # 2
schedule.rounds                     # (('A',), ('B',))
schedule.as_dict()                  # JSON-ready dict

result = verify_schedule(old, new, schedule.rounds)
result.ok                           # True
result.describe()                   # human-readable summary
```

Key entry points (`flowupdate/__init__.py`):

| Function | Purpose |
| --- | --- |
| `solve(old, new, *, max_round_size=9, max_changed=9)` | Minimum-round plan. Raises `ModelError` on bad input, `NoScheduleError` if no safe order exists. |
| `verify(old, new, schedule)` / `verify_schedule(old, new, rounds)` | Replay a plan, checking **every** within-round activation subset; confirm the final table equals the new table. |
| `check_round(base, nodes, new)` | Test a single round against a given base table; reports the first failing subset and the unreachable nodes. |
| `round_is_safe(base, nodes, new)` | Boolean shortcut for `check_round`. |
| `table_for_state(old, new, applied)` | Build the mixed table for a committed-node set. |
| `changed_nodes(old, new)` | Sorted nodes whose next hop differs. |
| `unreachable_nodes(table)` / `find_loops(table)` | Low-level diagnostics. |
| `validate_table(table, *, require_full=True)` | Check the routing invariants. |
| `load_problem(path)` | Load a JSON problem file. |

Data types: `Schedule`, `RoundCheck`, `Verification`, `ModelError`,
`NoScheduleError`.

## CLI

```
python3 -m flowupdate solve  examples/swap_pair.json
python3 -m flowupdate solve  examples/swap_pair.json --max-round-size 1
python3 -m flowupdate verify examples/swap_pair.json examples/swap_pair_plan.json
python3 -m flowupdate check  examples/swap_pair.json A B
```

- `solve` prints a JSON schedule (`round_count`, `changed`, `rounds`).
- `verify` exits 0 on success, 1 on an unsafe/incomplete plan.
- `check` exits 0 if the given round is safe, 1 otherwise.
- Input errors exit 2 with a message on stderr.

### Problem file format

```json
{
  "old": {"A": "B", "B": "OUT"},
  "new": {"A": "OUT", "B": "A"},
  "label": {"A": "optional human note"}
}
```

`old`/`new` may also be wrapped as `{"table": {...}}`. `label` is optional.

## Reproduction

Requires Python 3.11.6 (standard library only — no third-party dependencies).

```bash
# 1. Run the full deterministic test suite (from the workspace root)
python3 -m unittest discover -s tests -v

# 2. Run the bundled example problems end to end
python3 examples/run_examples.py

# 3. Cross-check optimality against an independent brute force (randomized)
python3 examples/crosscheck_optimality.py 100

# 4. Confirm worst-case timing at the 9-changed-node limit
python3 examples/perf_nine.py
python3 examples/perf_stress.py
```

## Verification scope

What is actually exercised and asserted by `tests/`:

- **Normal scenarios** — the `A`/`B` swap (2 rounds), a three-node chain
  reversal (3 rounds), a four-node instance whose optimum uses two 2-node rounds
  (`[A, C]`, then `[B, D]`).
- **Boundary scenarios** — identical tables (0 rounds, empty schedule); exactly
  9 changed nodes accepted; 10 changed nodes rejected; empty tables; nodes
  missing from one table; a `None` next hop; an unknown next hop; the egress
  name used as a forwarding node; an old table that already contains a loop;
  `max_round_size=1` forcing one node per round.
- **Interleaving scenarios** — a single round covering the swap is rejected with
  failing subset `('B',)`; reversing round order (`[B]` then `[A]`) is rejected;
  every within-round subset of each planned round is checked by the verifier.
- **Optimality** — for *all* valid four-node old/new pairs (125 valid tables, so
  15,500 ordered pairs) the solver's round count equals an independent
  brute-force optimum, and every produced plan verifies.
- **Verifier robustness** — plans that omit a changed node, include an unrelated
  node, repeat a node, or contain an empty round are all rejected.
- **CLI** — solve / verify / check exit codes and JSON output, including the
  error path (exit 2).
- **Nine-node maximum** — a nine-node chain reversal needs 9 rounds and verifies;
  solve time is ~7 ms. Randomized dense 9-node trials solve in under 0.2 s and
  all verify.

Explicitly **not** claimed: no real network devices, no netconf/gNMI/SNMP, no
vendor configuration syntax, no persistence or concurrent controllers. All data
is synthetic and local; nothing is sent off the machine.

## Layout

```
flowupdate/        library (model, solver, verify, cli)
tests/             unittest suite (test_planner.py, test_cli.py)
examples/          synthetic problem files, runner and analysis scripts
```
