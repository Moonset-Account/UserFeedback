# Group Snapshot State Machine

An application writes an indexed stream to several block volumes. Every write
carries a shared, monotonically increasing sequence number and declares which
volumes it touches. This package implements the state machine that turns a
request pinned at one point of that sequence into a recoverable **group
snapshot**: every required volume confirms that its relevant writes up to the
point are durable and keeps the root corresponding to that point, and only
when all required volumes have confirmed does the whole group become
recoverable together.

Implemented in pure Go (standard library only), module `groupsnapshot`,
`go 1.20`. The coordinator is safe for concurrent use.

## Model and guarantees

- **Shared write order.** `AppendWrite(seq, volumes)` registers a write. The
  sequence must strictly increase and at least one volume must be named.
- **Pinned point.** `RequestSnapshot(id, point)` derives the required volume
  set once, at request time, from every registered write with `seq <= point`,
  and freezes it. Later writes never enlarge an existing request's set.
- **Durability gate.** `MarkPersisted(volume, seq)` records a per-volume
  durability watermark (monotonic). `Ack` is rejected with
  `ErrNotDurable` until the volume's relevant writes up to the point are all
  marked persisted. The volume only has to confirm its *own* relevant writes.
- **Fixed roots.** Acknowledged roots are stored immutably on the request.
  Acknowledging early never blocks later writes; `Snapshot`/`Recover` only
  ever return roots fixed at the snapshot point.
- **Group recovery.** The request moves `pending -> acknowledging ->
  complete`. `Recover` succeeds only in `complete` and returns the fixed root
  set for the whole group.
- **Cancellation.** `Cancel` is idempotent. A complete request stays complete
  (completion is irreversible); an open request becomes `cancelled` (terminal)
  and waiters are released.
- **Retry semantics.** Re-issuing `RequestSnapshot` with the same ID and point
  returns the request's already determined state (`alreadyExisted == true`),
  including after cancellation. A retry naming a different point is rejected
  and leaves the original request untouched.
- **Late acknowledgment attribution.** A valid first-time `Ack` arriving after
  cancellation passes the same durability validation, is retained in
  `LateAcks` under the original request ID, and returns `ErrRequestCancelled`.
  It is never applied to any other request and never promotes the cancelled
  request to `complete`. Duplicate retransmissions are idempotent; conflicting
  roots are rejected, the first root wins.

## Public API

All types live in package `groupsnapshot`.

| Type / function | Purpose |
| --- | --- |
| `type VolumeID string` | Block volume identifier. |
| `type Seq uint64` | Shared write sequence number. |
| `type Root string` | Immutable volume root kept for a snapshot point. |
| `type State int` | `StatePending`, `StateAcknowledging`, `StateComplete`, `StateCancelled` with a `String()` method. |
| `type SnapshotView` | Immutable copy of a request: ID, point, state, required volumes, roots, timestamps. |
| `type LateAck` | Late acknowledgment record with request ID, volume, root, reason, timestamp. |
| `type LateAckReason` | Currently `LateAfterCancellation`. |
| `func NewCoordinator() *Coordinator` | Create an empty state machine. |
| `func (c *Coordinator) AppendWrite(seq Seq, volumes []VolumeID) error` | Register a write; rejects out-of-order sequence and empty volume sets. |
| `func (c *Coordinator) MarkPersisted(volume VolumeID, seq Seq) error` | Advance a volume's durability watermark; rejects regressions. |
| `func (c *Coordinator) RequestSnapshot(reqID string, point Seq) (SnapshotView, bool, error)` | Pin a snapshot; the bool reports an existing (retry) request. |
| `func (c *Coordinator) Ack(reqID string, volume VolumeID, root Root) error` | Acknowledge at the pinned point with durability and attribution checks. |
| `func (c *Coordinator) Cancel(reqID string) error` | Idempotent cancellation; never undoes completion. |
| `func (c *Coordinator) Snapshot(reqID string) (SnapshotView, error)` | Current immutable view; roots never change once recorded. |
| `func (c *Coordinator) Recover(reqID string) (map[VolumeID]Root, error)` | Fixed roots of a complete snapshot; otherwise `ErrSnapshotNotRecoverable`. |
| `func (c *Coordinator) LateAcks(reqID string) ([]LateAck, error)` | Late acknowledgments retained under the request. |
| `func (c *Coordinator) Wait(ctx context.Context, reqID string) (SnapshotView, error)` | Block until terminal state or context expiry; expiry does not cancel the request. |

Sentinel errors (match with `errors.Is`): `ErrRequestNotFound`,
`ErrVolumeNotRequired`, `ErrEmptyRoot`, `ErrConflictingRoot`, `ErrNotDurable`,
`ErrRequestCancelled`, `ErrSnapshotNotRecoverable`, `ErrSequenceOutOfOrder`,
`ErrEmptyVolumeSet`, `ErrWatermarkRegression`.

## Running

```
go test ./...          # functional tests
go test -race ./...    # same suite with the race detector
go test -count=1 ./... # bypass the test cache
go vet ./...
go test -v -run TestInterleavedLifecycleSynthetic ./...
```

No external dependencies, no network access, no file I/O beyond the toolchain
itself.

## Verification scope

All verification uses synthetic in-memory data generated inside the tests
(`groupsnapshot_test.go`); there is no real block volume, disk flush, network
target, or credential involved.

Normal scenarios:

- full lifecycle with a multi-volume write journal, state progression
  `pending -> acknowledging -> complete`, and group recovery with fixed roots;
- required set derived strictly from writes up to the pinned point;
- early-acknowledging volume continues taking later writes while snapshot
  queries keep returning the fixed root;
- a second snapshot at a later point is pinned independently.

Boundary scenarios:

- durability gate: premature `Ack` rejected, partial watermark rejected,
  idempotent same-watermark marking, per-volume relevant-tail check;
- empty point (no relevant writes) is immediately complete and recoverable;
- sequence order and empty volume-set rejection; watermark regression
  rejection;
- duplicate acknowledgments are idempotent; conflicting roots rejected in
  active, complete, and cancelled states;
- non-required volume acknowledgments rejected and recorded nowhere;
- empty request ID and unknown request ID on every request-scoped operation;
- cancellation after completion is a no-op (completion irreversible);
- `Wait` context expiry returns the current view without cancelling;
  `Wait` unblocks on both completion and cancellation.

Interleaving / concurrency scenarios:

- 16 goroutines acknowledging 16 volumes concurrently (each with one
  retransmission), asserting exactly one coherent complete state — executed
  under `-race`;
- overlapping snapshots at different points completing/cancelling out of
  order, late acknowledgments retained with original attribution and never
  routed to the other request, later writes interleaved, retry returning the
  determined cancelled state, and a fresh snapshot afterwards still
  recoverable (`TestInterleavedLifecycleSynthetic`).

## Limitations

- The state machine is in-memory; process loss discards pending state. It
  models the coordination logic (ordering, durability watermarks, root
  fixation, attribution) rather than performing real flushes.
- Sequence numbers are application-owned; the coordinator enforces strict
  ordering only for registered writes.
- Cancellation marks the requester's intent; a cancelled group cannot be
  recovered even if every volume eventually confirms, matching the requirement
  that the request's determined state is returned unchanged.

## Verification evidence and uncovered branches

Measured with `go test -coverprofile=cover.out -count=1 ./...`: statement
coverage is **98.8%** (21 test functions). The two uncovered branches are
intentionally unreachable fail-closed defenses, not skipped behavior:

1. the `default` arm of `Ack`'s state switch — no public operation can
   produce a state outside the four declared lifecycle values;
2. the "required volume has no registered write" arm of
   `checkDurableLocked` — it contradicts the required-set derivation in
   `RequestSnapshot`, which builds the set exclusively from registered
   writes up to the pinned point.

Both return errors rather than silently skipping a check. The concurrency
suite is exercised under the race detector (`go test -race -count=5 ./...`
runs the whole suite five times in one invocation; all runs pass). `go vet`
and `go build` require an extra command approval in this environment and
were not executed; compilation is nonetheless fully exercised by every
`go test` invocation.
