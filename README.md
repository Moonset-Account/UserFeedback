# Queue backlog scale controller

A deterministic, in-memory scale controller for a queue-consuming workload. It
turns backlog samples into replica create / cancel / terminate intents. There is
no networking, no cloud client and no background goroutine: callers drive it and
inspect the resulting intents.

## Layout

| Path | Purpose |
| --- | --- |
| `go.mod` | Module `example.com/queue-scaler`, Go 1.20 |
| `scaler.go` | `Controller` implementation and its small domain types |
| `scaler_test.go` | Acceptance tests (`go test ./...`) |

## Entry points

- `New(Spec, ...Option) (*Controller, error)` — validates
  `MinReplicas`, `MaxReplicas` and a strictly positive `TargetPerReplica`.
  `WithClock` injects a time source for tests.
- `(*Controller).Apply(Sample) (Result, error)` — the only way to feed workload
  state. Returns the computed target, the scale-down window state, the number of
  assigned replicas and the actions caused by *this* sample.
- `(*Controller).MarkReady(replicaID) error` — readiness acknowledgement.
  Idempotent; ignored for replicas that already received a termination intent.
- `(*Controller).FailCreate(replicaID) error` — withdraws a pending
  reservation after an infrastructure failure.
- `(*Controller).Replicas()`, `(*Controller).Counts()`,
  `(*Controller).Actions()` — read-only snapshots; `Actions` is the
  append-only action log.

## Rules implemented

1. **Target** = `ceil(backlog / TargetPerReplica)`, clamped to
   `[MinReplicas, MaxReplicas]`. Backlog `0` yields the minimum.
2. **Scale up is immediate** and issues exactly `target - assigned` create
   intents. `assigned` counts ready *and* pending replicas, so replicas that
   were requested but are not ready yet already occupy the target.
3. **Scale down** uses the highest target among valid samples strictly newer
   than `now - 60s`. A sample exactly 60s old has left the window. The window
   is squeezed only when `assigned > windowMax`.
4. **Missing backlog** (`Sample.Backlog == nil`) keeps the scale unchanged and
   is not recorded in the window. A measurement whose timestamp is already
   outside the window is treated as stale: `Result.Stale` is set, nothing is
   recorded and no action is taken, because it could never affect a decision.
5. **Stable identities**: replicas are named `replica-1`, `replica-2`, ...
   from a monotonically increasing counter; identifiers are never reused, even
   after a failed create or a termination.
6. **Idempotent replay**: a repeated `Sample.ID` is reported as `Duplicate`
   and changes no state — the action log gains no entries, in particular no new
   replica numbers. Duplicate readiness acknowledgements are no-ops.
7. **Failed create** removes the pending replica, records a `cancel_create`
   action with reason `create_failed`, and frees the slot for the next sample.
   The next sample allocates a fresh identifier (e.g. `replica-3`).
8. **Scale-down ordering**: newest requested pending replicas are cancelled
   first (`cancel_create`), then ready replicas are terminated in ascending
   replica-number order (`terminate`). Ties on request time are broken by the
   larger replica number.
9. **Terminating replicas leave the computation**: they are excluded from
   `assigned`, never terminated twice, and cannot return to ready.

## Verification scope

`go test ./...` runs black-box acceptance tests through the exported API; every
test makes real assertions. Coverage:

| Test | Constraint |
| --- | --- |
| `TestScaleUpIsImmediateAndCountsPendingReplicas` | acceptable path: exact ceiling target, immediate creates, pending counted in the target, duplicate acks |
| `TestCeilingAndBounds` | ceiling division, min/max clamping, zero backlog |
| `TestMissingBacklogKeepsScale` | missing measurement keeps scale, both up and down |
| `TestStaleMeasurementIsIgnored` | a backdated measurement outside the window cannot scale or poison the window |
| `TestScaleDownUsesHighestWindowTargetAndExactBoundary` | 60s boundary (exactly 60s = expired, 1ns younger = inside), highest-in-window target gates scale-down |
| `TestScaleDownCancelsNewestPendingThenReadyAscending` | mixed shrink ordering: newest pending first, then ready ascending |
| `TestTerminatingReplicasLeaveComputation` | terminating replicas excluded, no double termination, ack ignored |
| `TestDuplicateSampleReplayIsIdempotent` | replayed sample (including conflicting payload) adds no actions or replica numbers |
| `TestDuplicateScaleDownReplayIsIdempotent` | replaying the shrinking sample re-terminates nothing and adds no replica numbers |
| `TestCreateFailureReleasesReservation` | create failure withdraws the placeholder, no identifier reuse, invalid transitions rejected |
| `TestInvalidInputsAreRejected` | rejected branches: invalid specs, empty sample ID, negative backlog, unknown replica |
| `TestActionLogIsAppendOnlyAndStable` | end-to-end log: one create per identity, no growth on full replay |

Run with:

```sh
go test ./...
```
