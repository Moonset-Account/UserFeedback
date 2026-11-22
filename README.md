# tailsample — windowed tail sampling over sharded span reports

A small Go 1.20.2 library (no third-party dependencies) that performs per-window
tail sampling over span records reported independently by several shards.

## Model

* A span has its own start time, a trace ID, an optional parent span ID inside
  the same trace, and an optional **link** that names *another trace*.
* **Window assignment** uses only the span's own start time. Windows are
  half-open `[Start, Start+Size)` anchored at `Epoch`.
* **Groups** are the connected components of "traces joined by links", computed
  over the records *present in the window being settled*:
  * every span of a trace joins the same group;
  * a link from trace `A` to trace `B` merges the two groups **only if** `B` has
    at least one record in this window;
  * a group is atomic — it is kept whole or dropped whole.
* **Ordering** of the groups inside a window is:
  1. groups containing at least one error span first;
  2. then by the group's maximum span duration, descending;
  3. then by the group's smallest trace ID, ascending.
* **Budget** is a per-window **span count** (`BudgetSpans`, negative = unlimited).
  Groups are visited in the order above. A group whose span count does not fit
  in the remaining budget is skipped *as a whole*, and later (smaller) groups may
  still be kept.
* **Settlement**: each shard reports a watermark, meaning "I will never report a
  span starting before this time again". A window settles once *every*
  configured shard has reached at least the window's end. Settlement is driven
  only by watermark reports (or the explicit `Settle` used by tests/shutdown).
* **Duplicates**: a span is identified by `(Trace, ID)`. Repeated reports are
  folded into the first one and counted once, whether or not the window has
  settled. A repeat that arrives after settlement is never re-applied.
* **Gaps**: a kept span whose parent cannot be resolved inside its own window
  (or whose link target trace has no records in this window) contributes an
  explicit `Gap`, so a partial group is distinguishable from a complete one.
  References that point outside the current window never merge groups.
* **Late reports**: a record that arrives for a window that is already settled
  (or permanently closed by watermarks) is appended to the late list with the
  shard, the watermark seen at arrival, and a reason
  (`post-settlement`, `watermark-violation`, `window-closed`,
  `duplicate-after-settlement`, `duplicate-after-close`). It never mutates a
  published window.
* **Stability**: published results are immutable; `Replay(window)` returns a byte
  identical copy including the 1-based settlement sequence `Seq`.

## Public API

```go
type Span struct {
    ID, Trace, Parent string
    Start             time.Time
    Duration          time.Duration
    Error             bool
    Link              string // ID of another trace
}

type Window struct{ Start time.Time; Size time.Duration }
func (w Window) End() time.Time
func (w Window) Contains(t time.Time) bool

type Config struct {
    Epoch       time.Time
    WindowSize  time.Duration
    BudgetSpans int      // negative = unlimited
    Shards      []string // complete, stable set of shards
}

func NewCollector(cfg Config) (*Collector, error)

func (c *Collector) Ingest(shard string, s Span) error
func (c *Collector) ReportWatermark(shard string, t time.Time) ([]Result, error)
func (c *Collector) Settle() []Result
func (c *Collector) WindowFor(t time.Time) (Window, error)
func (c *Collector) Results() []Result
func (c *Collector) Replay(w Window) (Result, error)
func (c *Collector) Late() []LateRecord
func (c *Collector) DuplicateCount() int
func (c *Collector) Watermarks() map[string]time.Time

type Result struct {
    Window      Window
    Seq         int
    Kept        []Span
    Dropped     []Span
    Gaps        []Gap
    UsedSpans   int
    BudgetSpans int
}

type Gap struct {
    Trace, SpanID, Reference string
    Kind   string // "parent" or "link"
    Reason string // "parent-missing", "parent-out-of-window",
                  // "link-target-missing", "link-out-of-window"
}

type LateRecord struct {
    Shard             string
    Span              Span
    Watermark         time.Time
    ViolatesWatermark bool
    WindowSettled     bool
    Reason            string
}
```

Errors: `ErrBeforeEpoch`, `ErrUnknownShard`, `ErrInvalidSpan`, `ErrNotSettled`,
`ErrBadWindow`. All `Collector` methods are safe for concurrent use.

## Reproduction

```sh
go vet ./...        # not run here: the sandbox declined `go vet`; run it yourself
go test ./...       # 34 tests, all passing on go1.20.2
go test -race ./...
```

## What the tests actually verify

Every test below asserts real behaviour (no canned demo output). Run
`go test -v ./...` to see them individually.

Functional:
- `TestSpanWindowedByOwnTime`, `TestWindowBoundaryIsHalfOpen` — placement by own
  start time, half-open boundaries.
- `TestSettlementRequiresAllShards`, `TestWatermarkIsMonotonic`,
  `TestExplicitSettleDoesNotPublishWithoutWatermarks` — the "all shards cross
  the end" gate and monotonic watermarks.
- `TestErrorGroupsFirstThenDurationThenTraceID`,
  `TestTraceIDBreaksEqualDurationTies`,
  `TestMaxDurationUsesLongestSpanInGroup` — selection order.
- `TestBudgetSkipsWholeGroupAndKeepsLaterSmallerGroup`,
  `TestBudgetNeverSplitsLinkedGroup`, `TestUnlimitedBudgetKeepsEverything` —
  atomic skipping and usage accounting.
- `TestLinkedTracesKeptOrDroppedTogether`, `TestLinkedGroupErrorPropagates`,
  `TestLinkChainUnionsThreeTraces`, `TestLinkToUnknownTraceLeavesGap`,
  `TestLinkOutsideWindowDoesNotMerge` — connected groups and link gaps.
- `TestMissingParentLeavesExplicitGap`, `TestParentInAnotherWindowIsGap`,
  `TestNoGapWhenParentPresentInWindow` — parent gaps and out-of-window refs.
- `TestDuplicatesCountOnceBeforeSettlement`,
  `TestDuplicateAfterSettlementIsLateAndNotReadmitted` — dedup semantics.
- `TestLateRecordAfterSettlementIsReportedSeparately`,
  `TestRecordForClosedEmptyWindowIsLate`,
  `TestLateStragglerInOpenWindowDoesNotBlockSettlement`,
  `TestWatermarkViolationBeforeSettlementIsStillUsed` — late handling.
- `TestReplayErrors`, `TestReplaySequenceStableAcrossWindows`,
  `TestResultsAreCopies` — replay stability and immutability.
- `TestNewCollectorValidation`, `TestIngestRejectsBadInput` — input validation.

Boundary / interleaved:
- `TestInterleavedThreeShardScenario` — the acceptance scenario: three shards
  interleave a duplicate, an error, a linked pair, a span for the next window,
  and staggered watermarks; it asserts exactly which window settles when, the
  kept/dropped sets, span usage against the budget, the late detail of a
  post-settlement straggler, and a stable replay of window 0.
- `TestArrivalOrderDoesNotChangeResult` — same reports replayed in forward,
  reverse and shuffled cross-shard order produce identical kept/dropped/gap sets
  and usage.
- `TestConcurrentIngestAndWatermarks` (also run under `-race`) — concurrent
  ingestion from three shards, then deterministic settlement; every span is
  accounted for exactly once.

## Explicitly not verified here

- `go vet ./...` was not executed (the sandbox required approval for it); it is
  listed under reproduction for you to run.
- Real network, real collector backends, persistence of published results, and
  clock skew between shards are out of scope: the library operates on in-memory
  synthetic records only.
- The budget is a span count, not a serialized byte size; byte-accurate payload
  accounting is not modelled.
