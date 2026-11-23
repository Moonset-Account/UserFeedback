# pathmtu — sender-side Path MTU discovery across path generations

A dependency-free Go module (standard library only, Go 1.20.2) that models the
sender half of Path MTU Discovery for a packet connection whose path can change
("migrate" to a new generation).

## Public API

```go
type Config struct {
    MinPayload  int              // floor: always-sendable payload (>=1)
    MaxPayload  int              // initial ceiling for a fresh generation
    EvidenceTTL time.Duration    // reuse window when returning to a generation
    Now         func() time.Time // injectable clock; nil => time.Now
}

type Probe struct { ID uint64; Generation uint64; Size int }

type Result struct { Applied bool; Snapshot Snapshot }

type Snapshot struct {
    At               time.Time
    Generation       uint64
    Floor            int
    Verified         int   // verified lower bound
    Ceiling          int   // upper bound
    OrdinarySendSize int   // payload used for ordinary traffic == Verified
    NextProbe        int
    NextProbeValid   bool
    Revalidating     bool
    Kind             ChangeKind
    Reason           string
}
func (s Snapshot) String() string // one-line: ordinary size, next probe, reason

func New(cfg Config) (*Discoverer, error)

func (d *Discoverer) MigrateTo(gen uint64, minPayload int) Result
func (d *Discoverer) OnOrdinaryAck(gen uint64) Result
func (d *Discoverer) SendProbe() (Probe, bool)
func (d *Discoverer) OnProbeSuccess(id uint64) Result
func (d *Discoverer) OnProbeTimeout(id uint64) Result
func (d *Discoverer) OnProbeTooBig(id uint64, maxPayload int) Result

func (d *Discoverer) Current() Snapshot
func (d *Discoverer) NextProbe() (int, bool)
func (d *Discoverer) Trace() []Snapshot
func (d *Discoverer) PendingProbes() int
func (d *Discoverer) EvidenceFor(gen uint64) (Evidence, bool)
```

## Behaviour implemented (maps to the task requirements)

| Requirement | Implementation | Test |
|---|---|---|
| Success raises **only** the issuing generation's verified lower bound | `OnProbeSuccess` raises `verified` to probe size, capped by `ceiling` | `TestNormalProbeGrowth` |
| Size-too-big lowers the ceiling of that generation | `OnProbeTooBig` sets `ceiling = min(ceiling, limit)` (limit clamped to floor) | `TestTooBigLowersCeilingThenRevalidates` |
| New limit below verified ⇒ drop immediately and re-verify | verified dropped to limit, `Revalidating=true`, next probe = the limit | `TestTooBigLowersCeilingThenRevalidates` |
| Re-verification must not wedge if the confirmation probe is lost | confirmation probe lost but ordinary traffic at the limit acked ⇒ revalidated; lost with no liveness ⇒ retried at same size | `TestRevalidationDoesNotWedgeOnLostConfirmationProbe`, `TestRevalidationRetriesWhenNoLiveness` |
| Probe timeout with **no** ordinary ack in the same window ⇒ hold bounds | per-probe ack-sequence window; `ackSeq <= ackSeqAtSend` ⇒ `ChangeHeld`, streak reset | `TestTimeoutWithSimultaneousOrdinaryLossHolds`, `TestNoLivenessTimeoutResetsStreak` |
| Two **consecutive** probe timeouts with ordinary liveness ⇒ lower ceiling | second live timeout lowers ceiling to `failedProbe-1`, never below verified/floor | `TestTwoConsecutiveLiveTimeoutsLowerCeiling` |
| Migration: old feedback must not pollute the new generation | feedback routed by generation; retired-generation feedback recorded there, flagged `applied_elsewhere` | `TestMigrationIsolatesInFlightProbeFeedback`, `TestMigrationTooBigAndTimeoutFromOldGenIgnored` |
| Returning to an old path reuses only unexpired, generation-matching evidence | `EvidenceTTL` + generation match; stale *activation* (same gen, new activation) is rejected | `TestReturnToPathReusesUnexpiredEvidence`, `TestReturnToPathRejectsExpiredEvidence`, `TestStaleActivationProbeRejectedAfterReturn` |
| Output ordinary size, next probe and change reason at each instant | every event returns a `Snapshot`; `String()` renders it; `Trace()` keeps all of them | `TestSnapshotsExplainBoundaryChanges` |

### The tricky cases explicitly verified

1. **Probe round-trip across migration.** A probe is issued on generation 1,
   the connection migrates to generation 2, and the reply arrives afterwards.
   The success updates generation 1's stored evidence only; generation 2's
   bounds are untouched (`applied_elsewhere`). If the sender later returns to
   generation 1, a probe issued during the *earlier activation* is still
   rejected (activation counter, not just generation number).
2. **Probe lost together with ordinary packets.** The probe window records the
   ordinary-ack sequence at send time. If no ordinary ack arrived since, the
   timeout is unattributed: both bounds are held and the consecutive-timeout
   streak is reset. Only a timeout that coexists with ordinary liveness counts,
   and two of those in a row are required to lower the ceiling.

## Build / reproduce

From a clean workspace containing this module:

```sh
# Go 1.20.2, standard library only — no dependencies to download
go test ./...
go test ./... -v          # per-test output
go test ./... -run Example -v   # the deterministic timeline example
```

The example (`example_test.go`) prints, for a scripted timeline, the ordinary
send size, the next probe and the reason at each step. Its `// Output:` block is
checked by `go test`, so it is a real executable assertion, not a canned demo.

## Scope / verification notes

- Only this workspace and synthetic, local inputs are used. No network access,
  no external services, no installation, no credentials.
- `Discoverer` is intentionally **not** concurrency-safe; serialize calls at the
  transport. The clock is injectable so timing/expiry behaviour is
  deterministic.
- All behaviour above is exercised by `pathmtu_test.go` + `example_test.go` and
  validated by a real `go test ./...` run (see below).
