package tailsample

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

var epoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

func at(ms int) time.Time { return epoch.Add(time.Duration(ms) * time.Millisecond) }

func newTestCollector(t *testing.T, budget int, shards ...string) *Collector {
	t.Helper()
	if len(shards) == 0 {
		shards = []string{"a", "b", "c"}
	}
	c, err := NewCollector(Config{
		Epoch:       epoch,
		WindowSize:  time.Second,
		BudgetSpans: budget,
		Shards:      shards,
	})
	if err != nil {
		t.Fatalf("NewCollector: %v", err)
	}
	return c
}

func mustIngest(t *testing.T, c *Collector, shard string, s Span) {
	t.Helper()
	if err := c.Ingest(shard, s); err != nil {
		t.Fatalf("Ingest(%s, %s): %v", shard, s.ID, err)
	}
}

func span(trace, id, parent string, startMs int, dur time.Duration, isErr bool) Span {
	return Span{ID: id, Trace: trace, Parent: parent, Start: at(startMs), Duration: dur, Error: isErr}
}

func ids(spans []Span) []string {
	out := make([]string, len(spans))
	for i, s := range spans {
		out[i] = s.ID
	}
	return out
}

func traceOrder(spans []Span) []string {
	out := make([]string, 0, len(spans))
	seen := map[string]bool{}
	for _, s := range spans {
		if !seen[s.Trace] {
			seen[s.Trace] = true
			out = append(out, s.Trace)
		}
	}
	return out
}

func findByID(spans []Span, id string) (Span, bool) {
	for _, s := range spans {
		if s.ID == id {
			return s, true
		}
	}
	return Span{}, false
}

// --- configuration ---------------------------------------------------------

func TestNewCollectorValidation(t *testing.T) {
	cases := []struct {
		name string
		cfg  Config
	}{
		{"zero epoch", Config{WindowSize: time.Second, Shards: []string{"a"}}},
		{"zero window", Config{Epoch: epoch, Shards: []string{"a"}}},
		{"negative window", Config{Epoch: epoch, WindowSize: -time.Second, Shards: []string{"a"}}},
		{"no shards", Config{Epoch: epoch, WindowSize: time.Second}},
		{"empty shard", Config{Epoch: epoch, WindowSize: time.Second, Shards: []string{""}}},
		{"duplicate shard", Config{Epoch: epoch, WindowSize: time.Second, Shards: []string{"a", "a"}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := NewCollector(tc.cfg); err == nil {
				t.Fatalf("expected error for %s, got nil", tc.name)
			}
		})
	}
}

func TestIngestRejectsBadInput(t *testing.T) {
	c := newTestCollector(t, -1)
	if err := c.Ingest("a", Span{ID: "1"}); !errors.Is(err, ErrInvalidSpan) {
		t.Fatalf("missing trace: got %v", err)
	}
	if err := c.Ingest("a", Span{Trace: "t"}); !errors.Is(err, ErrInvalidSpan) {
		t.Fatalf("missing id: got %v", err)
	}
	if err := c.Ingest("a", span("t", "1", "", 0, -time.Millisecond, false)); !errors.Is(err, ErrInvalidSpan) {
		t.Fatalf("negative duration: got %v", err)
	}
	if err := c.Ingest("a", span("t", "1", "", -1, time.Millisecond, false)); !errors.Is(err, ErrBeforeEpoch) {
		t.Fatalf("before epoch: got %v", err)
	}
	if err := c.Ingest("zzz", span("t", "1", "", 0, time.Millisecond, false)); !errors.Is(err, ErrUnknownShard) {
		t.Fatalf("unknown shard: got %v", err)
	}
	if _, err := c.ReportWatermark("zzz", at(1000)); !errors.Is(err, ErrUnknownShard) {
		t.Fatalf("unknown shard watermark: got %v", err)
	}
}

// --- window assignment -----------------------------------------------------

func TestSpanWindowedByOwnTime(t *testing.T) {
	c := newTestCollector(t, -1, "a")
	// A child that started in window 0 but whose parent starts in window 1.
	mustIngest(t, c, "a", span("t1", "child", "root", 900, 50*time.Millisecond, false))
	mustIngest(t, c, "a", span("t1", "root", "", 1000, 120*time.Millisecond, false))

	if _, err := c.ReportWatermark("a", at(2000)); err != nil {
		t.Fatal(err)
	}
	results := c.Results()
	if len(results) != 2 {
		t.Fatalf("expected 2 windows, got %d", len(results))
	}
	w0, w1 := results[0], results[1]
	if w0.Window.Start != at(0) || w1.Window.Start != at(1000) {
		t.Fatalf("windows out of order: %v %v", w0.Window.Start, w1.Window.Start)
	}
	if got := ids(w0.Kept); len(got) != 1 || got[0] != "child" {
		t.Fatalf("window 0 kept %v, want [child]", got)
	}
	if got := ids(w1.Kept); len(got) != 1 || got[0] != "root" {
		t.Fatalf("window 1 kept %v, want [root]", got)
	}
}

func TestWindowBoundaryIsHalfOpen(t *testing.T) {
	c := newTestCollector(t, -1, "a")
	mustIngest(t, c, "a", span("t", "last", "", 999, time.Millisecond, false))
	mustIngest(t, c, "a", span("t", "next", "", 1000, time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(2000)); err != nil {
		t.Fatal(err)
	}
	results := c.Results()
	if len(results) != 2 {
		t.Fatalf("expected 2 windows, got %d", len(results))
	}
	if got := ids(results[0].Kept); len(got) != 1 || got[0] != "last" {
		t.Fatalf("window 0 = %v", got)
	}
	if got := ids(results[1].Kept); len(got) != 1 || got[0] != "next" {
		t.Fatalf("window 1 = %v", got)
	}
}

// --- settlement gating on three shards ------------------------------------

func TestSettlementRequiresAllShards(t *testing.T) {
	c := newTestCollector(t, -1) // a, b, c
	mustIngest(t, c, "a", span("t", "s1", "", 100, time.Millisecond, false))

	// Two of three shards past the window end: not settled yet.
	if res, err := c.ReportWatermark("a", at(1000)); err != nil || len(res) != 0 {
		t.Fatalf("a watermark: res=%d err=%v", len(res), err)
	}
	if res, err := c.ReportWatermark("b", at(5000)); err != nil || len(res) != 0 {
		t.Fatalf("b watermark: res=%d err=%v", len(res), err)
	}
	if got := len(c.Results()); got != 0 {
		t.Fatalf("settled early with %d results", got)
	}

	// The last shard crosses the end.
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil {
		t.Fatal(err)
	}
	if len(res) != 1 || res[0].Window.Start != at(0) {
		t.Fatalf("expected window 0 to settle, got %+v", res)
	}
	if res[0].Seq != 1 {
		t.Fatalf("first settlement seq = %d, want 1", res[0].Seq)
	}
}

func TestWatermarkIsMonotonic(t *testing.T) {
	c := newTestCollector(t, -1)
	if _, err := c.ReportWatermark("a", at(3000)); err != nil {
		t.Fatal(err)
	}
	// Older watermark must be ignored, and must not un-settle anything.
	if res, err := c.ReportWatermark("a", at(100)); err != nil || res != nil {
		t.Fatalf("older watermark: res=%v err=%v", res, err)
	}
	if got := c.Watermarks()["a"]; !got.Equal(at(3000)) {
		t.Fatalf("watermark regressed to %v", got)
	}
	if got := c.Watermarks()["b"]; !got.IsZero() {
		t.Fatalf("b should have no watermark, got %v", got)
	}
}

// --- duplicate handling ----------------------------------------------------

func TestDuplicatesCountOnceBeforeSettlement(t *testing.T) {
	c := newTestCollector(t, -1)
	s := span("t", "s1", "", 100, 10*time.Millisecond, true)
	mustIngest(t, c, "a", s)
	mustIngest(t, c, "b", s) // same span reported by another shard
	mustIngest(t, c, "a", s) // replay within the same shard

	if got := c.DuplicateCount(); got != 2 {
		t.Fatalf("DuplicateCount = %d, want 2", got)
	}
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil {
		t.Fatal(err)
	}
	if len(res) != 1 {
		t.Fatalf("expected 1 result, got %d", len(res))
	}
	if got := ids(res[0].Kept); len(got) != 1 || got[0] != "s1" {
		t.Fatalf("kept = %v, want exactly one s1", got)
	}
}

// --- ordering within a window ---------------------------------------------

func TestErrorGroupsFirstThenDurationThenTraceID(t *testing.T) {
	c := newTestCollector(t, -1) // unlimited, so only ordering is observed
	// tA: slow, no error. tB: slow, error. tC: fast, no error.
	mustIngest(t, c, "a", span("tA", "a1", "", 100, 900*time.Millisecond, false))
	mustIngest(t, c, "a", span("tB", "b1", "", 200, 100*time.Millisecond, true))
	mustIngest(t, c, "a", span("tC", "c1", "", 300, 50*time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil {
		t.Fatal(err)
	}
	if len(res) != 1 {
		t.Fatalf("expected 1 result")
	}
	if got := traceOrder(res[0].Kept); len(got) != 3 || got[0] != "tB" || got[1] != "tA" || got[2] != "tC" {
		t.Fatalf("group order = %v, want [tB tA tC]", got)
	}
}

func TestTraceIDBreaksEqualDurationTies(t *testing.T) {
	c := newTestCollector(t, -1)
	for _, tr := range []string{"tC", "tA", "tB"} {
		mustIngest(t, c, "a", span(tr, tr+"1", "", 100, 10*time.Millisecond, false))
	}
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	got := traceOrder(res[0].Kept)
	want := []string{"tA", "tB", "tC"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("tie order = %v, want %v", got, want)
		}
	}
}

func TestMaxDurationUsesLongestSpanInGroup(t *testing.T) {
	c := newTestCollector(t, -1)
	// tLong's longest span is 500ms; tSlow's single span is 400ms. Even though
	// tSlow has a longer *first* span, the group max governs ordering.
	mustIngest(t, c, "a", span("tLong", "l1", "", 100, 10*time.Millisecond, false))
	mustIngest(t, c, "a", span("tLong", "l2", "", 150, 500*time.Millisecond, false))
	mustIngest(t, c, "a", span("tSlow", "s1", "", 200, 400*time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	got := traceOrder(res[0].Kept)
	if got[0] != "tLong" || got[1] != "tSlow" {
		t.Fatalf("order = %v, want tLong first", got)
	}
}

func TestResultKeptSpansAreDeterministic(t *testing.T) {
	build := func(order []int) Result {
		c := newTestCollector(t, -1)
		reports := []Span{
			span("t", "z", "root", 300, time.Millisecond, false),
			span("t", "root", "", 100, time.Millisecond, false),
			span("t", "m", "root", 200, time.Millisecond, false),
		}
		for _, i := range order {
			mustIngest(t, c, "a", reports[i])
		}
		if _, err := c.ReportWatermark("a", at(1000)); err != nil {
			t.Fatal(err)
		}
		if _, err := c.ReportWatermark("b", at(1000)); err != nil {
			t.Fatal(err)
		}
		res, err := c.ReportWatermark("c", at(1000))
		if err != nil || len(res) != 1 {
			t.Fatalf("err=%v res=%d", err, len(res))
		}
		return res[0]
	}
	r1 := build([]int{0, 1, 2})
	r2 := build([]int{2, 1, 0})
	if fmt.Sprint(ids(r1.Kept)) != fmt.Sprint(ids(r2.Kept)) {
		t.Fatalf("kept order depends on arrival: %v vs %v", ids(r1.Kept), ids(r2.Kept))
	}
	want := []string{"root", "m", "z"}
	if got := ids(r1.Kept); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("kept = %v, want %v", got, want)
	}
}

// --- linked groups ---------------------------------------------------------

func TestLinkedTracesKeptOrDroppedTogether(t *testing.T) {
	c := newTestCollector(t, -1)
	// Trace tB links to tA; tA has the only error, so the whole group sorts as
	// erroring. tC is unrelated and must be independent.
	linked := span("tB", "b1", "", 100, 10*time.Millisecond, false)
	linked.Link = "tA"
	mustIngest(t, c, "a", span("tA", "a1", "", 150, 10*time.Millisecond, false))
	mustIngest(t, c, "a", linked)
	mustIngest(t, c, "a", span("tC", "c1", "", 200, 10*time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	r := res[0]
	// No errors anywhere, so ordering is by max duration then trace id; both
	// groups have 10ms, so tA group (min trace tA) comes before tC.
	got := traceOrder(r.Kept)
	if len(got) != 3 || got[0] != "tA" || got[1] != "tB" || got[2] != "tC" {
		t.Fatalf("linked group order = %v", got)
	}
}

func TestDuplicateAfterSettlementIsLateAndNotReadmitted(t *testing.T) {
	c := newTestCollector(t, -1)
	s := span("t", "s1", "", 100, time.Millisecond, false)
	mustIngest(t, c, "a", s)
	for _, shard := range []string{"a", "b", "c"} {
		if _, err := c.ReportWatermark(shard, at(1000)); err != nil {
			t.Fatal(err)
		}
	}
	before := c.Results()[0]

	// The same span reported again after settlement: it stays counted once and
	// is reported in the late list, not re-applied.
	mustIngest(t, c, "b", s)
	if got := c.DuplicateCount(); got != 1 {
		t.Fatalf("DuplicateCount = %d, want 1", got)
	}
	late := c.Late()
	if len(late) != 1 {
		t.Fatalf("expected 1 late record, got %d", len(late))
	}
	if late[0].Span.ID != "s1" || !late[0].WindowSettled || late[0].Reason != "duplicate-after-settlement" {
		t.Fatalf("unexpected late record: %+v", late[0])
	}
	after := c.Results()[0]
	if fmt.Sprint(ids(after.Kept)) != fmt.Sprint(ids(before.Kept)) || after.Seq != before.Seq {
		t.Fatalf("published window changed after duplicate: %v vs %v", ids(after.Kept), ids(before.Kept))
	}
}

func TestLinkedGroupErrorPropagates(t *testing.T) {
	c := newTestCollector(t, -1)
	linked := span("aaa", "b1", "", 100, 1*time.Millisecond, false)
	linked.Link = "zzz" // links into trace zzz, which holds the error
	mustIngest(t, c, "a", span("zzz", "z1", "", 50, 1*time.Millisecond, true))
	mustIngest(t, c, "a", linked)
	mustIngest(t, c, "a", span("mmm", "m1", "", 200, 900*time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	got := traceOrder(res[0].Kept)
	// The linked component {aaa, zzz} carries the error and outranks mmm even
	// though mmm is much slower.
	if len(got) != 3 || got[2] != "mmm" {
		t.Fatalf("order = %v, want linked error group first", got)
	}
	if !(got[0] == "aaa" && got[1] == "zzz") {
		t.Fatalf("linked group spans not adjacent: %v", got)
	}
}

func TestLinkOutsideWindowDoesNotMerge(t *testing.T) {
	c := newTestCollector(t, -1)
	// tB in window 0 links to tA whose only span lands in window 1. The link
	// cannot merge across windows; tB keeps its own group and records a gap.
	linked := span("tB", "b1", "", 100, 10*time.Millisecond, false)
	linked.Link = "tA"
	mustIngest(t, c, "a", linked)
	mustIngest(t, c, "a", span("tA", "a1", "", 1100, 10*time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(2000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(2000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(2000))
	if err != nil || len(res) != 2 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	w0 := res[0]
	if got := traceOrder(w0.Kept); len(got) != 1 || got[0] != "tB" {
		t.Fatalf("window 0 should keep only tB, got %v", got)
	}
	gaps := w0.Gaps
	if len(gaps) != 1 || gaps[0].Kind != "link" || gaps[0].Reason != "link-out-of-window" {
		t.Fatalf("gaps = %+v, want one link-out-of-window", gaps)
	}
}

func TestLinkToUnknownTraceLeavesGap(t *testing.T) {
	c := newTestCollector(t, -1)
	linked := span("tB", "b1", "", 100, 10*time.Millisecond, false)
	linked.Link = "never-seen"
	mustIngest(t, c, "a", linked)
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	gaps := res[0].Gaps
	if len(gaps) != 1 || gaps[0].Kind != "link" || gaps[0].Reason != "link-target-missing" {
		t.Fatalf("gaps = %+v, want one link-target-missing", gaps)
	}
	if gaps[0].Reference != "never-seen" {
		t.Fatalf("gap reference = %q", gaps[0].Reference)
	}
}

func TestLinkChainUnionsThreeTraces(t *testing.T) {
	c := newTestCollector(t, -1)
	ab := span("tA", "a1", "", 100, 1*time.Millisecond, false)
	ab.Link = "tB"
	bc := span("tB", "b1", "", 150, 1*time.Millisecond, false)
	bc.Link = "tC"
	mustIngest(t, c, "a", ab)
	mustIngest(t, c, "a", bc)
	mustIngest(t, c, "a", span("tC", "c1", "", 200, 1*time.Millisecond, true))
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	// All three traces form one erroring group; its rows must be contiguous.
	got := traceOrder(res[0].Kept)
	if len(got) != 3 {
		t.Fatalf("expected one merged group of 3 traces, got %v", got)
	}
	if len(res[0].Gaps) != 0 {
		t.Fatalf("unexpected gaps: %+v", res[0].Gaps)
	}
}

// --- gaps ------------------------------------------------------------------

func TestMissingParentLeavesExplicitGap(t *testing.T) {
	c := newTestCollector(t, -1)
	mustIngest(t, c, "a", span("t", "child", "ghost", 100, 5*time.Millisecond, true))
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	r := res[0]
	if got := ids(r.Kept); len(got) != 1 || got[0] != "child" {
		t.Fatalf("kept = %v", got)
	}
	if len(r.Gaps) != 1 {
		t.Fatalf("gaps = %+v, want 1", r.Gaps)
	}
	g := r.Gaps[0]
	if g.Trace != "t" || g.SpanID != "child" || g.Reference != "ghost" || g.Kind != "parent" || g.Reason != "parent-missing" {
		t.Fatalf("unexpected gap: %+v", g)
	}
}

func TestParentInAnotherWindowIsGap(t *testing.T) {
	c := newTestCollector(t, -1)
	mustIngest(t, c, "a", span("t", "root", "", 100, 5*time.Millisecond, false))
	mustIngest(t, c, "a", span("t", "child", "root", 1200, 5*time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(2000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(2000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(2000))
	if err != nil || len(res) != 2 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	w1 := res[1]
	if len(w1.Gaps) != 1 || w1.Gaps[0].Reason != "parent-out-of-window" {
		t.Fatalf("gaps = %+v, want parent-out-of-window", w1.Gaps)
	}
}

func TestNoGapWhenParentPresentInWindow(t *testing.T) {
	c := newTestCollector(t, -1)
	mustIngest(t, c, "a", span("t", "root", "", 100, 5*time.Millisecond, false))
	mustIngest(t, c, "a", span("t", "child", "root", 120, 5*time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	if len(res[0].Gaps) != 0 {
		t.Fatalf("unexpected gaps: %+v", res[0].Gaps)
	}
}

// --- budget ----------------------------------------------------------------

func TestBudgetSkipsWholeGroupAndKeepsLaterSmallerGroup(t *testing.T) {
	// A three-span "big" group with the longest max duration sorts first but
	// cannot fit in a two-span budget; it is skipped whole and the two
	// single-span groups after it are still kept.
	big1 := span("tBig", "big1", "", 100, 300*time.Millisecond, false)
	big2 := span("tBig", "big2", "", 110, 200*time.Millisecond, false)
	big3 := span("tBig", "big3", "", 120, 100*time.Millisecond, false)
	small := span("tSmall", "small1", "", 200, 10*time.Millisecond, false)
	another := span("tAnother", "another1", "", 300, 5*time.Millisecond, false)

	c := newTestCollector(t, 2)
	mustIngest(t, c, "a", big1)
	mustIngest(t, c, "a", big2)
	mustIngest(t, c, "a", big3)
	mustIngest(t, c, "a", small)
	mustIngest(t, c, "a", another)
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	r := res[0]
	for _, id := range []string{"big1", "big2", "big3"} {
		if _, ok := findByID(r.Kept, id); ok {
			t.Fatalf("%s should have been dropped: kept=%v", id, ids(r.Kept))
		}
		if _, ok := findByID(r.Dropped, id); !ok {
			t.Fatalf("%s missing from dropped: %v", id, ids(r.Dropped))
		}
	}
	if got := traceOrder(r.Kept); len(got) != 2 {
		t.Fatalf("expected the two smaller groups kept, got %v", got)
	}
	if r.BudgetSpans != 2 {
		t.Fatalf("BudgetSpans = %d, want 2", r.BudgetSpans)
	}
	if r.UsedSpans != len(r.Kept) {
		t.Fatalf("UsedSpans = %d, kept = %d", r.UsedSpans, len(r.Kept))
	}
	if r.UsedSpans > r.BudgetSpans {
		t.Fatalf("usage %d exceeds budget %d", r.UsedSpans, r.BudgetSpans)
	}
}

func TestBudgetNeverSplitsLinkedGroup(t *testing.T) {
	// The linked group needs two spans but the budget only allows one; it must
	// be dropped whole, never half-kept, while the single-span group fits.
	linkedA := span("tB", "b1", "", 100, 5*time.Millisecond, true)
	linkedA.Link = "tA"
	other := span("tA", "a1", "", 150, 5*time.Millisecond, true)
	solo := span("tS", "s1", "", 200, 5*time.Millisecond, false)

	c := newTestCollector(t, 1)
	mustIngest(t, c, "a", linkedA)
	mustIngest(t, c, "a", other)
	mustIngest(t, c, "a", solo)
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	r := res[0]
	if _, ok := findByID(r.Kept, "b1"); ok {
		t.Fatalf("linked span b1 kept without a1: %v", ids(r.Kept))
	}
	if _, ok := findByID(r.Kept, "a1"); ok {
		t.Fatalf("linked span a1 kept without b1: %v", ids(r.Kept))
	}
	// Both linked spans dropped, solo kept since it fits alone.
	if got := ids(r.Kept); len(got) != 1 || got[0] != "s1" {
		t.Fatalf("kept = %v, want [s1]", got)
	}
	if len(r.Dropped) != 2 {
		t.Fatalf("dropped = %v, want both linked spans", ids(r.Dropped))
	}
}

func TestUnlimitedBudgetKeepsEverything(t *testing.T) {
	c := newTestCollector(t, -1)
	for i := 0; i < 5; i++ {
		mustIngest(t, c, "a", span(fmt.Sprintf("t%d", i), fmt.Sprintf("s%d", i), "", 100+i, time.Millisecond, i%2 == 0))
	}
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	if len(res[0].Kept) != 5 || len(res[0].Dropped) != 0 {
		t.Fatalf("kept=%d dropped=%d, want all kept", len(res[0].Kept), len(res[0].Dropped))
	}
}

// --- late and watermark violations ----------------------------------------

func TestLateRecordAfterSettlementIsReportedSeparately(t *testing.T) {
	c := newTestCollector(t, -1)
	mustIngest(t, c, "a", span("t", "s1", "", 100, time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}

	// Now a straggler from shard b that violates b's watermark and lands in
	// the already-published window.
	lateSpan := span("t", "s2", "", 500, time.Millisecond, true)
	mustIngest(t, c, "b", lateSpan)

	late := c.Late()
	if len(late) != 1 {
		t.Fatalf("expected 1 late record, got %d (%+v)", len(late), late)
	}
	lr := late[0]
	if lr.Shard != "b" || lr.Span.ID != "s2" {
		t.Fatalf("wrong late record: %+v", lr)
	}
	if !lr.WindowSettled {
		t.Fatalf("late record should be marked settled: %+v", lr)
	}
	if !lr.ViolatesWatermark || lr.Reason != "watermark-violation" {
		t.Fatalf("late record should flag watermark violation: %+v", lr)
	}

	// Published window must be untouched, and replay must be identical.
	published := c.Results()[0]
	if len(published.Kept) != 1 || len(published.Dropped) != 0 {
		t.Fatalf("published window mutated: kept=%v dropped=%v", ids(published.Kept), ids(published.Dropped))
	}
	replayed, err := c.Replay(published.Window)
	if err != nil {
		t.Fatalf("Replay: %v", err)
	}
	if fmt.Sprint(replayed.Kept) != fmt.Sprint(published.Kept) ||
		fmt.Sprint(replayed.Dropped) != fmt.Sprint(published.Dropped) ||
		fmt.Sprint(replayed.Gaps) != fmt.Sprint(published.Gaps) ||
		replayed.Seq != published.Seq ||
		replayed.UsedSpans != published.UsedSpans ||
		replayed.BudgetSpans != published.BudgetSpans {
		t.Fatalf("replay diverged from published result")
	}
}

func TestWatermarkViolationBeforeSettlementIsStillUsed(t *testing.T) {
	c := newTestCollector(t, -1)
	if _, err := c.ReportWatermark("a", at(700)); err != nil {
		t.Fatal(err)
	}
	// Start 100 is before a's watermark 700, but window 0 has not settled
	// because b and c have not advanced. The record is used, and because it
	// arrived before settlement it is not a late report.
	mustIngest(t, c, "a", span("t", "early", "", 100, time.Millisecond, true))
	if got := len(c.Late()); got != 0 {
		t.Fatalf("pre-settlement record must not be late, got %d", got)
	}
	// a must now cross the window end too before anything settles.
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(1000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	if _, ok := findByID(res[0].Kept, "early"); !ok {
		t.Fatalf("early span should have been admitted and kept: %v", ids(res[0].Kept))
	}
}

func TestReplayErrors(t *testing.T) {
	c := newTestCollector(t, -1)
	if _, err := c.Replay(Window{Start: at(0), Size: time.Second}); !errors.Is(err, ErrNotSettled) {
		t.Fatalf("unsettled replay: got %v, want ErrNotSettled", err)
	}
	if _, err := c.Replay(Window{Start: at(0), Size: 2 * time.Second}); !errors.Is(err, ErrBadWindow) {
		t.Fatalf("wrong size: got %v, want ErrBadWindow", err)
	}
	if _, err := c.Replay(Window{Start: at(500), Size: time.Second}); !errors.Is(err, ErrBadWindow) {
		t.Fatalf("misaligned start: got %v, want ErrBadWindow", err)
	}
}

func TestReplaySequenceStableAcrossWindows(t *testing.T) {
	c := newTestCollector(t, -1)
	// Load windows 0 and 1, then settle both at once and replay in reverse.
	mustIngest(t, c, "a", span("t0", "s0", "", 100, time.Millisecond, false))
	mustIngest(t, c, "a", span("t1", "s1", "", 1100, time.Millisecond, false))
	for _, shard := range []string{"a", "b", "c"} {
		if _, err := c.ReportWatermark(shard, at(2000)); err != nil {
			t.Fatal(err)
		}
	}
	results := c.Results()
	if len(results) != 2 {
		t.Fatalf("expected 2 published windows, got %d", len(results))
	}
	if results[0].Seq != 1 || results[1].Seq != 2 {
		t.Fatalf("seqs = %d, %d, want 1, 2", results[0].Seq, results[1].Seq)
	}
	for _, want := range results {
		got, err := c.Replay(want.Window)
		if err != nil {
			t.Fatalf("replay %v: %v", want.Window.Start, err)
		}
		if got.Seq != want.Seq || fmt.Sprint(ids(got.Kept)) != fmt.Sprint(ids(want.Kept)) {
			t.Fatalf("replay of seq %d diverged", want.Seq)
		}
	}
}

func TestLateStragglerInOpenWindowDoesNotBlockSettlement(t *testing.T) {
	c := newTestCollector(t, -1)
	mustIngest(t, c, "a", span("t", "s1", "", 100, time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("c", at(1000)); err != nil {
		t.Fatal(err)
	}
	// A record that arrives genuinely late but still inside window 1, which is
	// not yet closed; it must be admitted normally.
	mustIngest(t, c, "c", span("t", "s2", "", 1500, time.Millisecond, false))
	if got := len(c.Late()); got != 0 {
		t.Fatalf("unexpected late records: %d", got)
	}
	if _, err := c.ReportWatermark("a", at(2000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(2000)); err != nil {
		t.Fatal(err)
	}
	res, err := c.ReportWatermark("c", at(2000))
	if err != nil || len(res) != 1 {
		t.Fatalf("err=%v res=%d", err, len(res))
	}
	if _, ok := findByID(res[0].Kept, "s2"); !ok {
		t.Fatalf("s2 not kept: %v", ids(res[0].Kept))
	}
}

func TestRecordForClosedEmptyWindowIsLate(t *testing.T) {
	c := newTestCollector(t, -1)
	// Window 0 receives no records; all shards advance past its end, so it can
	// never be filled. A later record for window 0 violates the shard's own
	// watermark and must be reported as late without creating a result.
	for _, shard := range []string{"a", "b", "c"} {
		if _, err := c.ReportWatermark(shard, at(1000)); err != nil {
			t.Fatal(err)
		}
	}
	mustIngest(t, c, "a", span("t", "s0", "", 500, time.Millisecond, false))
	late := c.Late()
	if len(late) != 1 {
		t.Fatalf("expected 1 late record, got %d", len(late))
	}
	if late[0].WindowSettled || !late[0].ViolatesWatermark || late[0].Reason != "watermark-violation" {
		t.Fatalf("unexpected record: %+v", late[0])
	}
	if got := len(c.Results()); got != 0 {
		t.Fatalf("closed empty window must not publish a result, got %d", got)
	}
}

// --- interleaved three-shard scenario -------------------------------------

// TestInterleavedThreeShardScenario is the end-to-end acceptance scenario:
// three shards report errors, links and watermarks interleaved, across two
// windows and a linked group that straddles a window boundary.
func TestInterleavedThreeShardScenario(t *testing.T) {
	// Window 0 spans:
	//   trace t1: root + child, no error, total 10ms   (2 spans)
	//   trace t2: single error span, 50ms              (1 span) -> error group
	//   trace t3: links to t4; t4 present in window 0  (2 spans, 40ms max)
	t1root := span("t1", "t1r", "", 100, 5*time.Millisecond, false)
	t1child := span("t1", "t1c", "t1r", 120, 5*time.Millisecond, false)
	t2 := span("t2", "t2a", "", 200, 50*time.Millisecond, true)
	t3 := span("t3", "t3a", "", 300, 40*time.Millisecond, false)
	t3.Link = "t4"
	t4 := span("t4", "t4a", "", 320, 2*time.Millisecond, false)

	// A span budget of 3 fits the one-span error group plus the two-span
	// linked group, but not the two-span t1 group.
	budget := 3

	c := newTestCollector(t, budget)
	mustIngest(t, c, "a", t1root)
	mustIngest(t, c, "b", t2)
	mustIngest(t, c, "c", t1child)
	mustIngest(t, c, "a", t3)
	mustIngest(t, c, "b", t4)

	// A duplicate of t2 from another shard must count once.
	mustIngest(t, c, "c", t2)

	// Interleave watermarks: no window settles until all three cross 1s.
	if res, err := c.ReportWatermark("a", at(900)); err != nil || len(res) != 0 {
		t.Fatalf("a@900: res=%d err=%v", len(res), err)
	}
	if res, err := c.ReportWatermark("b", at(1500)); err != nil || len(res) != 0 {
		t.Fatalf("b@1500: res=%d err=%v", len(res), err)
	}
	// A window 1 span arrives before settlement of window 0.
	t5 := span("t5", "t5a", "", 1300, 10*time.Millisecond, false)
	mustIngest(t, c, "c", t5)

	// c reaches 1s, but a is still at 900ms, so nothing settles.
	if res, err := c.ReportWatermark("c", at(1000)); err != nil || len(res) != 0 {
		t.Fatalf("c@1000: res=%d err=%v", len(res), err)
	}
	// a crosses the end of window 0; only window 0 settles.
	settled, err := c.ReportWatermark("a", at(1000))
	if err != nil {
		t.Fatal(err)
	}
	if len(settled) != 1 {
		t.Fatalf("expected exactly window 0 to settle, got %d", len(settled))
	}
	r0 := settled[0]
	if r0.Window.Start != at(0) || r0.Seq != 1 {
		t.Fatalf("unexpected first settlement: %+v", r0.Window)
	}
	if got := c.DuplicateCount(); got != 1 {
		t.Fatalf("DuplicateCount = %d, want 1", got)
	}

	// t2 is the only erroring group and must be first.
	kept := r0.Kept
	if len(kept) == 0 || kept[0].Trace != "t2" {
		t.Fatalf("error group not first: %v", traceOrder(kept))
	}
	// t1 group is dropped entirely (does not fit after the error group).
	if _, ok := findByID(r0.Kept, "t1r"); ok {
		t.Fatalf("t1r should have been dropped: %v", ids(r0.Kept))
	}
	// The linked t3/t4 group fits and is kept whole.
	if _, ok := findByID(r0.Kept, "t3a"); !ok {
		t.Fatalf("t3a should be kept: %v", ids(r0.Kept))
	}
	if _, ok := findByID(r0.Kept, "t4a"); !ok {
		t.Fatalf("t4a should be kept: %v", ids(r0.Kept))
	}
	if r0.UsedSpans > budget {
		t.Fatalf("over budget: used %d > %d", r0.UsedSpans, budget)
	}
	if r0.UsedSpans != 3 {
		t.Fatalf("UsedSpans = %d, want 3", r0.UsedSpans)
	}

	// Window 1 settles once all three cross 2s.
	for _, step := range []struct {
		shard string
		t     time.Time
	}{{"a", at(2000)}, {"b", at(2000)}} {
		if _, err := c.ReportWatermark(step.shard, step.t); err != nil {
			t.Fatal(err)
		}
	}
	res, err := c.ReportWatermark("c", at(2000))
	if err != nil {
		t.Fatal(err)
	}
	if len(res) != 1 || res[0].Window.Start != at(1000) || res[0].Seq != 2 {
		t.Fatalf("window 1 settlement: %+v", res)
	}
	if _, ok := findByID(res[0].Kept, "t5a"); !ok {
		t.Fatalf("t5a not kept in window 1: %v", ids(res[0].Kept))
	}

	// A late, watermark-violating record for window 0 from shard a.
	lateSpan := span("t2", "t2late", "", 400, time.Millisecond, true)
	mustIngest(t, c, "a", lateSpan)
	late := c.Late()
	if len(late) != 1 {
		t.Fatalf("expected 1 late record, got %d", len(late))
	}
	lr := late[0]
	if lr.Shard != "a" || lr.Span.ID != "t2late" || !lr.WindowSettled || !lr.ViolatesWatermark {
		t.Fatalf("bad late record: %+v", lr)
	}

	// Replaying window 0 in full must reproduce the published result exactly.
	rep, err := c.Replay(r0.Window)
	if err != nil {
		t.Fatalf("Replay window 0: %v", err)
	}
	if rep.Seq != 1 || rep.UsedSpans != r0.UsedSpans || rep.BudgetSpans != r0.BudgetSpans {
		t.Fatalf("replay usage/seq diverged: %+v vs %+v", rep, r0)
	}
	if fmt.Sprint(ids(rep.Kept)) != fmt.Sprint(ids(r0.Kept)) {
		t.Fatalf("replay kept diverged: %v vs %v", ids(rep.Kept), ids(r0.Kept))
	}
	if fmt.Sprint(ids(rep.Dropped)) != fmt.Sprint(ids(r0.Dropped)) {
		t.Fatalf("replay dropped diverged: %v vs %v", ids(rep.Dropped), ids(r0.Dropped))
	}

	// Published window count and sequence remain exactly two.
	published := c.Results()
	if len(published) != 2 || published[0].Seq != 1 || published[1].Seq != 2 {
		t.Fatalf("published windows = %d, seqs %v", len(published), []int{published[0].Seq, published[1].Seq})
	}
}

// --- arrival-order independence -------------------------------------------

// TestArrivalOrderDoesNotChangeResult replays the same reports under several
// deterministic ingestion orders (across shards) and requires byte-identical
// published results.
func TestArrivalOrderDoesNotChangeResult(t *testing.T) {
	type report struct {
		shard string
		s     Span
	}
	link := span("tB", "b1", "", 150, 20*time.Millisecond, true)
	link.Link = "tA"
	base := []report{
		{"a", span("tA", "a1", "", 100, 5*time.Millisecond, false)},
		{"b", link},
		{"c", span("tC", "c1", "ghost", 200, 30*time.Millisecond, false)},
		{"a", span("tD", "d1", "", 300, 1*time.Millisecond, false)},
		{"b", span("tA", "a2", "a1", 120, 2*time.Millisecond, false)},
	}
	// The linked group {tA(a1,a2), tB(b1)} costs 3 spans; tC costs 1. A budget
	// of 4 fits both but leaves no room for tD.
	budget := 4

	run := func(order []int) Result {
		t.Helper()
		c := newTestCollector(t, budget)
		for _, i := range order {
			mustIngest(t, c, base[i].shard, base[i].s)
		}
		for _, shard := range []string{"a", "b", "c"} {
			if _, err := c.ReportWatermark(shard, at(1000)); err != nil {
				t.Fatal(err)
			}
		}
		results := c.Results()
		if len(results) != 1 {
			t.Fatalf("expected 1 result, got %d", len(results))
		}
		return results[0]
	}

	forward := run([]int{0, 1, 2, 3, 4})
	reverse := run([]int{4, 3, 2, 1, 0})
	shuffled := run([]int{2, 0, 4, 1, 3})
	for name, other := range map[string]Result{"reverse": reverse, "shuffled": shuffled} {
		if fmt.Sprint(ids(forward.Kept)) != fmt.Sprint(ids(other.Kept)) {
			t.Fatalf("%s kept order differs: %v vs %v", name, ids(forward.Kept), ids(other.Kept))
		}
		if fmt.Sprint(ids(forward.Dropped)) != fmt.Sprint(ids(other.Dropped)) {
			t.Fatalf("%s dropped order differs", name)
		}
		if fmt.Sprint(forward.Gaps) != fmt.Sprint(other.Gaps) {
			t.Fatalf("%s gaps differ: %+v vs %+v", name, forward.Gaps, other.Gaps)
		}
		if forward.UsedSpans != other.UsedSpans {
			t.Fatalf("%s usage differs", name)
		}
	}
	// Sanity: the linked error group {tA,tB} outranks the rest, and tC carries
	// a missing-parent gap.
	if _, ok := findByID(forward.Kept, "b1"); !ok {
		t.Fatalf("linked error span b1 missing: %v", ids(forward.Kept))
	}
	if len(forward.Gaps) != 1 || forward.Gaps[0].Reason != "parent-missing" {
		t.Fatalf("gaps = %+v", forward.Gaps)
	}
}

func TestExplicitSettleDoesNotPublishWithoutWatermarks(t *testing.T) {
	c := newTestCollector(t, -1)
	mustIngest(t, c, "a", span("t", "s1", "", 100, time.Millisecond, false))
	if got := c.Settle(); got != nil {
		t.Fatalf("Settle published %d windows without watermarks", len(got))
	}
	for _, shard := range []string{"a", "b", "c"} {
		if _, err := c.ReportWatermark(shard, at(1000)); err != nil {
			t.Fatal(err)
		}
	}
	if got := len(c.Results()); got != 1 {
		t.Fatalf("published %d windows, want 1", got)
	}
}

// --- concurrency -----------------------------------------------------------

func TestConcurrentIngestAndWatermarks(t *testing.T) {
	c := newTestCollector(t, -1)
	const shards = 3
	const perShard = 100

	var wg sync.WaitGroup
	for s := 0; s < shards; s++ {
		shard := []string{"a", "b", "c"}[s]
		wg.Add(1)
		go func(shard string, base int) {
			defer wg.Done()
			for i := 0; i < perShard; i++ {
				sp := span(fmt.Sprintf("t%d-%d", base, i), fmt.Sprintf("s%d-%d", base, i), "", i*5, time.Millisecond, i%17 == 0)
				if err := c.Ingest(shard, sp); err != nil {
					t.Errorf("ingest: %v", err)
					return
				}
			}
			if _, err := c.ReportWatermark(shard, at(1000)); err != nil {
				t.Errorf("watermark: %v", err)
			}
		}(shard, s)
	}
	wg.Wait()

	// Drive the final settlement deterministically.
	for _, shard := range []string{"a", "b", "c"} {
		if _, err := c.ReportWatermark(shard, at(1000)); err != nil {
			t.Fatal(err)
		}
	}
	results := c.Results()
	if len(results) != 1 {
		t.Fatalf("expected 1 settled window, got %d", len(results))
	}
	total := len(results[0].Kept) + len(results[0].Dropped)
	if total != shards*perShard {
		t.Fatalf("spans accounted = %d, want %d", total, shards*perShard)
	}
	if results[0].UsedSpans != len(results[0].Kept) {
		t.Fatalf("UsedSpans mismatch")
	}
	// Kept spans are grouped by trace in contiguous blocks, and each block is
	// ordered by start time then span id.
	closed := map[string]bool{}
	var cur string
	for i, s := range results[0].Kept {
		if i == 0 || s.Trace != cur {
			if closed[s.Trace] {
				t.Fatalf("trace %q appears in multiple non-contiguous blocks", s.Trace)
			}
			if cur != "" {
				closed[cur] = true
			}
			cur = s.Trace
			continue
		}
		prev := results[0].Kept[i-1]
		if s.Start.Before(prev.Start) || (s.Start.Equal(prev.Start) && s.ID < prev.ID) {
			t.Fatalf("spans of trace %q out of order at index %d", s.Trace, i)
		}
	}
}

// --- result isolation ------------------------------------------------------

func TestResultsAreCopies(t *testing.T) {
	c := newTestCollector(t, -1)
	mustIngest(t, c, "a", span("t", "s1", "", 100, time.Millisecond, false))
	if _, err := c.ReportWatermark("a", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("b", at(1000)); err != nil {
		t.Fatal(err)
	}
	if _, err := c.ReportWatermark("c", at(1000)); err != nil {
		t.Fatal(err)
	}
	r := c.Results()[0]
	r.Kept[0].ID = "mutated"
	again := c.Results()[0]
	if again.Kept[0].ID != "s1" {
		t.Fatalf("published result was mutated through a returned copy: %q", again.Kept[0].ID)
	}
}
