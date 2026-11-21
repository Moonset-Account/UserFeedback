# Coordination service: sequential-node lock with predecessor watches and fencing

An in-process, deterministic model of a ZooKeeper-style exclusive lock.

Each contender creates an **ephemeral sequential node** under one lock parent.
The smallest sequence number holds the lock. Every other contender arms a
**one-shot watch on its immediate predecessor only**. When a session expires,
every node and watch it owns is removed atomically. The protected `Resource`
enforces a **fencing token**: once the new holder performs its first write, any
write carrying a smaller sequence is rejected — even if the old process has not
yet noticed that its session expired.

## Public API

Everything lives in package `coordination` (module `coordination`).

### `Service` — the coordination service

| Symbol | Behaviour |
| --- | --- |
| `NewService() *Service` | Empty service. |
| `(*Service) OpenSession(id string) error` | Register a client session. Duplicate ids fail with `ErrSessionExists`; empty ids with `ErrEmptySession`. |
| `(*Service) SessionExists(id string) bool` | True while the session is open and unexpired. |
| `(*Service) SessionNodePaths(id string) []string` | Sorted paths of nodes still owned by the session. |
| `(*Service) CreateEphemeralSequential(parent, sessionID, data string) (Node, error)` | Create an ephemeral sequential node. The counter is **per parent**, so each lock queue numbers `1, 2, 3, …` and keeps increasing after deletions. |
| `(*Service) Children(parent string) []Node` | Immutable snapshot of one queue, ordered by sequence. |
| `(*Service) GetNode(path string) (Node, bool)` | Snapshot of one node. |
| `(*Service) WatcherCount(path string) int` | One-shot watches currently armed on a path. |
| `(*Service) SessionWatcherCount(sessionID string) int` | Watches still armed for a session. |
| `(*Service) WatchPredecessor(selfPath, predPath, sessionID string, fn func(WatchEvent)) (uint64, WatchStatus, error)` | Atomically check that `selfPath` (owned by the session) and `predPath` still exist, then arm a one-shot watch on `predPath`. Returns `WatchNodeMissing` with no error if the predecessor is already gone. |
| `(*Service) WatchNode(path, sessionID string, fn func(WatchEvent)) (uint64, WatchStatus, error)` | Arm a one-shot watch on an arbitrary node. |
| `(*Service) Unwatch(id uint64)` | Disarm a watch (no-op if it already fired). |
| `(*Service) TryAcquire(selfPath, sessionID string) (bool, error)` | Atomically report whether `selfPath` is still the smallest node in its queue. |
| `(*Service) DeleteNode(path, sessionID string) error` | Delete an owned node and fire exactly the watchers armed on **that** node. |
| `(*Service) ExpireSession(sessionID string) ([]string, error)` | Atomically mark the session expired, remove **all** its nodes and watches, notify exactly the watchers armed on the removed nodes, and return the sorted removed paths. Expiring twice fails with `ErrSessionExpired`. |

`Node` carries `Parent, Path, Name, Sequence, SessionID, Data`.

### `Contender` — the client-side protocol state machine

| Symbol | Behaviour |
| --- | --- |
| `NewContender(svc, sessionID, parent string) *Contender` | One client for one lock. |
| `(*Contender) Enqueue() error` | Create the ephemeral sequential node. |
| `(*Contender) Enumerate() error` | List the queue and record the direct predecessor (empty if first). |
| `(*Contender) InstallWatch() (WatchStatus, error)` | Arm the one-shot watch found by the last `Enumerate`. If the predecessor has vanished, returns `WatchNodeMissing` (not an error), increments `RaceRecoveries`, and moves to `PhaseWoken`; the caller must `Enumerate` again. Returns `ErrNoPredecessor` when this contender is first. |
| `(*Contender) TryAcquireLock() (bool, error)` | Atomic "am I still first?" check; on success records the fencing token and moves to `PhaseHeld`. |
| `(*Contender) Step() (Phase, error)` | Advance exactly one phase (non-blocking). |
| `(*Contender) Run(maxSteps int) (Phase, error)` | Step until held, lost, released, or blocked watching with no pending wake. |
| `(*Contender) Release() error` | Disarm own watch and delete own node; only its watcher wakes. |
| `(*Contender) Node()/NodePath()/Phase()/Predecessor()/Fence()/Held()/RaceRecoveries()/WakeCount()/Wakes()` | Observation accessors. `Fence()` (the node sequence) stays valid after session loss, so a stale holder can attempt a late write. |
| `(*Contender) TakeOver(res *Resource, firstWrite string) error` | Register with the resource and perform the new holder's first write. |
| `(*Contender) WriteToResource(res *Resource, data string) error` | Fenced write without registering; this is the stale holder's late-write path. |

`Phase` values: `PhaseUnqueued`, `PhaseEnqueued`, `PhaseEnumerated`,
`PhaseWatching`, `PhaseWoken`, `PhaseHeld`, `PhaseReleased`, `PhaseLost`.

### `Resource` — the protected resource

| Symbol | Behaviour |
| --- | --- |
| `NewResource() *Resource` | Empty resource with fence 0. |
| `(*Resource) Acquire(sessionID string, token uint64) error` | Record an elected holder. Raises no barrier; rejects zero tokens (`ErrInvalidFence`) and tokens below the current fence (`ErrStaleHolder`). |
| `(*Resource) Write(sessionID string, token uint64, data string) error` | Accept when `token >= fence`, then raise the fence to `token`. Reject `token < fence` with `ErrStaleHolder` and leave data untouched. |
| `(*Resource) Fence()/Holder()/Data()/Version()/History()/Grants()` | Observation accessors; `History` records accepted **and** rejected attempts. |

## Protocol rules implemented

1. **Sequential queue and minimal holder.** One ephemeral sequential node per
   contender per lock; a strictly increasing per-lock sequence. Only the smallest
   sequence holds the lock (`TryAcquire` is the atomic decision).
2. **Watch the direct predecessor only.** `WatchPredecessor` arms one one-shot
   watch; a head's departure wakes exactly the contenders that actually armed a
   watch on the removed node, nobody else.
3. **Enumeration/watch race.** `Enumerate` and `InstallWatch` are separate calls.
   If the predecessor disappears in between, `WatchPredecessor` returns
   `WatchNodeMissing` atomically (it never arms a watch on a dead node). The
   contender re-enumerates: if it is now first it acquires with `TryAcquireLock`,
   otherwise it arms a fresh watch on the new predecessor.
4. **Session expiry.** `ExpireSession` removes all of the session's nodes and
   watches in one step. Watches owned by the expiring session die with it and do
   not fire.
5. **Fencing takeover.** The fencing token is the node sequence. After the new
   holder's first accepted write, the resource's fence is the new token; any
   write with a smaller token is rejected, even from a process that still
   believes it holds the lock. A zero token is always invalid. Equal tokens
   (same holder repeating a write) are accepted.
6. **Step-driven testing.** `Enqueue`, `Enumerate`, `InstallWatch`,
   `TryAcquireLock`, `ExpireSession`, and `Resource.Write` are all independent
   calls, so enumeration, watch installation, expiry, and writes can be advanced
   separately and interleaved.

## Reproduce

Requires Go 1.20.2 (the version this was built and verified with), standard
library only; there are no dependencies and nothing is downloaded.

```sh
go test ./...
go test -race ./...
go test -v ./...          # list every test
```

## Verification scope

Verified on Go 1.20.2 darwin/arm64 in a clean workspace:

- `go test ./...` — PASS
- `go test -race ./...` — PASS
- `go test -count=2 -race ./...` — PASS

Tests (`./*_test.go`, all executed):

**Deterministic protocol tests**
- `TestNormalQueueGrantOrder` — three contenders, in-order hand-off, each watches
  only its direct predecessor, only the direct successor wakes each time.
- `TestHeadReleaseWakesOnlyRegisteredWatcher` — head expiry wakes only its
  watcher; a later queued watcher is undisturbed.
- `TestEnumerationWatchRacePredecessorGone` — predecessor expires between
  `Enumerate` and `InstallWatch`; `WatchNodeMissing`, re-enumerate, acquire
  directly.
- `TestEnumerationWatchRaceReenumerateToNewPredecessor` — a non-head predecessor
  vanishes; re-enumerate to the real direct predecessor, arm a fresh watch, then
  wake and acquire.
- `TestSessionExpiryRemovesAllNodesAndWatches` — expiry across two locks removes
  all nodes and watches; only genuine watchers wake; the expiring session's own
  stray watch dies; double expiry and stale holder operations error out.
- `TestFencingRejectsStaleHolderAfterNewFirstWrite` — old holder's session
  expires but it still believes it holds; new holder writes; old holder's late
  write is rejected with `ErrStaleHolder`, data/version/fence unchanged, and the
  rejection is recorded.
- `TestFenceAcceptanceBoundaries` — zero token invalid; equal token accepted;
  lower token rejected on both `Write` and `Acquire`.
- `TestFenceBarrierMovesOnFirstWrite` — registering a successor does not move the
  barrier; the successor's first write does.
- `TestSequenceNumbersPerLock` — per-lock sequences start at 1, increase, and
  continue increasing after deletion; paths sort in sequence order.
- `TestFirstContenderAcquiresWithoutPredecessor` — empty-queue boundary; watch
  phase skipped with `ErrNoPredecessor`.
- `TestReleaseWakesOnlyWatchersOnReleasedNode` — releasing a queued non-head node
  wakes only its own watcher and the holder is unaffected.
- `TestWatchAndSessionErrors` — session id, unknown session, bad self node,
  missing node, and missing-predecessor error surfaces.

**Concurrency stress test**
- `TestConcurrentHandoffSingleHolder` — five contenders driven from goroutines
  perform a serial hand-off: queue drains, exactly five accepted writes with
  strictly increasing tokens and distinct writers, no rejected writes, no
  unexpected session loss.

## Limitations

- This is an in-process simulation, not a real distributed service or a wire
  protocol. There is no network, no persistence, and no real ZooKeeper
  compatibility.
- Locking is per process via a mutex; it models the protocol's ordering
  guarantees, not distributed clock or latency behaviour.
- `ExpireSession` is driven explicitly (the test plays the role of the session
  manager); no timer-based session heartbeat is implemented.
