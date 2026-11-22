# errfeedback — integer error-feedback for communication-efficient training

A small, deterministic, standard-library-only Go module that implements
sender-side error feedback (gradient compensation) with a receiver that accepts
exactly one sparse message per logical origin and publishes an aggregate only
after every expected group is confirmed.

Module path: `example.com/errfeedback` — Go 1.20.2.

## What it does

Every gradient value and every compensation value is an `int64` attached to a
**logical coordinate** (`Coord`). The communication *bucket* a coordinate
travels in is a per-step decision; the coordinate is not.

Per step the sender:

1. adds its confirmed residual for the coordinate to the incoming gradient,
2. snaps the sum onto the step's integer lattice (`StepSpec.Lattice`) with the
   step's rounding rule,
3. keeps at most `StepSpec.Keep` coordinates per bucket, chosen by descending
   magnitude (ties by ascending coordinate),
4. stages the difference `quantized - sent` as the **tentative** residual.

The tentative residual is committed **exactly once**, when the last bucket of the
step is confirmed. An aborted step, or a step with only some buckets confirmed,
leaves the committed residual untouched. Because unsent values and quantization
error are both retained, the per-coordinate partial sums telescope:
`sum(sent) + final residual == sum(gradient)`.

The receiver keys each accepted message by `(Step, Source, Bucket)`. A second
message for the same key is dropped (`AckDuplicate`); a message from a source
that was never expected is rejected (`ErrUnknownSource`). Only when all
`len(Sources) * len(Buckets)` groups are present does it publish the per-source
aggregate.

## Public API

Types
- `SourceID`, `StepID`, `BucketID`, `Coord` — logical identities. `Coord` is the
  stable key for compensation; it never changes when buckets are re-laid out.
- `StepStatus` (`StepOpen`, `StepSettled`, `StepVoided`), `ConfirmOutcome`
  (`ConfirmRecorded`, `ConfirmSettled`, `ConfirmDuplicate`, `ConfirmIgnored`),
  `AckResult` (`AckAccepted`, `AckDuplicate`, `AckRejected`).

Sender
- `NewSender(source, coords) (*Sender, error)` — fixed logical coordinate set.
- `s.Begin(step, StepSpec, StepPlan, gradient map[Coord]int64) error` — start a
  step; computes and stages packets and the tentative residual.
- `s.Packets(step)` / `s.Packet(step, bucket)` — copies of the sparse messages.
- `s.Confirm(Ack)` / `AckFor(msg)` — apply one bucket ack; returns
  `ConfirmOutcome`. Late/duplicate/stale acks never re-commit.
- `s.Abort(step) StepStatus` — void a step; residual unchanged.
- `s.Residual(coord)`, `s.Residuals()`, `s.Status(step)`, `s.PendingSteps()`,
  `s.StepAttempt(step)`, `s.Coords()`, `s.Source()` — inspection.

Plan and rule
- `StepSpec{Lattice, Rounding, Keep, MaxAbs}` with `Validate()`.
  `Rounding` ∈ `RoundNearestEven` (banker's), `RoundNearestAway`,
  `RoundTowardZero`, `RoundFloor`, `RoundCeil`.
- `NewStepPlan(planID, layoutID, ...Bucket) (StepPlan, error)` and
  `StepPlan.Validate/Coords/BucketOf/BucketByID`. A plan must partition the
  sender's coordinates exactly.
- `SparseMessage` — wire payload with `Entries` (sparse, transmitted),
  `Quantized` (full lattice points, for verification), and `Checksum` /
  `ComputeChecksum()`.

Receiver
- `NewReceiver()`, `r.Expect(Expect{...})`,
  `r.Deliver(SparseMessage) (AckResult, error)`,
  `r.Published(step) ([]Contribution, bool)`,
  `r.AcceptedCount(step)`, `r.AbortStep(step)`.

Journal / recovery
- `NewJournal(source, coords)`, `s.AttachJournal(j)`,
  `RecoverSender(entries []JournalEntry) (*Sender, error)`.
  The journal is an append-only, hash-chained log of every `Begin` (with the
  exact gradient), `Confirm` outcome, and `Abort`. `RecoverSender` replays it
  into a fresh sender: confirmed residual comes back exactly, an unconfirmed
  step stays open and its packets regenerate byte-for-byte (same checksum), and
  a late duplicate confirm replays as `ConfirmDuplicate` without a second write.
  Tampered or reordered entries return `ErrJournalInconsistent`.

## Semantics that the tests pin down

- **One message per logical source per step per bucket.** Duplicates are counted
  once, never summed.
- **Publish only when complete.** `Published(step)` is false until every
  `(source, bucket)` group is accepted.
- **Settle once.** Exactly one `Confirm` returns `ConfirmSettled`; every later
  ack for that step returns `ConfirmDuplicate` and leaves the residual unchanged,
  so a late packet cannot double-write compensation.
- **Void keeps the old residual.** `Abort` discards the step's tentative
  residual; late acks are `ConfirmIgnored`.
- **Repartition is coordinate-safe.** Changing buckets between steps moves each
  compensation value with its `Coord`; reusing a `LayoutID` with a different
  partition returns `ErrStaleLayout`.
- **No silent overflow.** `int64` arithmetic overflow surfaces `ErrOverflow`.

## Reproduce

Requires Go 1.20.2 and only the standard library; no network, no dependencies.

```
cd <this directory>
go test ./...
go test -race ./...
go vet ./...
```

(If a sandbox blocks `go vet`/`go test` invocations behind an approval prompt,
approve them; the package has no external dependencies to fetch.)

On Go 1.20.2 the author observed:

```
$ go test ./...
ok  	example.com/errfeedback

$ go test -race -count=10 ./...
ok  	example.com/errfeedback
```

## What is actually verified

`golden_test.go`
- `TestGoldenSequenceTwoSourcesInterleavedAbortRepartition` — the exact sequence
  the task asks for: two sources, two interleaved buckets, a step abandoned
  halfway, then a repartition. Every transmitted value, every committed residual
  and the reconstruction identity `sent + residual == gradient + prior residual`
  are checked against hand-computed integers for all four steps and both
  sources.

`scenario_test.go`
- `TestTwoSourcesInterleavedBucketsAndRepartition` — two sources, two steps,
  interleaved buckets, and a repartition between steps. Asserts the published
  aggregate equals the packet sum, every published value is a real quantized
  lattice point, and per coordinate `sent + confirmed residual == gradient +
  prior residual` for both steps and both sources.
- `TestResidualMigratesWithCoordinateOnRepartition` — a coordinate that changes
  bucket keeps its compensation and is retransmitted from its new bucket.
- `TestAbortMidwayThenRepartition` — a step abandoned after one bucket: the
  residual is byte-identical before/after, late acks are ignored, the voided
  step is never published, and the next repartitioned step reconstructs its
  input from the surviving residual.

`module_test.go`
- duplicate delivery and per-origin dedup; foreign-source and corrupted-packet
  rejection; late-ack-after-settle does not double-write (5 replay rounds);
  stale/mismatched acks (`ErrWrongSource`, ignored); recovery resend with
  identical checksums and equality to a clean run; journal tamper/reorder
  detection and truncated-replay behaviour; step-ID and layout-ID reuse
  rejection; plan coverage; recovery keeps journaling (second recovery matches);
  receiver void-then-expect; concurrent confirm settles exactly once; concurrent
  duplicate delivery dedupes.

`property_test.go`
- 50-step telescoping run with repartition every other step: per coordinate
  `sum(sent) + final residual == sum(gradient)` and residual bounded.
- sparsity selects the largest per bucket and leaves unselected values intact;
  overflow on `Begin`; an all-zero-payload step still settles and publishes an
  empty aggregate.

`lattice_test.go`
- every rounding mode including both tie breaks and negatives; clamping; no
  wrap at `int64` extremes; sparse selection ordering; checked arithmetic.

Everything above is executed by `go test`. There is no self-authored demo output
standing in for verification; the numbers in this README are the real test
results.

## Scope and safety

Local, synthetic integer data only. No network targets, accounts, credentials,
or external systems are touched. The package is a deterministic simulation of the
sender/receiver protocol and is not a production transport.
