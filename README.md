# rcugrace — replayable RCU grace-period coordinator

A deterministic, replayable coordinator for RCU grace periods. It consumes a
stream of events (threads entering/leaving read-side sections, thread
migration, CPU online/offline/quiescent announcements, reclaim-callback
registration and reclaim receipts) and reports, per callback, **when it may
take effect** and **which threads it is still waiting on**.

Module path: `rcugrace` · Go 1.20.2 · no third-party dependencies.

## Model

- **Reader instance.** A thread entering its read section from depth zero
  creates one reader instance with a fresh id and a fresh *generation*. Nested
  `EnterRead` calls only increase a depth counter; the instance keeps the
  generation of its **outermost** entry. The instance is retired when the depth
  returns to zero.
- **Wait set snapshot.** A callback's wait set is exactly the reader instances
  that are inside a read section *at registration time*. Readers that start
  later never join it, so they cannot extend an already-registered callback.
- **Migration.** `Migrate` updates the CPU a thread is observed on. A reader
  keeps its instance id and generation, so a migrated thread is still the same
  reader.
- **CPU announcements.** `CPUOnline`, `CPUOffline` and `CPUQuiescent` are
  recorded faithfully (so replay is exact) but never influence readiness. An
  offline or quiescent CPU does **not** stand in for a thread leaving its
  section.
- **Multiple callbacks, per-source ordering.** Each callback keeps its own wait
  set. Within one source, callbacks are handed to the reclaimer strictly in
  registration order; sources are independent. Because the wait set is a
  snapshot of all open readers, a later same-source callback always still holds
  any open reader blocking an earlier one — ordering therefore falls out of the
  wait sets and is additionally enforced by a per-source head check.
- **Reclaim receipts.** The first `AckCallback` for a callback confirms it;
  repeated or replayed receipts are accepted and logged but change nothing, so a
  callback can only be confirmed once.
- **Reopen.** `Snapshot`/`Reopen` persist committed callback positions
  (`EffectiveAt`, `AckedAt`), pending wait sets, open (possibly nested) read
  sections, generation counters and the accepted event log. `Reopen` verifies
  the snapshot against a fresh replay of its own log and rejects a snapshot that
  does not reproduce.

## Public API

```go
func New() *Coordinator
func Replay(events []Event) (*Coordinator, error)

func (c *Coordinator) Apply(e Event) (Outcome, error)
func (c *Coordinator) Log() []LoggedEvent
func (c *Coordinator) Generation() uint64
func (c *Coordinator) LastSeq() int64

func (c *Coordinator) CallbackReport(id string) (CallbackReport, bool)
func (c *Coordinator) Reports() []CallbackReport
func (c *Coordinator) PendingCallbacks() []CallbackReport
func (c *Coordinator) Readers() []ReaderReport
func (c *Coordinator) CPUs() []CPUReport

func (c *Coordinator) Snapshot() ([]byte, error)
func Restore(data []byte) (*Coordinator, error)
func Reopen(data []byte) (*Coordinator, error)
```

Event constructors: `EnterRead(thread)`, `ExitRead(thread)`,
`Migrate(thread, cpu)`, `CPUOnline(cpu)`, `CPUOffline(cpu)`,
`CPUQuiescent(cpu)`, `RegisterCallback(id, source)`, `AckCallback(id)`.

`Apply` validates first and is atomic: a rejected event returns an error, leaves
state untouched, and is **not** appended to the log. Sentinels: `ErrEmptyThread`,
`ErrEmptyCallbackID`, `ErrNegativeCPU`, `ErrUnknownKind`, `ErrNotReading`,
`ErrDuplicateCallback`, `ErrUnknownCallback`, `ErrBadSnapshot`.

`CallbackReport` fields: `ID`, `Source`, `Order`, `Seq`, `Waiters` (sorted
threads still inside), `GraceSatisfiedAt`, `EffectiveAt` (the point from which
the callback may take effect; `0` while pending), `Acked`, `AckedAt`.

## Reproduce

```sh
go test ./...          # full suite
go test -race -count=3 ./...
go test -run Example -v ./...   # prints per-callback timing for the scenario
```

## Verification scope

Hardware: Apple silicon (darwin/arm64), Go 1.20.2. `go test ./...`,
`go test -race -count=3 ./...` and `go test -run Example -v ./...` all pass.

Covered by `coordinator_test.go`:

| Concern | Test |
|---|---|
| Empty wait set releases at registration | `TestEmptyWaitSetEffectiveImmediately` |
| Blocks on a current reader, releases on exit | `TestBlocksOnCurrentReaderThenReleases` |
| Late readers do not extend | `TestReadersStartingLaterDoNotExtend` |
| Nesting counts as one outermost generation | `TestNestingCountsAsOutermostGeneration` |
| Migration keeps reader identity | `TestMigrationDoesNotEndReader` |
| CPU offline/quiescent never release a reader | `TestCPUOfflineAndQuiescentDoNotReleaseReader` |
| Migrated reader + thread past grace + nested old thread | `TestMigratedReaderThreadPastGraceOtherStillNested` |
| Different wait sets, source order | `TestDifferentWaitSetsAndSourceOrder` |
| Source independence + registration order | `TestSourceIndependenceAndOrdering` |
| Duplicate receipt confirms once | `TestDuplicateAckOnlyConfirmsOnce` |
| Receipt before effective is recorded | `TestAckBeforeEffectiveIsRecorded` |
| Unmatched exit rejected, not logged | `TestExitWithoutEnterRejected` |
| Rejected events leave state unchanged | `TestRejectedEventsLeaveStateUnchanged` |
| Re-enter after exit is a new reader | `TestReenterAfterExitIsNewReader` |
| Replay determinism | `TestReplayIsDeterministic` |
| Reopen preserves committed + open state | `TestSnapshotReopenPreservesCommittedAndOpen` |
| Reopen rejects corrupt snapshot | `TestReopenRejectsCorruptSnapshot` |
| Log reproduces state, rejects excluded | `TestLogDrivesReplayFaithfully` |

`TestMigratedReaderThreadPastGraceOtherStillNested` is the explicitly requested
sequence: a reader migrates, another thread has already crossed its grace
period (its callback committed earlier), and an old thread is still nested.
It asserts each callback's effective point and current waiters.

## Not covered / limitations

- **Not a concurrency primitive.** The `Coordinator` is single-threaded and
  not safe for concurrent use; callers serialise replay, which is what makes the
  event log a faithful record. The module coordinates grace periods from an
  event stream; it does not implement pointer publishing/`rcu_assign_pointer`.
- **No wall-clock timing.** Ordering is by event sequence number only.
- **`go vet` was not run** in this environment (command needed operator
  approval); the suite compiled and passed under `go test` and `-race`.
- **No fuzzing.** Corrupt-snapshot handling is covered by targeted cases, not a
  fuzz harness.
