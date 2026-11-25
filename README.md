# entropygate

Streaming health gate for the two independent raw-entropy lanes of a hardware
security module, sitting between the raw sources and the key generator.

Each lane is checked by two caller-parameterised tests:

1. **Repetition count test** — the length of the current run of consecutive
   identical symbols. Reaching `RepetitionThreshold` isolates the lane
   immediately, mid-window if needed. The run counter is deliberately **not**
   cleared when a fixed window rolls over, and it continues across `Feed`
   calls.
2. **Fixed-window first-symbol test** — symbols are grouped into
   non-overlapping windows of `WindowSize`. When the number of occurrences of
   the window's own first symbol reaches `FirstSymbolThreshold`, the lane is
   isolated. The check is made as the window is built, so a window can fail
   before it is complete, and it can fail in a later `Feed` call than the one
   that supplied the window header.

Entropy is only ever released one complete window at a time. A window is
buffered until it is complete **and** has passed both tests, so a window
header that is only later judged unhealthy is never returned to the caller.
Windows completed before an isolation point in the same chunk are still
released; the window in which isolation occurs, and the rest of that chunk,
are discarded.

Isolation is latched. A latched lane rejects input with `ErrLaneIsolated`
until `Reset` is called. After an explicit `Reset`, each lane must pass
`ResetCleanWindows` complete windows before it may release entropy again
(those probation windows establish health but their symbols are not
forwarded). Failure during probation latches the lane again. `Reset` affects
both lanes and discards any partial windows.

The lanes are fully independent: isolating one lane does not stop, buffer,
reset, or otherwise alter the other.

## Public API

Package path: `entropygate` (module `entropygate`, imported as `entropygate`).

```go
type Lane uint8
const (
    LaneA Lane = 0
    LaneB Lane = 1
)
func (l Lane) String() string

type Config struct {
    RepetitionThreshold  int // > 0, consecutive identical symbols that isolate
    WindowSize           int // > 0, symbols per fixed window
    FirstSymbolThreshold int // > 0, occurrences of the window's first symbol
    ResetCleanWindows    int // >= 0, clean windows required after Reset
}

type Outcome struct {
    Lane               Lane
    Accepted           int    // symbols consumed from this chunk
    Released           []byte // complete healthy windows that may be forwarded
    Isolated           bool   // lane latched when Feed returned
    IsolationPoint     int    // 1-based index in this chunk, -1 if none
    ProbationRemaining int    // further clean windows required
}

type Status struct {
    Isolated           bool
    ProbationRemaining int
    Active             bool // !Isolated && ProbationRemaining == 0
    Buffered           int  // symbols in the incomplete window
    RunLength          int  // length of the trailing identical-symbol run
}

func New(cfg Config) (*Gate, error)
func (g *Gate) Feed(lane Lane, symbols []byte) (Outcome, error)
func (g *Gate) Reset()
func (g *Gate) Status(lane Lane) (Status, error)

var (
    ErrInvalidConfig error
    ErrInvalidLane   error
    ErrLaneIsolated  error
)
```

`Feed` accepts chunks of any size (including empty) and the same byte stream
may be split arbitrarily. Its result — the released prefix, the consumed
count and the isolation point — is invariant under chunking. `Gate` is safe
for concurrent use; each lane has its own lock.

Thresholds of `1` are legal. `FirstSymbolThreshold > WindowSize` is legal and
means that test never fires. `ResetCleanWindows == 0` makes a reset lane
immediately usable.

## Running

```sh
go test ./...            # unit, table, chunking and example tests
go test -race ./...      # concurrency test under the race detector
go test -fuzz=FuzzChunkingInvariance -fuzztime=20s .
```

Requires Go 1.20.2 (no external dependencies).

## Verification scope

All data is synthetic, generated in the tests. Everything below was executed
locally.

| Area | Tests |
| --- | --- |
| Config validation (zero/negative thresholds, boundary-legal configs) | `TestNewRejectsInvalidConfig`, `TestNewAcceptsBoundaryConfig` |
| Only complete passing windows released, partials buffered | `TestReleasesOnlyCompletePassingWindows`, `TestWindowSizeOne` |
| Run count spans a window boundary | `TestRepetitionCountSpansWindowBoundary` |
| Run count spans two reads inside one window | `TestRepetitionCountSpansTwoReadsWithinOneWindow` |
| Window header not leaked when the failure is only visible at window end / in a later read | `TestWindowHeaderNotLeakedWhenFailureFoundAtWindowEnd`, `TestWindowFailureDetectedInLaterRead`, `ExampleGate` |
| Window failure detectable before window end | `TestWindowFailureDetectedBeforeWindowEnd` |
| Windows before the isolation point in one chunk are released; later bytes are not consumed | `TestWindowsBeforeIsolationInSameChunkAreReleased` |
| `Released` does not alias the internal buffer | `TestReleasedSliceNotAliasedToInternalBuffer` |
| Lane independence (isolation, blocking, no cross-revival) | `TestLanesAreIndependent`, `TestConcurrentLanes` |
| Reset probation, cross-feed probation windows, re-latch on failure, zero probation | `TestResetRequiresCleanWindowsBeforeRelease`, `TestFailureDuringProbationRelatches`, `TestResetWithZeroCleanWindowsActivatesImmediately`, `TestProbationAndActiveWindowsInOneFeed`, `TestResetClearsIsolationForBothLanes`, `TestIsolatedLaneReportsZeroProbation` |
| Reset discards partial windows | `TestResetDiscardsPartialWindowOnHealthyLane` |
| Threshold edge values (1, above window size) | `TestThresholdOneIsolatesImmediately`, `TestFirstSymbolThresholdOneIsolatesOnWindowHeader`, `TestFirstSymbolThresholdAboveWindowSizeNeverFires` |
| Empty feed, invalid lane, stable isolation error that consumes nothing | `TestEmptyFeedIsNoOp`, `TestInvalidLaneRejected`, `TestIsolationErrorIsStableAndDoesNotConsume` |
| Arbitrary chunking equivalence (every 1- and 2-cut split, byte-by-byte) | `TestArbitraryChunkingMatchesWholeStream`, `TestChunkingWithCrossWindowRepetitionAndProbation` |
| Randomised chunking invariant (whole stream vs. two cuts, before and after reset) | `FuzzChunkingInvariance` |

Recorded results on 2026-11-26:

- `go test ./...` — PASS, coverage 100.0% of statements.
- `go test -race -count=1 ./...` — PASS.
- `go test -fuzz=FuzzChunkingInvariance -fuzztime=20s .` — PASS after
  ~3.5M executions, no failing input.

Limitations: the gate implements only the two health tests above (no other
statistical tests, no key-generator integration, no persistence). "Reset"
is an in-process call; wiring it to a physical device reset is out of scope.
