# Row Lock Manager

A deterministic, single-process row lock manager in Go 1.20.2 supporting
shared (S), exclusive (X) and in-place upgrade (S → X) locks, FIFO-fair queue
ordering, an explicit wait-for graph, and deadlock resolution by aborting the
most recently started transaction on a cycle.

## Layout

| Path | Purpose |
| --- | --- |
| `go.mod` | Module `example.com/rowlock`, `go 1.20`, no external dependencies. |
| `rowlock.go` | Implementation and public API. |
| `rowlock_test.go` | Acceptance suite (`testing` only, real assertions). |

## Public entry points

Everything is driven imperatively, in caller-chosen order; there are no
goroutines, timers, or background workers, so a scenario is exactly the
sequence of calls you make.

- `NewManager()` — create an empty manager.
- `(*Manager).Begin(id)` — register a transaction and assign its unique,
  strictly increasing startup sequence number.
- `(*Manager).Lock(id, row, mode)` — request `Shared` or `Exclusive` on a row.
  Requesting `Exclusive` while holding `Shared` is an in-place upgrade.
  Returns a `LockResult`; it does not return an error for deadlock.
- `(*Manager).Release(id, row)` — cancel a pending request and/or release a
  held lock on one row, then re-grant.
- `(*Manager).End(id)` — commit a transaction, releasing it on every row, then
  re-grant.
- `(*Manager).WaitFor()` — current wait-for edges, deterministic order.
- `(*Manager).Cycles()` — cyclic strongly connected components of the graph.
- `(*Manager).Snapshot()` / `rowState` — holders and queue per row.
- `StartOrder`, `Status` — startup sequence and lifecycle state.

`LockResult.String()` renders a one-line explanation including every deadlock
round (cycle members, victim, released locks), every grant produced by the
step, the residual holders, and any requests still queued.

## Scheduling rules

1. **No barging.** A request is granted only if it is compatible with every
   holder except the caller and with every *earlier* ungranted request in the
   row queue. The queue is drained strictly head-first, so a later request can
   never pass an earlier incompatible one.
2. **Compatible readers pool.** Because the queue is drained head-first and S/S
   are compatible, a run of consecutive compatible shared requests is granted
   together in a single pass.
3. **Upgrade keeps the read lock.** While an S → X upgrade waits, the caller
   still holds S. The upgrade is granted in place once the caller is the only
   remaining holder.
4. **Wait-for graph uses direct blocking only.** Edges come from conflicting
   holders and from earlier incompatible queue entries (queue order). A
   transaction's own held lock never produces a self-edge.
5. **Unique startup order.** Empty and duplicate transaction ids are rejected,
   so the startup sequence is a strict total order.
6. **Deadlock resolution.** For each cyclic component the most recently started
   transaction is aborted and its locks on *all* rows (and its queued requests)
   are released; grantable requests are then re-granted. This repeats until the
   graph is acyclic. Multiple rounds in one step are reported in order.

## Verification

Run the suite:

```sh
go test ./...
```

Verification scope (`rowlock_test.go`), covering both accepted outcomes and
rejected/blocked outcomes plus the boundaries between them:

| Test | What it pins |
| --- | --- |
| `TestCompatibleSharedRunGrantedTogether` | A run of compatible readers is granted together (accept). |
| `TestWriterBlocksAndQueueOrderStopsBarging` | Writers and readers cannot pass earlier incompatible requests; queue and grants are exact (reject). |
| `TestUpgradeKeepsSharedAndIgnoresOwnLock` | Upgrade retains S while waiting; no self-edge; in-place grant (boundary). |
| `TestTwoUpgradersDeadlockAbortsLatest` | Two upgraders deadlock; the later-started victim rolls back, the older upgrades. |
| `TestThirdReaderQueuedBehindTwoUpgrades` | Headline scene: T1/T2 upgrade with T3 queued behind; explains who rolls back (T3, then T2), who upgrades (T1), and when the third reader proceeds. |
| `TestThreeWayCycleAbortsOnlyLatest` | Three-way cycle aborts exactly the latest member. |
| `TestEndReleasesEveryRow` | End releases all rows and re-grants in deterministic row order. |
| `TestReadsPoolAfterWritersDrain` | Writers block readers, but once they drain the whole reader run is admitted at once. |
| `TestQueuedUpgradeBlocksLateReaderWithoutCycle` | Queue order blocks a late reader without creating a false cycle. |
| `TestCascadingCyclesResolvedUntilClear` | Resolution repeats across rounds until acyclic; victims' locks are re-granted. |
| `TestErrors` | Duplicate/empty/unknown ids, invalid mode, downgrade rejection, not-held, lifecycle misuse, monotonic startup order. |

The assertions were checked for sensitivity by mutation: making `compatible`
always true fails 9 of 11 tests, and selecting the oldest rather than the
latest cycle member as victim fails all 4 victim-selection tests.

### The two-upgrader + third-reader scene

`TestThirdReaderQueuedBehindTwoUpgrades` drives:

1. `T1`, `T2` take S on `row`; `T3` starts later.
2. `T1` requests X → queued, still holding S.
3. `T3` requests S → queues behind `T1`'s pending X.
4. `T2` requests X → queue is `[T1:X, T3:S, T2:X]`. `T1` waits on `T2`'s S,
   `T3` waits on `T1`'s X (queue order), `T2` waits on `T3`'s S (queue order):
   one three-member cycle.
5. Resolution aborts `T3` (latest), then `T2`, until acyclic. `T1` is granted
   an in-place upgrade.
6. The aborted reader restarts as a new transaction; it queues behind `T1`'s
   X and is granted when `T1` ends.

`LockResult.String()` for step 4 names both victims and the surviving upgrade.
