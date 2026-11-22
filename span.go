package tailsample

import (
	"sort"
	"time"
)

// traceID is the canonical trace identifier used for deterministic tie-breaking.
type traceID string

// Span is one immutable record reported by the collection side.
//
// Start is the span's own start time and is the only field used to assign it to
// a window. Parent names a span in the same trace. Link holds the id of another
// trace: linked traces belong to one connected group and are kept or dropped
// together. A link is only resolved against records of the same window.
type Span struct {
	ID       string
	Trace    string
	Parent   string
	Start    time.Time
	Duration time.Duration
	Error    bool
	Link     string
}

// Window is a half-open time window [Start, Start+Size).
type Window struct {
	Start time.Time
	Size  time.Duration
}

// Contains reports whether t falls inside the window.
func (w Window) Contains(t time.Time) bool {
	return !t.Before(w.Start) && t.Before(w.End())
}

// End is the exclusive end of the window.
func (w Window) End() time.Time { return w.Start.Add(w.Size) }

// group is a connected component of spans, computed over trace membership and
// span links that resolve inside the window being settled.
type group struct {
	spans    []*Span
	hasError bool
	maxDur   time.Duration
	order    traceID // smallest trace id in the group
}

func (g *group) addSpan(s *Span) {
	g.spans = append(g.spans, s)
	if s.Error {
		g.hasError = true
	}
	if s.Duration > g.maxDur {
		g.maxDur = s.Duration
	}
}

// finalize sorts spans so a result is independent of arrival interleaving.
func (g *group) finalize() {
	sort.Slice(g.spans, func(i, j int) bool {
		a, b := g.spans[i], g.spans[j]
		if a.Trace != b.Trace {
			return a.Trace < b.Trace
		}
		if !a.Start.Equal(b.Start) {
			return a.Start.Before(b.Start)
		}
		return a.ID < b.ID
	})
}

// LateRecord is a span report that arrived after its owning window was settled
// (or was permanently closed by watermarks) and therefore could not be applied.
// A record that arrives before settlement is always used, even if it contradicts
// a shard watermark; it is not late reporting.
type LateRecord struct {
	Shard             string
	Span              Span
	Watermark         time.Time // last watermark known for the shard at arrival
	ViolatesWatermark bool      // Span.Start is before Watermark
	WindowSettled     bool      // the owning window had a published result
	Reason            string    // "post-settlement", "watermark-violation" or "window-closed"
}

// Gap is an explicit hole left in a kept group. Every kept span whose parent or
// link target cannot be resolved inside the window contributes one Gap, so a
// consumer can tell a partial group from a complete one.
type Gap struct {
	Trace     string
	SpanID    string
	Reference string
	Kind      string // "parent" or "link"
	Reason    string // "parent-missing", "parent-out-of-window", "link-target-missing", "link-out-of-window"
}

// Result is the immutable outcome of settling one window.
type Result struct {
	Window      Window
	Seq         int // 1-based settlement order; stable across replays
	Kept        []Span
	Dropped     []Span
	Gaps        []Gap
	UsedSpans   int // number of spans retained in this window
	BudgetSpans int // per-window span budget; negative means unlimited
}

// groupCost is the number of spans a group consumes from the window budget.
// Groups are billed atomically: a group's whole span count must fit.
func groupCost(g *group) int { return len(g.spans) }
