# Speculative Shuffle Scheduler

A local, in-memory infrastructure component for a batch-shuffle scheduler. It
models the part of the map/reduce lifecycle where **two speculative attempts**
race for the same map partition, the **first success wins**, and the winning
Shuffle block may later be **lost with its executor**. When that happens the
scheduler reopens the lost round, invalidates only the results that actually
read the lost block (plus their transitive dependents), and leaves every other
result untouched.

Nothing here touches a network, a real executor, or a real shuffle service.
All inputs are synthetic and all state lives in the scheduler object.

## Layout

```
shufflesched/
  __init__.py     public exports
  model.py        BlockId, Block, Attempt, Partition, status enums, result types
  scheduler.py    ShuffleScheduler: the whole state machine
  requests.py     AttemptSpec / CompletionSpec / LossEvent / ReadSet helpers
  errors.py       construction-time and read-set exceptions
tests/
  test_graph.py            DAG construction, readiness, narrow vs shuffle deps
  test_results.py          first-success-wins, read-set validation
  test_loss.py             loss handling, selective invalidation, recompute
  test_stale_and_diamond.py stale rounds + full diamond failure replay
  helpers.py               synthetic DAG builders
```

## Entry point

`shufflesched.ShuffleScheduler` is the only entry point. Requires Python 3.11+
(the code uses `X | None` annotations and `enum.StrEnum`-style typing, but no
third-party dependencies).

```python
from shufflesched import ShuffleScheduler

sched = ShuffleScheduler()
sched.add_partition("A")                          # source (map) partition
sched.add_partition("B", ["A"], kind="narrow")    # narrow dependency
sched.add_partition("C", ["B"], kind="shuffle")   # shuffle (stage) dependency
sched.seal()                                      # validate the DAG
```

### The four operations

| Call | Purpose |
| --- | --- |
| `sched.add_partition(pid, upstreams, kind=...)` | Declare a partition and its inputs. `kind` is `source`, `narrow` or `shuffle`. |
| `sched.start(pid, attempt_id, executor)` | Launch an attempt; returns `StartResult` (`started`, `not_ready`, `duplicate_attempt`, ...). Launch two attempts for the same partition to model speculation. |
| `sched.complete(attempt_id, executor, reads=..., payload=...)` | Deliver a success callback. Returns a `Completion`. The first accepted callback for the current round wins; later ones are rejected. |
| `sched.report_executor_lost(event_id, executor)` | Report executor loss. Returns a `LossReport` listing what was lost, recomputed, and preserved. |

`reads` maps *upstream partition* → the `BlockId` (or raw attempt id) the result
actually read. When omitted, the attempt's launch-time snapshot is used.

### Recording the read set

Every committed result stores the upstream partitions it actually read and the
winning block it read from each, in `sched.consumed(pid)`:

```python
sched.provide_source("A", "a-1", "e1")        # map output on e1
sched.start("B", "b-1", "e1")
sched.complete("b-1", "e1")                   # reads {"A": A@e0#a-1}
assert sched.consumed("B") == {"A": BlockId("A", 0, "a-1")}
```

A result may name only declared inputs and only the *currently winning* block
of each input. A result that reports a superseded or fabricated block is
rejected (`REJECTED_STALE_INPUTS`) and never installed.

### Selective invalidation

Loss invalidation is driven by the recorded read sets, not by the DAG alone:

* the partitions whose **winning block was on the lost executor** are reopened;
* every committed result whose read set contains a lost block is invalidated;
* their transitive dependents are invalidated **only if they consumed them**;
* everything else keeps its block and read set unchanged.

A downstream partition that declares an input but did not actually read it is
*preserved*. Narrow (`parent → child`) and shuffle (`stage → stage`) edges are
treated identically for this walk, so multi-level chains recompute correctly.

### Stale rounds and duplicate notifications

Each partition has a monotonic `epoch`. Reopening after a loss increments it.
An attempt remembers the epoch it was launched under, and:

* a success callback whose epoch is no longer current is rejected as
  `REJECTED_STALE_EPOCH` — it can never overwrite the new round's block;
* an attempt orphaned by a recomputation is likewise rejected as stale, even
  though its slot was freed for the relaunch;
* a second success in the same round is rejected as `REJECTED_ALREADY_WON`;
* a repeated executor-loss event is acknowledged as `DUPLICATE_EVENT` and
  performs no work, so it cannot undo a result recomputed in between;
* blocks superseded by a newer result are forgotten, so a later loss report
  never names a block that no longer exists.

## Verification

Run the acceptance suite from the workspace root:

```
python3 -m unittest discover -s tests -t . -v
```

Current coverage (46 tests, all assertions against the real scheduler):

* **DAG** — kinds, duplicate/unknown/cyclic partitions, readiness across
  narrow and shuffle joins.
* **Results** — two speculative attempts, first-success-wins in either order,
  duplicate/unknown/impostor callbacks, read-set legality and staleness.
* **Loss** — only downstream consumers are invalidated; preserved branches keep
  their blocks and read sets; multi-level narrow+shuffle chains; duplicate and
  distinct loss events; superseded blocks are never reported lost.
* **Stale rounds / diamond replay** — a late old-round callback cannot replace a
  new block; the diamond's join recomputes **exactly once** after a left-branch
  failure; the join cannot become ready before the recomputed parent; a late old
  join attempt neither wins nor unblocks the join; a duplicate loss notification
  does not reopen the join a second time.

The guards for the epoch check, the read-set invalidation filter, and the
duplicate-event check were each removed in turn to confirm that at least one
test fails for every one of them.
