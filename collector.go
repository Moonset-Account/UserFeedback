package tailsample

import (
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

var (
	// ErrBeforeEpoch is returned when a span starts before the collector epoch.
	ErrBeforeEpoch = errors.New("tailsample: span start is before epoch")
	// ErrUnknownShard is returned when a shard was not registered in Config.
	ErrUnknownShard = errors.New("tailsample: unknown shard")
	// ErrInvalidSpan is returned for malformed span reports.
	ErrInvalidSpan = errors.New("tailsample: invalid span")
	// ErrNotSettled is returned by Replay for an unpublished window.
	ErrNotSettled = errors.New("tailsample: window not settled")
	// ErrBadWindow is returned when a window is not aligned with the epoch.
	ErrBadWindow = errors.New("tailsample: window is not aligned with the epoch")
)

// Config configures a Collector.
//
// Epoch anchors window index 0. WindowSize must be positive. Shards must be the
// complete, stable set of shards feeding the collector: a window settles only
// once every shard has advanced its watermark to at least the window end.
// BudgetSpans is the per-window span budget; a negative value means unlimited.
type Config struct {
	Epoch       time.Time
	WindowSize  time.Duration
	BudgetSpans int
	Shards      []string
}

// Collector performs windowed tail sampling over spans from several shards.
// All methods are safe for concurrent use. Spans never settle a window on their
// own: settlement is driven exclusively by watermark reports (or an explicit
// Settle call for tests and shutdown).
type Collector struct {
	mu  sync.Mutex
	cfg Config

	shards map[string]struct{}
	wm     map[string]time.Time
	wmSeen map[string]bool

	buffer     map[int64][]Span
	nextWindow int64

	seen         map[spanKey]int64
	traceWindows map[traceID]map[int64]struct{}
	dup          map[spanKey]int

	published map[int64]Result
	order     []int64
	nextSeq   int

	late []LateRecord
}

type spanKey struct {
	trace traceID
	id    string
}

// NewCollector validates cfg and returns an empty Collector.
func NewCollector(cfg Config) (*Collector, error) {
	if cfg.Epoch.IsZero() {
		return nil, errors.New("tailsample: epoch must be set")
	}
	if cfg.WindowSize <= 0 {
		return nil, fmt.Errorf("tailsample: window size must be positive, got %v", cfg.WindowSize)
	}
	if len(cfg.Shards) == 0 {
		return nil, errors.New("tailsample: at least one shard is required")
	}
	c := &Collector{
		cfg:          cfg,
		shards:       make(map[string]struct{}, len(cfg.Shards)),
		wm:           make(map[string]time.Time, len(cfg.Shards)),
		wmSeen:       make(map[string]bool, len(cfg.Shards)),
		buffer:       make(map[int64][]Span),
		seen:         make(map[spanKey]int64),
		traceWindows: make(map[traceID]map[int64]struct{}),
		dup:          make(map[spanKey]int),
		published:    make(map[int64]Result),
	}
	for _, s := range cfg.Shards {
		if s == "" {
			return nil, errors.New("tailsample: empty shard name")
		}
		if _, exists := c.shards[s]; exists {
			return nil, fmt.Errorf("tailsample: duplicate shard %q", s)
		}
		c.shards[s] = struct{}{}
	}
	return c, nil
}

// Ingest reports one span from shard. Spans are assigned to a window by their
// own Start. A report that arrives after its window is closed is recorded as
// late and never mutates a published window.
func (c *Collector) Ingest(shard string, s Span) error {
	if s.Trace == "" || s.ID == "" {
		return ErrInvalidSpan
	}
	if s.Duration < 0 {
		return fmt.Errorf("%w: negative duration on span %q", ErrInvalidSpan, s.ID)
	}
	if s.Start.Before(c.cfg.Epoch) {
		return ErrBeforeEpoch
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	if _, ok := c.shards[shard]; !ok {
		return fmt.Errorf("%w: %q", ErrUnknownShard, shard)
	}

	key := spanKey{trace: traceID(s.Trace), id: s.ID}
	idx := c.indexFrom(s.Start)
	observedWM, hasWM := c.wm[shard], c.wmSeen[shard]
	violates := hasWM && s.Start.Before(observedWM)
	closed := c.closedLocked(idx)

	if _, dup := c.seen[key]; dup {
		// A repeated span is counted once and never re-admitted. If the repeat
		// arrives after the window closed it is also reported as late.
		c.dup[key]++
		if closed {
			_, published := c.published[idx]
			reason := "duplicate-after-settlement"
			if !published {
				reason = "duplicate-after-close"
			}
			c.recordLateLocked(shard, s, observedWM, violates, published, reason)
		}
		return nil
	}

	if closed {
		_, published := c.published[idx]
		reason := "watermark-violation"
		if !violates {
			if published {
				reason = "post-settlement"
			} else {
				reason = "window-closed"
			}
		}
		c.recordLateLocked(shard, s, observedWM, violates, published, reason)
		return nil
	}

	c.buffer[idx] = append(c.buffer[idx], s)
	c.seen[key] = idx
	if c.traceWindows[key.trace] == nil {
		c.traceWindows[key.trace] = make(map[int64]struct{})
	}
	c.traceWindows[key.trace][idx] = struct{}{}
	// A pre-settlement record that contradicts its shard's watermark is still
	// used because the window is open. It is not a late record: late reporting
	// is defined as arrival after settlement.
	return nil
}

func (c *Collector) recordLateLocked(shard string, s Span, wm time.Time, violates, settled bool, reason string) {
	c.late = append(c.late, LateRecord{
		Shard:             shard,
		Span:              s,
		Watermark:         wm,
		ViolatesWatermark: violates,
		WindowSettled:     settled,
		Reason:            reason,
	})
}

// ReportWatermark records that shard will never again report a span starting
// before t. Watermarks are monotonic per shard; an older value is ignored.
// Every window whose end all shards have reached is settled, in window order.
func (c *Collector) ReportWatermark(shard string, t time.Time) ([]Result, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.shards[shard]; !ok {
		return nil, fmt.Errorf("%w: %q", ErrUnknownShard, shard)
	}
	if c.wmSeen[shard] && !t.After(c.wm[shard]) {
		return nil, nil
	}
	c.wm[shard] = t
	c.wmSeen[shard] = true
	return c.settleReadyLocked(), nil
}

// Settle publishes every currently eligible window. It exists for tests and
// graceful shutdown; normal operation settles on ReportWatermark.
func (c *Collector) Settle() []Result {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.settleReadyLocked()
}

// WindowFor returns the window that owns t.
func (c *Collector) WindowFor(t time.Time) (Window, error) {
	if t.Before(c.cfg.Epoch) {
		return Window{}, ErrBeforeEpoch
	}
	return c.windowAt(c.indexFrom(t)), nil
}

// Results returns deep copies of published results in settlement order.
func (c *Collector) Results() []Result {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]Result, 0, len(c.order))
	for _, idx := range c.order {
		out = append(out, copyResult(c.published[idx]))
	}
	return out
}

// Replay returns the published result for w, identical (including Seq) to the
// original settlement. It returns ErrNotSettled if w was never published and
// ErrBadWindow if w.Start is not aligned with the epoch or its size differs.
func (c *Collector) Replay(w Window) (Result, error) {
	if w.Size != c.cfg.WindowSize {
		return Result{}, fmt.Errorf("%w: size %v != %v", ErrBadWindow, w.Size, c.cfg.WindowSize)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	idx := c.indexFrom(w.Start)
	if c.windowAt(idx).Start != w.Start {
		return Result{}, fmt.Errorf("%w: start %v", ErrBadWindow, w.Start)
	}
	r, ok := c.published[idx]
	if !ok {
		return Result{}, fmt.Errorf("%w: %v", ErrNotSettled, w.Start)
	}
	return copyResult(r), nil
}

// Late returns late reports in arrival order.
func (c *Collector) Late() []LateRecord {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]LateRecord, len(c.late))
	copy(out, c.late)
	return out
}

// DuplicateCount is the total number of repeated span reports folded into the
// first occurrence.
func (c *Collector) DuplicateCount() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := 0
	for _, v := range c.dup {
		n += v
	}
	return n
}

// Watermarks returns the last reported watermark per shard.
func (c *Collector) Watermarks() map[string]time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make(map[string]time.Time, len(c.wm))
	for k, v := range c.wm {
		out[k] = v
	}
	return out
}

func (c *Collector) indexFrom(t time.Time) int64 {
	return int64(t.Sub(c.cfg.Epoch) / c.cfg.WindowSize)
}

func (c *Collector) windowAt(idx int64) Window {
	start := c.cfg.Epoch.Add(time.Duration(idx) * c.cfg.WindowSize)
	return Window{Start: start, Size: c.cfg.WindowSize}
}

// closedLocked reports whether idx can no longer accept spans.
func (c *Collector) closedLocked(idx int64) bool {
	if idx < c.nextWindow {
		return true
	}
	if _, ok := c.published[idx]; ok {
		return true
	}
	return c.allShardsPassedLocked(c.windowAt(idx).End())
}

func (c *Collector) allShardsPassedLocked(end time.Time) bool {
	for shard := range c.shards {
		if !c.wmSeen[shard] || c.wm[shard].Before(end) {
			return false
		}
	}
	return true
}

// settleReadyLocked publishes all buffered windows that are fully past every
// shard's watermark, in ascending window order.
func (c *Collector) settleReadyLocked() []Result {
	if len(c.buffer) == 0 {
		return nil
	}
	idxs := make([]int64, 0, len(c.buffer))
	for idx := range c.buffer {
		if idx >= c.nextWindow {
			idxs = append(idxs, idx)
		}
	}
	sort.Slice(idxs, func(i, j int) bool { return idxs[i] < idxs[j] })

	var out []Result
	for _, idx := range idxs {
		if !c.allShardsPassedLocked(c.windowAt(idx).End()) {
			// A missing earlier window blocks later ones so Seq order matches
			// window order.
			break
		}
		res := c.buildResultLocked(idx)
		c.nextSeq++
		res.Seq = c.nextSeq
		res.BudgetSpans = c.cfg.BudgetSpans
		c.published[idx] = res
		c.order = append(c.order, idx)
		delete(c.buffer, idx)
		if idx >= c.nextWindow {
			c.nextWindow = idx + 1
		}
		out = append(out, copyResult(res))
	}
	return out
}

func (c *Collector) buildResultLocked(idx int64) Result {
	w := c.windowAt(idx)
	spans := c.buffer[idx]

	byTrace := make(map[traceID][]*Span, len(spans))
	for i := range spans {
		s := &spans[i]
		byTrace[traceID(s.Trace)] = append(byTrace[traceID(s.Trace)], s)
	}

	uf := make(map[traceID]traceID, len(byTrace))
	var find func(traceID) traceID
	find = func(x traceID) traceID {
		p, ok := uf[x]
		if !ok {
			uf[x] = x
			return x
		}
		if p != x {
			uf[x] = find(p)
			return uf[x]
		}
		return x
	}
	// Links merge traces only when the referenced trace has records in this
	// window. A reference outside the window cannot merge and becomes a gap.
	for _, s := range spans {
		if s.Link == "" || traceID(s.Link) == traceID(s.Trace) {
			continue
		}
		if _, present := byTrace[traceID(s.Link)]; !present {
			continue
		}
		a, b := find(traceID(s.Trace)), find(traceID(s.Link))
		if a == b {
			continue
		}
		if a < b {
			uf[b] = a
		} else {
			uf[a] = b
		}
	}

	components := make(map[traceID]*group, len(byTrace))
	for trace, ts := range byTrace {
		root := find(trace)
		g, ok := components[root]
		if !ok {
			g = &group{order: trace}
			components[root] = g
		}
		if trace < g.order {
			g.order = trace
		}
		for _, s := range ts {
			g.addSpan(s)
		}
	}

	groups := make([]*group, 0, len(components))
	for _, g := range components {
		g.finalize()
		groups = append(groups, g)
	}
	sort.Slice(groups, func(i, j int) bool {
		a, b := groups[i], groups[j]
		if a.hasError != b.hasError {
			return a.hasError
		}
		if a.maxDur != b.maxDur {
			return a.maxDur > b.maxDur
		}
		return a.order < b.order
	})

	remaining := c.cfg.BudgetSpans
	unlimited := remaining < 0
	var kept, dropped []Span
	for _, g := range groups {
		cost := groupCost(g)
		if unlimited || cost <= remaining {
			if !unlimited {
				remaining -= cost
			}
			for _, s := range g.spans {
				kept = append(kept, *s)
			}
			continue
		}
		// Groups are atomic: a group that does not fit is skipped whole, and
		// smaller groups that follow may still be kept.
		for _, s := range g.spans {
			dropped = append(dropped, *s)
		}
	}

	res := Result{
		Window:    w,
		Kept:      kept,
		Dropped:   dropped,
		UsedSpans: len(kept),
	}

	for _, s := range kept {
		if s.Parent != "" && s.Parent != s.ID {
			pk := spanKey{trace: traceID(s.Trace), id: s.Parent}
			if pIdx, ok := c.seen[pk]; !ok {
				res.Gaps = append(res.Gaps, Gap{
					Trace: s.Trace, SpanID: s.ID, Reference: s.Parent,
					Kind: "parent", Reason: "parent-missing",
				})
			} else if pIdx != idx {
				res.Gaps = append(res.Gaps, Gap{
					Trace: s.Trace, SpanID: s.ID, Reference: s.Parent,
					Kind: "parent", Reason: "parent-out-of-window",
				})
			}
		}
		if s.Link != "" && traceID(s.Link) != traceID(s.Trace) {
			windows, ok := c.traceWindows[traceID(s.Link)]
			if !ok {
				res.Gaps = append(res.Gaps, Gap{
					Trace: s.Trace, SpanID: s.ID, Reference: s.Link,
					Kind: "link", Reason: "link-target-missing",
				})
			} else if _, here := windows[idx]; !here {
				res.Gaps = append(res.Gaps, Gap{
					Trace: s.Trace, SpanID: s.ID, Reference: s.Link,
					Kind: "link", Reason: "link-out-of-window",
				})
			}
		}
	}
	sort.Slice(res.Gaps, func(i, j int) bool {
		a, b := res.Gaps[i], res.Gaps[j]
		if a.Trace != b.Trace {
			return a.Trace < b.Trace
		}
		if a.SpanID != b.SpanID {
			return a.SpanID < b.SpanID
		}
		if a.Kind != b.Kind {
			return a.Kind < b.Kind
		}
		if a.Reference != b.Reference {
			return a.Reference < b.Reference
		}
		return a.Reason < b.Reason
	})
	return res
}

func copyResult(r Result) Result {
	r.Kept = append([]Span(nil), r.Kept...)
	r.Dropped = append([]Span(nil), r.Dropped...)
	r.Gaps = append([]Gap(nil), r.Gaps...)
	return r
}
