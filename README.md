# pimutex — priority-inheritance mutex model

A small, in-memory model of a priority-inheritance mutex for a fixed set of
simulated real-time threads and locks. It is a **model**, not an OS
scheduler: no goroutines are blocked or scheduled by it, and it makes no
timing guarantees. It exists to make the priority-inheritance graph explicit
and inspectable.

## Toolchain

- Go **1.20.2** (`go version go1.20.2 darwin/arm64`).
- Standard library only. `go.mod` declares `go 1.20` and no dependencies, so
  the build makes no network access and installs nothing.

## Model

- A **thread** has a base priority. **Larger integers mean higher priority.**
- A **lock** has at most one owner and a FIFO wait queue.
- A thread may hold **several** locks at once, but may wait on **at most one**
  lock at a time.
- A thread's **effective priority** is the maximum of:
  - its own base priority, and
  - the effective priorities of every thread waiting on any lock it holds.

  The recurrence is well founded because the caller guarantees the wait-for
  graph has **no cycles**. Effective priorities are recomputed from the
  current state on every query, so a change at any point in the chain is
  visible on every link immediately — no incremental bookkeeping can go stale.
- **Release** hands the lock to the waiting thread with the highest
  **effective** priority (not base priority). Ties go to the **earliest**
  waiter in queue order.
- **CancelWait** removes a thread from the queue it is waiting on. The thread
  keeps its base priority and any locks it still holds; only the wait-for
  edge disappears, so priorities above it fall back at once. This is the
  "cancelling H leaves L boosted by the second-highest waiter on another
  lock, and M relaxes to exactly that value" behaviour.

## Public API (`package pimutex`)

```go
type ThreadID string
type LockID string

type AcquireOutcome uint8
const (
    Acquired AcquireOutcome = iota // lock was free, caller now owns it
    Waiting                        // lock was held, caller joined the queue
)

type Model struct{ /* unexported */ }

func New() *Model

// Registration
func (m *Model) AddThread(id ThreadID, base int) error
func (m *Model) AddLock(id LockID) error

// Operations
func (m *Model) Acquire(id ThreadID, lockID LockID) (AcquireOutcome, error)
func (m *Model) Release(id ThreadID, lockID LockID) error
func (m *Model) CancelWait(id ThreadID) (LockID, error)
func (m *Model) SetBasePriority(id ThreadID, base int) error

// Queries
func (m *Model) BasePriority(id ThreadID) (int, error)
func (m *Model) EffectivePriority(id ThreadID) (int, error)
func (m *Model) EffectivePriorities() map[ThreadID]int
func (m *Model) Owner(lockID LockID) (owner ThreadID, held bool, err error)
func (m *Model) Owners() map[LockID]ThreadID
func (m *Model) Waiters(lockID LockID) ([]ThreadID, error)
func (m *Model) WaitingOn(id ThreadID) (lockID LockID, waiting bool, err error)
func (m *Model) HeldLocks(id ThreadID) ([]LockID, error)
func (m *Model) Threads() []ThreadID
func (m *Model) Locks() []LockID
```

Sentinel errors (test with `errors.Is`): `ErrInvalidID`,
`ErrDuplicateThread`, `ErrDuplicateLock`, `ErrUnknownThread`,
`ErrUnknownLock`, `ErrAlreadyOwned`, `ErrThreadWaiting`, `ErrNotOwner`,
`ErrNotWaiting`, `ErrWouldDeadlock`.

`Model` methods are safe for concurrent use (an internal mutex serializes
access); the returned maps and slices are copies.

### Notes on misuse

- Re-acquiring a lock the caller already owns returns `ErrAlreadyOwned`
  (locks are not recursive).
- Acquiring while already waiting on a lock returns `ErrThreadWaiting`.
- `ErrWouldDeadlock` is **defensive only**: the problem statement guarantees
  acyclic input, so a correctly driven model never needs it. It exists so an
  accidental cycle is rejected instead of corrupting the graph.

## Build and test

Run from this directory:

```sh
go test ./... -count=1          # run the suite
go test ./... -count=1 -v       # per-test names
go test ./... -count=1 -race    # with the race detector
go test ./... -count=1 -vet=all # vet checks during the test build
```

## What the tests verify

All tests are in `model_test.go` and were actually executed (results below).
They use real assertions on model state, not canned output.

| Test | Scenario | Kind |
| --- | --- | --- |
| `TestNormalAcquireRelease` | free acquire, single-waiter handoff, last release frees the lock | normal |
| `TestInheritanceChain` | M owns B, L owns A and waits on B, H waits on A → H's priority flows L→M; cancelling H unwinds it; release hands B to L | normal / prompt |
| `TestCancelFallsBackToSecondHighest` | H (10) and H2 (8) wait on **different** locks L holds; after H cancels, L and M fall to 8, then to base after H2 cancels | interleaved / prompt |
| `TestHandoffPriorityOrderAndTieBreak` | handoff picks highest effective priority; equal priorities pick the earliest waiter | normal |
| `TestInheritedPriorityDrivesHandoff` | handoff compares **effective** priority: a boosted waiter beats a higher-base waiter | interleaved |
| `TestThreeHopChain` | 4-thread / 3-lock chain is fully transitive; relaxation unwinds one link at a time | interleaved |
| `TestBasePriorityChangePropagates` | `SetBasePriority` raise/lower/negative propagates to holders | boundary |
| `TestSnapshotReflectsState` | aggregate queries (`EffectivePriorities`, `Owners`, `Waiters`, `HeldLocks`, `Threads`, `Locks`) and that snapshots are copies | normal |
| `TestErrors` | every sentinel error path, plus re-acquire-own-lock and wait-while-waiting | boundary |
| `TestDeadlockRejected` | a cycle-forming acquire is rejected and leaves state intact | boundary (defensive) |
| `TestConcurrentReaders` | concurrent readers alongside a deterministic writer under `-race` | concurrency |

### Observed results

```
$ go test ./... -count=1
ok  	example.com/pimutex	0.465s

$ go test ./... -count=1 -race
ok  	example.com/pimutex	0.579s

$ go test ./... -count=1 -vet=all
ok  	example.com/pimutex	0.452s
```

All 11 top-level tests (and 15 `TestErrors` subtests) passed. The verbose run
reported `PASS` for each; no test was skipped.

## Verification scope and limits

Verified:

- Priority inheritance across chains of length 1–3, including a thread that
  holds multiple locks.
- Relaxation after `CancelWait` and after lowering a base priority, including
  the "second-highest waiter on another held lock" case from the prompt.
- Handoff ordering by effective priority with FIFO tie-break.
- Input validation and every documented error path.
- Safe concurrent reads under the race detector.

Not covered / out of scope:

- Real thread scheduling, blocking, timeouts, or fairness over time — this is
  a static state model, not a scheduler.
- More than one lock per thread being waited on (the problem forbids it) and
  cyclic input (the problem guarantees none); cycles are rejected defensively
  rather than simulated.
- Persistence, networking, or any interaction with real system targets. The
  model operates only on local, synthetic identifiers created in the tests.
- Performance at large scale; the implementation recomputes effective
  priorities per query, which is intentional for a reference model.
