package entropygate_test

import (
	"bytes"
	"errors"
	"fmt"
	"sync"
	"testing"

	"entropygate"
)

func mustGate(t *testing.T, cfg entropygate.Config) *entropygate.Gate {
	t.Helper()
	g, err := entropygate.New(cfg)
	if err != nil {
		t.Fatalf("New(%+v) returned %v", cfg, err)
	}
	return g
}

func mustFeed(t *testing.T, g *entropygate.Gate, lane entropygate.Lane, syms ...byte) entropygate.Outcome {
	t.Helper()
	out, err := g.Feed(lane, syms)
	if err != nil {
		t.Fatalf("Feed(%s, %v) returned unexpected error %v", lane, syms, err)
	}
	return out
}

func mustStatus(t *testing.T, g *entropygate.Gate, lane entropygate.Lane) entropygate.Status {
	t.Helper()
	st, err := g.Status(lane)
	if err != nil {
		t.Fatalf("Status(%s) returned %v", lane, err)
	}
	return st
}

// stream builds a byte slice from an int slice for readable fixtures.
func stream(vals ...int) []byte {
	out := make([]byte, len(vals))
	for i, v := range vals {
		out[i] = byte(v)
	}
	return out
}

// window returns n symbols that pass both tests for the standard fixture
// config below: a run-free cycle whose first symbol occurs exactly once.
func window(first int, n int) []byte {
	out := make([]byte, n)
	for i := range out {
		out[i] = byte(first + i)
	}
	return out
}

// Standard fixture: repetition of 4, windows of 8, first-symbol count of 4.
func stdConfig() entropygate.Config {
	return entropygate.Config{
		RepetitionThreshold:  4,
		WindowSize:           8,
		FirstSymbolThreshold: 4,
		ResetCleanWindows:    2,
	}
}

func TestNewRejectsInvalidConfig(t *testing.T) {
	base := stdConfig()
	cases := []struct {
		name string
		mut  func(*entropygate.Config)
	}{
		{"zero repetition", func(c *entropygate.Config) { c.RepetitionThreshold = 0 }},
		{"negative repetition", func(c *entropygate.Config) { c.RepetitionThreshold = -1 }},
		{"zero window", func(c *entropygate.Config) { c.WindowSize = 0 }},
		{"negative window", func(c *entropygate.Config) { c.WindowSize = -2 }},
		{"zero first-symbol", func(c *entropygate.Config) { c.FirstSymbolThreshold = 0 }},
		{"negative first-symbol", func(c *entropygate.Config) { c.FirstSymbolThreshold = -3 }},
		{"negative reset windows", func(c *entropygate.Config) { c.ResetCleanWindows = -1 }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := base
			tc.mut(&cfg)
			g, err := entropygate.New(cfg)
			if err == nil || g != nil {
				t.Fatalf("New(%+v) = (%v, %v), want (nil, error)", cfg, g, err)
			}
			if !errors.Is(err, entropygate.ErrInvalidConfig) {
				t.Fatalf("error %v does not wrap ErrInvalidConfig", err)
			}
		})
	}
}

func TestNewAcceptsBoundaryConfig(t *testing.T) {
	cases := []entropygate.Config{
		{RepetitionThreshold: 1, WindowSize: 1, FirstSymbolThreshold: 1},
		{RepetitionThreshold: 1, WindowSize: 1, FirstSymbolThreshold: 2},
		{RepetitionThreshold: 255, WindowSize: 1024, FirstSymbolThreshold: 4096, ResetCleanWindows: 0},
	}
	for _, cfg := range cases {
		if _, err := entropygate.New(cfg); err != nil {
			t.Fatalf("New(%+v) returned %v, want nil", cfg, err)
		}
	}
}

func TestReleasesOnlyCompletePassingWindows(t *testing.T) {
	g := mustGate(t, stdConfig())

	w1 := window(0, 8) // 0..7
	out := mustFeed(t, g, entropygate.LaneA, w1[:5]...)
	if len(out.Released) != 0 {
		t.Fatalf("partial window released %v", out.Released)
	}
	if out.Accepted != 5 || out.Isolated || out.IsolationPoint != -1 {
		t.Fatalf("unexpected outcome after partial window: %+v", out)
	}
	st := mustStatus(t, g, entropygate.LaneA)
	if st.Buffered != 5 || !st.Active || st.RunLength != 1 {
		t.Fatalf("unexpected status after partial window: %+v", st)
	}

	out = mustFeed(t, g, entropygate.LaneA, w1[5:]...)
	if !bytes.Equal(out.Released, w1) {
		t.Fatalf("Released = %v, want %v", out.Released, w1)
	}
	if out.Accepted != 3 || out.Isolated || out.IsolationPoint != -1 {
		t.Fatalf("unexpected outcome after completing window: %+v", out)
	}
	st = mustStatus(t, g, entropygate.LaneA)
	if st.Buffered != 0 || st.RunLength != 1 || !st.Active {
		t.Fatalf("unexpected status after complete window: %+v", st)
	}
}

func TestRepetitionCountSpansWindowBoundary(t *testing.T) {
	g := mustGate(t, stdConfig())

	// First window ends in a run of 3 equal symbols.
	first := stream(0, 1, 2, 3, 4, 5, 7, 7, 7)
	if len(first) != 9 {
		t.Fatalf("fixture length %d", len(first))
	}
	out := mustFeed(t, g, entropygate.LaneA, first...)
	// The first 8 symbols form a passing window; the 9th starts the next
	// window and continues the run.
	if !bytes.Equal(out.Released, first[:8]) {
		t.Fatalf("Released = %v, want %v", out.Released, first[:8])
	}
	if out.Isolated || out.IsolationPoint != -1 || out.Accepted != 9 {
		t.Fatalf("unexpected outcome: %+v", out)
	}
	st := mustStatus(t, g, entropygate.LaneA)
	if st.RunLength != 3 || st.Buffered != 1 {
		t.Fatalf("run must continue across the window boundary, status %+v", st)
	}

	// A single further equal symbol completes the run of 4 and isolates.
	out = mustFeed(t, g, entropygate.LaneA, 7)
	if !out.Isolated || out.IsolationPoint != 1 || out.Accepted != 1 {
		t.Fatalf("expected isolation at first symbol of the chunk: %+v", out)
	}
	if len(out.Released) != 0 {
		t.Fatalf("untrusted window symbols were released: %v", out.Released)
	}
	st = mustStatus(t, g, entropygate.LaneA)
	if !st.Isolated || st.Buffered != 0 || st.ProbationRemaining != 0 {
		t.Fatalf("unexpected status after isolation: %+v", st)
	}
}

func TestRepetitionCountSpansTwoReadsWithinOneWindow(t *testing.T) {
	g := mustGate(t, stdConfig())

	// Two equal symbols, then a read boundary, then two more.
	out := mustFeed(t, g, entropygate.LaneA, 9, 9)
	if out.Isolated || len(out.Released) != 0 {
		t.Fatalf("unexpected outcome: %+v", out)
	}
	out = mustFeed(t, g, entropygate.LaneA, 9, 9)
	if !out.Isolated || out.IsolationPoint != 2 || out.Accepted != 2 {
		t.Fatalf("run must continue across reads and isolate at its 4th symbol: %+v", out)
	}
	if mustStatus(t, g, entropygate.LaneA).Buffered != 0 {
		t.Fatal("partial window was not discarded on isolation")
	}
}

func TestWindowHeaderNotLeakedWhenFailureFoundAtWindowEnd(t *testing.T) {
	g := mustGate(t, stdConfig())

	// The window's first symbol (5) appears four times, the fourth occurrence
	// being the final symbol of the window. Until that symbol arrives the
	// window looks healthy, so a naive implementation would already have
	// returned its earlier symbols.
	w := stream(5, 1, 2, 3, 5, 4, 5, 5)
	out := mustFeed(t, g, entropygate.LaneA, w...)
	if !out.Isolated {
		t.Fatalf("expected isolation: %+v", out)
	}
	if out.IsolationPoint != 8 || out.Accepted != 8 {
		t.Fatalf("IsolationPoint = %d Accepted = %d, want 8/8", out.IsolationPoint, out.Accepted)
	}
	if len(out.Released) != 0 {
		t.Fatalf("window header leaked before the window was judged: %v", out.Released)
	}
	if st := mustStatus(t, g, entropygate.LaneA); st.Buffered != 0 || !st.Isolated {
		t.Fatalf("unexpected status: %+v", st)
	}
}

func TestWindowFailureDetectedBeforeWindowEnd(t *testing.T) {
	g := mustGate(t, stdConfig())

	// Fourth occurrence of the window's first symbol arrives at position 7,
	// before the window is complete.
	w := stream(6, 1, 6, 2, 6, 3, 6, 4)
	out := mustFeed(t, g, entropygate.LaneA, w...)
	if !out.Isolated || out.IsolationPoint != 7 || out.Accepted != 7 {
		t.Fatalf("unexpected outcome: %+v", out)
	}
	if len(out.Released) != 0 {
		t.Fatalf("unhealthy window prefix released: %v", out.Released)
	}
	if st := mustStatus(t, g, entropygate.LaneA); st.Buffered != 0 {
		t.Fatalf("buffer not discarded: %+v", st)
	}
}

func TestWindowFailureDetectedInLaterRead(t *testing.T) {
	g := mustGate(t, stdConfig())

	// First five symbols of a window that will fail at its 6th symbol.
	if out := mustFeed(t, g, entropygate.LaneA, 3, 1, 3, 2, 3); out.Isolated || len(out.Released) != 0 {
		t.Fatalf("unexpected outcome: %+v", out)
	}
	// 6th symbol is the fourth 3.
	out := mustFeed(t, g, entropygate.LaneA, 3)
	if !out.Isolated || out.IsolationPoint != 1 {
		t.Fatalf("unexpected outcome: %+v", out)
	}
	if len(out.Released) != 0 {
		t.Fatalf("buffered header released: %v", out.Released)
	}
}

func TestWindowsBeforeIsolationInSameChunkAreReleased(t *testing.T) {
	g := mustGate(t, stdConfig())

	w1 := window(0, 8)
	w2 := window(1, 8)
	bad := stream(2, 3, 2, 4, 2, 5, 2, 6) // fourth 2 at position 7
	chunk := append(append(append([]byte{}, w1...), w2...), bad...)

	out := mustFeed(t, g, entropygate.LaneA, chunk...)
	wantReleased := append(append([]byte{}, w1...), w2...)
	if !bytes.Equal(out.Released, wantReleased) {
		t.Fatalf("Released = %v, want %v", out.Released, wantReleased)
	}
	if !out.Isolated || out.IsolationPoint != 23 || out.Accepted != 23 {
		t.Fatalf("IsolationPoint = %d Accepted = %d, want 23/23", out.IsolationPoint, out.Accepted)
	}
}

func TestReleasedSliceNotAliasedToInternalBuffer(t *testing.T) {
	g := mustGate(t, stdConfig())
	w1 := window(0, 8)
	out := mustFeed(t, g, entropygate.LaneA, w1...)
	snapshot := append([]byte{}, out.Released...)

	w2 := window(1, 8)
	out2 := mustFeed(t, g, entropygate.LaneA, w2...)
	if !bytes.Equal(out.Released, snapshot) {
		t.Fatalf("earlier Released was mutated by later feed: %v vs %v", out.Released, snapshot)
	}
	if !bytes.Equal(out2.Released, w2) {
		t.Fatalf("second window Released = %v, want %v", out2.Released, w2)
	}
}

func TestLanesAreIndependent(t *testing.T) {
	g := mustGate(t, stdConfig())

	// Isolate lane A.
	if out := mustFeed(t, g, entropygate.LaneA, 4, 4, 4, 4); !out.Isolated {
		t.Fatalf("lane A not isolated: %+v", out)
	}

	// Lane B is unaffected and releases entropy.
	w := window(0, 8)
	out := mustFeed(t, g, entropygate.LaneB, w...)
	if !bytes.Equal(out.Released, w) {
		t.Fatalf("lane B Released = %v, want %v", out.Released, w)
	}
	if st := mustStatus(t, g, entropygate.LaneB); !st.Active || st.Isolated {
		t.Fatalf("lane B status: %+v", st)
	}

	// Lane A stays latched; its input is not consumed.
	out, err := g.Feed(entropygate.LaneA, []byte{0, 1, 2})
	if !errors.Is(err, entropygate.ErrLaneIsolated) {
		t.Fatalf("Feed on isolated lane error = %v, want ErrLaneIsolated", err)
	}
	if out.Accepted != 0 || len(out.Released) != 0 || !out.Isolated {
		t.Fatalf("isolated lane consumed input: %+v", out)
	}
	if st := mustStatus(t, g, entropygate.LaneA); !st.Isolated || st.Active {
		t.Fatalf("lane A status: %+v", st)
	}

	// Isolating B later must not revive A.
	if out := mustFeed(t, g, entropygate.LaneB, 7, 7, 7, 7); !out.Isolated {
		t.Fatalf("lane B not isolated: %+v", out)
	}
	if !mustStatus(t, g, entropygate.LaneA).Isolated {
		t.Fatal("lane A changed state when lane B was isolated")
	}
}

func TestResetRequiresCleanWindowsBeforeRelease(t *testing.T) {
	g := mustGate(t, stdConfig())

	// Get lane A isolated, and leave a partial window behind on lane B.
	if out := mustFeed(t, g, entropygate.LaneA, 1, 1, 1, 1); !out.Isolated {
		t.Fatalf("lane A not isolated: %+v", out)
	}
	if out := mustFeed(t, g, entropygate.LaneB, 5, 6, 7); out.Isolated || len(out.Released) != 0 {
		t.Fatalf("lane B unexpected outcome: %+v", out)
	}

	g.Reset()

	for _, lane := range []entropygate.Lane{entropygate.LaneA, entropygate.LaneB} {
		st := mustStatus(t, g, lane)
		if st.Isolated || st.Active || st.ProbationRemaining != 2 || st.Buffered != 0 {
			t.Fatalf("status of lane %s after reset: %+v", lane, st)
		}
	}

	// First probation window is consumed but not released.
	w1 := window(0, 8)
	out := mustFeed(t, g, entropygate.LaneA, w1[:3]...)
	if out.Isolated || len(out.Released) != 0 || out.ProbationRemaining != 2 {
		t.Fatalf("unexpected outcome mid probation: %+v", out)
	}
	out = mustFeed(t, g, entropygate.LaneA, w1[3:]...)
	if out.Isolated || len(out.Released) != 0 || out.Accepted != 5 || out.ProbationRemaining != 1 {
		t.Fatalf("probation window leaked or miscounted: %+v", out)
	}

	// Second probation window still produces no entropy.
	w2 := window(1, 8)
	out = mustFeed(t, g, entropygate.LaneA, w2...)
	if out.Isolated || len(out.Released) != 0 || out.ProbationRemaining != 0 {
		t.Fatalf("second probation window: %+v", out)
	}
	if st := mustStatus(t, g, entropygate.LaneA); !st.Active {
		t.Fatalf("lane A not active after probation: %+v", st)
	}

	// Third window is the first releasable one.
	w3 := window(2, 8)
	out = mustFeed(t, g, entropygate.LaneA, w3...)
	if !bytes.Equal(out.Released, w3) {
		t.Fatalf("Released = %v, want %v", out.Released, w3)
	}

	// Lane B was never isolated, but Reset clears it too and puts it into its
	// own probation; it must also pass two windows.
	if st := mustStatus(t, g, entropygate.LaneB); st.Isolated || st.Active || st.ProbationRemaining != 2 {
		t.Fatalf("lane B status after reset: %+v", st)
	}
	bw := window(0, 8)
	out = mustFeed(t, g, entropygate.LaneB, bw...)
	if len(out.Released) != 0 || out.ProbationRemaining != 1 {
		t.Fatalf("lane B first probation window: %+v", out)
	}
}

func TestFailureDuringProbationRelatches(t *testing.T) {
	g := mustGate(t, stdConfig())
	g.Reset()
	if st := mustStatus(t, g, entropygate.LaneA); st.ProbationRemaining != 2 {
		t.Fatalf("unexpected status: %+v", st)
	}

	// Pass the first probation window, then fail during the second.
	out := mustFeed(t, g, entropygate.LaneA, window(0, 8)...)
	if out.ProbationRemaining != 1 {
		t.Fatalf("outcome = %+v", out)
	}
	out = mustFeed(t, g, entropygate.LaneA, 2, 2, 2, 2)
	if !out.Isolated || out.IsolationPoint != 4 || out.ProbationRemaining != 0 {
		t.Fatalf("failure during probation not latched: %+v", out)
	}
	if _, err := g.Feed(entropygate.LaneA, []byte{0}); !errors.Is(err, entropygate.ErrLaneIsolated) {
		t.Fatalf("error = %v, want ErrLaneIsolated", err)
	}
}

func TestResetWithZeroCleanWindowsActivatesImmediately(t *testing.T) {
	cfg := stdConfig()
	cfg.ResetCleanWindows = 0
	g := mustGate(t, cfg)
	if out := mustFeed(t, g, entropygate.LaneA, 3, 3, 3, 3); !out.Isolated {
		t.Fatalf("not isolated: %+v", out)
	}
	g.Reset()
	if st := mustStatus(t, g, entropygate.LaneA); !st.Active || st.ProbationRemaining != 0 || st.Isolated {
		t.Fatalf("status after reset: %+v", st)
	}
	w := window(0, 8)
	if out := mustFeed(t, g, entropygate.LaneA, w...); !bytes.Equal(out.Released, w) {
		t.Fatalf("Released = %v, want %v", out.Released, w)
	}
}

func TestResetDiscardsPartialWindowOnHealthyLane(t *testing.T) {
	g := mustGate(t, stdConfig())
	mustFeed(t, g, entropygate.LaneA, 0, 1, 2, 3)
	g.Reset()
	if st := mustStatus(t, g, entropygate.LaneA); st.Buffered != 0 || st.RunLength != 0 || st.Active {
		t.Fatalf("partial window survived reset: %+v", st)
	}
	// A full new window is needed before anything is released; the four
	// pre-reset symbols do not count towards it.
	if out := mustFeed(t, g, entropygate.LaneA, 0, 1, 2, 3); len(out.Released) != 0 {
		t.Fatalf("pre-reset symbols counted towards a new window: %v", out.Released)
	}
}

func TestThresholdOneIsolatesImmediately(t *testing.T) {
	g := mustGate(t, entropygate.Config{
		RepetitionThreshold:  1,
		WindowSize:           4,
		FirstSymbolThreshold: 10,
	})
	out := mustFeed(t, g, entropygate.LaneA, 0)
	if !out.Isolated || out.IsolationPoint != 1 || out.Accepted != 1 {
		t.Fatalf("unexpected outcome: %+v", out)
	}
	if st := mustStatus(t, g, entropygate.LaneA); st.RunLength != 1 {
		t.Fatalf("status = %+v", st)
	}
}

func TestFirstSymbolThresholdOneIsolatesOnWindowHeader(t *testing.T) {
	g := mustGate(t, entropygate.Config{
		RepetitionThreshold:  10,
		WindowSize:           4,
		FirstSymbolThreshold: 1,
	})
	out := mustFeed(t, g, entropygate.LaneA, 0, 1, 2, 3)
	if !out.Isolated || out.IsolationPoint != 1 || len(out.Released) != 0 {
		t.Fatalf("unexpected outcome: %+v", out)
	}
}

func TestFirstSymbolThresholdAboveWindowSizeNeverFires(t *testing.T) {
	g := mustGate(t, entropygate.Config{
		RepetitionThreshold:  10,
		WindowSize:           4,
		FirstSymbolThreshold: 5,
	})
	for i := 0; i < 25; i++ {
		out := mustFeed(t, g, entropygate.LaneA, byte(i%4))
		if out.Isolated {
			t.Fatalf("isolated at i=%d: %+v", i, out)
		}
	}
	if st := mustStatus(t, g, entropygate.LaneA); !st.Active {
		t.Fatalf("status = %+v", st)
	}
}

func TestWindowSizeOne(t *testing.T) {
	g := mustGate(t, entropygate.Config{
		RepetitionThreshold:  5,
		WindowSize:           1,
		FirstSymbolThreshold: 2,
	})
	s := stream(0, 1, 2, 3)
	out := mustFeed(t, g, entropygate.LaneA, s...)
	if !bytes.Equal(out.Released, s) {
		t.Fatalf("Released = %v, want %v", out.Released, s)
	}
	if st := mustStatus(t, g, entropygate.LaneA); st.Buffered != 0 {
		t.Fatalf("status = %+v", st)
	}
}

func TestEmptyFeedIsNoOp(t *testing.T) {
	g := mustGate(t, stdConfig())
	out := mustFeed(t, g, entropygate.LaneA)
	if out.Accepted != 0 || len(out.Released) != 0 || out.Isolated || out.IsolationPoint != -1 {
		t.Fatalf("outcome = %+v", out)
	}
	if st := mustStatus(t, g, entropygate.LaneA); st.Buffered != 0 || st.RunLength != 0 {
		t.Fatalf("status = %+v", st)
	}
}

func TestInvalidLaneRejected(t *testing.T) {
	g := mustGate(t, stdConfig())
	bad := entropygate.Lane(2)
	if _, err := g.Feed(bad, []byte{0}); !errors.Is(err, entropygate.ErrInvalidLane) {
		t.Fatalf("Feed error = %v, want ErrInvalidLane", err)
	}
	if _, err := g.Status(bad); !errors.Is(err, entropygate.ErrInvalidLane) {
		t.Fatalf("Status error = %v, want ErrInvalidLane", err)
	}
}

func TestLaneString(t *testing.T) {
	if got := entropygate.LaneA.String(); got != "A" {
		t.Fatalf("LaneA.String() = %q", got)
	}
	if got := entropygate.LaneB.String(); got != "B" {
		t.Fatalf("LaneB.String() = %q", got)
	}
	if got := entropygate.Lane(9).String(); got != "Lane(9)" {
		t.Fatalf("Lane(9).String() = %q", got)
	}
}

// chunkedResult is the aggregation of a stream fed in arbitrary chunks.
type chunkedResult struct {
	released   []byte
	accepted   int
	isolated   bool
	isolation  int // 1-based global position, -1 if none
	probation  int
	laneStatus entropygate.Status
}

// runChunked feeds stream cut at the given boundaries (indices at which a new
// chunk begins), stopping as soon as the gate reports isolation.
func runChunked(t *testing.T, g *entropygate.Gate, lane entropygate.Lane, data []byte, cuts []int) chunkedResult {
	t.Helper()
	res := chunkedResult{isolation: -1}
	offset := 0
	start := 0
	boundaries := append(append([]int{}, cuts...), len(data))
	for _, end := range boundaries {
		if end < start || end > len(data) {
			t.Fatalf("bad cut boundary %d for data length %d", end, len(data))
		}
		if res.isolated {
			break
		}
		chunk := data[start:end]
		out, err := g.Feed(lane, chunk)
		if err != nil {
			t.Fatalf("Feed at offset %d: %v", offset, err)
		}
		res.released = append(res.released, out.Released...)
		res.accepted += out.Accepted
		if out.Isolated && out.IsolationPoint >= 0 {
			res.isolated = true
			res.isolation = offset + out.IsolationPoint
		}
		res.probation = out.ProbationRemaining
		offset += len(chunk)
		start = end
	}
	st, err := g.Status(lane)
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	res.laneStatus = st
	return res
}

func TestArbitraryChunkingMatchesWholeStream(t *testing.T) {
	// W1 and W2 pass; W3 fails when its first symbol (2) appears four times.
	// The trailing bytes after the failure must never be consumed.
	data := stream(
		0, 1, 2, 3, 4, 5, 6, 7, // W1
		1, 2, 3, 4, 5, 6, 7, 0, // W2
		2, 3, 2, 4, 2, 5, 2, 6, // W3: fails at its 7th symbol
		8, 9, 10, 11,
	)

	baselineGate := mustGate(t, stdConfig())
	baseline := runChunked(t, baselineGate, entropygate.LaneA, data, nil)
	if !baseline.isolated || baseline.isolation != 23 || baseline.accepted != 23 {
		t.Fatalf("baseline isolation = %d accepted = %d isolated = %v", baseline.isolation, baseline.accepted, baseline.isolated)
	}
	if want := append(window(0, 8), stream(1, 2, 3, 4, 5, 6, 7, 0)...); !bytes.Equal(baseline.released, want) {
		t.Fatalf("baseline released = %v, want %v", baseline.released, want)
	}

	// Every single cut position, every pair of cut positions, and a
	// byte-by-byte feed.
	var cutSets [][]int
	for i := 0; i <= len(data); i++ {
		cutSets = append(cutSets, []int{i})
	}
	for i := 0; i < len(data); i++ {
		for j := i + 1; j <= len(data); j++ {
			cutSets = append(cutSets, []int{i, j})
		}
	}
	all := make([]int, len(data))
	for i := range all {
		all[i] = i
	}
	cutSets = append(cutSets, all)

	for _, cuts := range cutSets {
		g := mustGate(t, stdConfig())
		got := runChunked(t, g, entropygate.LaneA, data, cuts)
		if !bytes.Equal(got.released, baseline.released) ||
			got.accepted != baseline.accepted ||
			got.isolated != baseline.isolated ||
			got.isolation != baseline.isolation {
			t.Fatalf("cuts %v: got released=%v accepted=%d isolated=%v at=%d, want released=%v accepted=%d isolated=%v at=%d",
				cuts, got.released, got.accepted, got.isolated, got.isolation,
				baseline.released, baseline.accepted, baseline.isolated, baseline.isolation)
		}
	}
}

func TestChunkingWithCrossWindowRepetitionAndProbation(t *testing.T) {
	// W1 passes. W2 passes. Then a run of four 9s straddles the W2/W3
	// boundary: the 4th 9 is the 3rd symbol of W3.
	data := append([]byte{}, window(0, 8)...)
	data = append(data, 1, 2, 3, 4, 5, 6, 7, 9) // W2, ends with a 9
	data = append(data, 9, 9, 9)                // 9-run reaches 4 at the 3rd of these
	data = append(data, 8, 9)

	// Global 1-based positions: W2's last 9 is 16, then 17, 18, 19; the run
	// reaches four at position 19, the 3rd symbol of W3.
	baselineGate := mustGate(t, stdConfig())
	baseline := runChunked(t, baselineGate, entropygate.LaneA, data, nil)
	if !baseline.isolated || baseline.isolation != 19 {
		t.Fatalf("baseline isolation = %d isolated=%v", baseline.isolation, baseline.isolated)
	}
	if want := append(window(0, 8), stream(1, 2, 3, 4, 5, 6, 7, 9)...); !bytes.Equal(baseline.released, want) {
		t.Fatalf("baseline released = %v, want %v", baseline.released, want)
	}

	var cutSets [][]int
	for i := 0; i <= len(data); i++ {
		cutSets = append(cutSets, []int{i})
	}
	for i := 16; i < len(data); i++ {
		for j := i; j <= len(data); j++ {
			cutSets = append(cutSets, []int{i, j})
		}
	}
	for _, cuts := range cutSets {
		g := mustGate(t, stdConfig())
		got := runChunked(t, g, entropygate.LaneA, data, cuts)
		if !bytes.Equal(got.released, baseline.released) || got.isolation != baseline.isolation {
			t.Fatalf("cuts %v: got released=%v isolation=%d, want released=%v isolation=%d",
				cuts, got.released, got.isolation, baseline.released, baseline.isolation)
		}
	}

	// Same stream after reset: probation windows are not released regardless
	// of chunking, and the run is counted across the reset windows and the
	// window boundary.
	g := mustGate(t, stdConfig())
	g.Reset()
	res := runChunked(t, g, entropygate.LaneA, data, []int{3, 11, 17, 19, 20})
	if len(res.released) != 0 {
		t.Fatalf("probation released symbols: %v", res.released)
	}
	if !res.isolated || res.isolation != 19 {
		t.Fatalf("probation isolation = %d isolated=%v", res.isolation, res.isolated)
	}
}

func TestConcurrentLanes(t *testing.T) {
	g := mustGate(t, stdConfig())
	var wg sync.WaitGroup
	wg.Add(2)

	go func() {
		defer wg.Done()
		for i := 0; i < 100; i++ {
			w := window(i%200, 8)
			if _, err := g.Feed(entropygate.LaneA, w); err != nil {
				t.Errorf("lane A feed %d: %v", i, err)
				return
			}
		}
	}()
	go func() {
		defer wg.Done()
		for i := 0; i < 100; i++ {
			if _, err := g.Feed(entropygate.LaneB, []byte{byte(i), byte(i + 1)}); err != nil {
				t.Errorf("lane B feed %d: %v", i, err)
				return
			}
			if _, err := g.Status(entropygate.LaneB); err != nil {
				t.Errorf("lane B status %d: %v", i, err)
				return
			}
		}
	}()
	wg.Wait()

	st := mustStatus(t, g, entropygate.LaneA)
	if st.Isolated {
		t.Fatalf("lane A unexpectedly isolated: %+v", st)
	}
	if st.RunLength != 1 {
		t.Fatalf("lane A ended with run %d", st.RunLength)
	}
}

func TestIsolationErrorIsStableAndDoesNotConsume(t *testing.T) {
	g := mustGate(t, stdConfig())
	mustFeed(t, g, entropygate.LaneA, 1, 1, 1, 1)
	for i := 0; i < 3; i++ {
		out, err := g.Feed(entropygate.LaneA, []byte{0, 0, 0, 0})
		if !errors.Is(err, entropygate.ErrLaneIsolated) {
			t.Fatalf("attempt %d: error = %v", i, err)
		}
		if out.Accepted != 0 || len(out.Released) != 0 {
			t.Fatalf("attempt %d consumed input: %+v", i, out)
		}
	}
}

func ExampleGate() {
	g, _ := entropygate.New(entropygate.Config{
		RepetitionThreshold:  4,
		WindowSize:           8,
		FirstSymbolThreshold: 4,
		ResetCleanWindows:    0,
	})
	// A first window passes and is released...
	out, _ := g.Feed(entropygate.LaneA, []byte{0, 1, 2, 3, 4, 5, 6, 7})
	fmt.Println(len(out.Released), out.Isolated)
	// ...a window header that is later judged unhealthy is never released.
	out, _ = g.Feed(entropygate.LaneA, []byte{5, 1, 2, 3, 5, 4, 5, 5})
	fmt.Println(len(out.Released), out.Isolated, out.IsolationPoint)
	// Output:
	// 8 false
	// 0 true 8
}

// ExampleGate_crossWindowRun demonstrates a repetition run that starts in one
// window, crosses the window boundary and only completes in a second Feed
// call. The isolation point is relative to the second chunk, and everything
// buffered for the untrusted window is withheld.
func ExampleGate_crossWindowRun() {
	g, _ := entropygate.New(entropygate.Config{
		RepetitionThreshold:  4,
		WindowSize:           8,
		FirstSymbolThreshold: 4,
	})

	// Read 1: a passing window followed by the start of a new window whose
	// last two symbols are 9,9. The run is now 2 symbols long.
	read1 := []byte{0, 1, 2, 3, 4, 5, 6, 7, 9, 9}
	out, _ := g.Feed(entropygate.LaneA, read1)
	fmt.Printf("read1 released=%v isolated=%v\n", out.Released, out.Isolated)
	st, _ := g.Status(entropygate.LaneA)
	fmt.Printf("read1 buffered=%d run=%d\n", st.Buffered, st.RunLength)

	// Read 2: one more 9 brings the run to 3, the next 9 to 4 and isolates.
	// The 9 at position 2 is the third symbol of the new window, so the two
	// 9s buffered from read 1 are never released.
	read2 := []byte{9, 9, 7, 7}
	out, _ = g.Feed(entropygate.LaneA, read2)
	fmt.Printf("read2 released=%v accepted=%d isolated=%v at=%d\n",
		out.Released, out.Accepted, out.Isolated, out.IsolationPoint)
	_, err := g.Feed(entropygate.LaneA, []byte{0})
	fmt.Println("blocked:", errors.Is(err, entropygate.ErrLaneIsolated))
	// Output:
	// read1 released=[0 1 2 3 4 5 6 7] isolated=false
	// read1 buffered=2 run=2
	// read2 released=[] accepted=2 isolated=true at=2
	// blocked: true
}
