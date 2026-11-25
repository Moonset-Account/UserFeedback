package entropygate_test

import (
	"bytes"
	"errors"
	"sort"
	"testing"

	"entropygate"
)

// fuzzConfig is a small fixed configuration used by the fuzz target.
var fuzzConfig = entropygate.Config{
	RepetitionThreshold:  3,
	WindowSize:           5,
	FirstSymbolThreshold: 3,
	ResetCleanWindows:    1,
}

// feedAll feeds data to a fresh gate, splitting it at the supplied cut points,
// and aggregates the observable results. It stops feeding once the lane is
// latched, mirroring how a caller would treat an isolated lane.
type aggregate struct {
	released  []byte
	accepted  int
	isolated  bool
	isolation int
}

func feedAll(t *testing.T, g *entropygate.Gate, lane entropygate.Lane, data []byte, cuts []int) aggregate {
	t.Helper()
	agg := aggregate{isolation: -1}

	// Build and validate the chunk boundaries up front.
	prev := 0
	for _, c := range cuts {
		if c < prev || c > len(data) {
			t.Fatalf("cut %d out of range [%d,%d]", c, prev, len(data))
		}
		prev = c
	}
	bounds := make([]int, 0, len(cuts)+1)
	bounds = append(bounds, cuts...)
	bounds = append(bounds, len(data))

	start := 0
	offset := 0
	for _, end := range bounds {
		if agg.isolated {
			break
		}
		out, err := g.Feed(lane, data[start:end])
		if err != nil {
			t.Fatalf("Feed: %v", err)
		}
		agg.released = append(agg.released, out.Released...)
		agg.accepted += out.Accepted
		if out.Isolated && out.IsolationPoint >= 0 {
			agg.isolated = true
			agg.isolation = offset + out.IsolationPoint
		}
		offset += len(data[start:end])
		start = end
	}
	return agg
}

func TestProbationAndActiveWindowsInOneFeed(t *testing.T) {
	cfg := stdConfig()
	cfg.ResetCleanWindows = 2
	g := mustGate(t, cfg)
	g.Reset()

	pass1 := window(0, 8)
	pass2 := window(1, 8)
	active := window(2, 8)
	data := append(append(append([]byte{}, pass1...), pass2...), active...)

	out := mustFeed(t, g, entropygate.LaneA, data...)
	if !bytes.Equal(out.Released, active) {
		t.Fatalf("Released = %v, want only the third window %v", out.Released, active)
	}
	if out.ProbationRemaining != 0 || out.Isolated {
		t.Fatalf("outcome = %+v", out)
	}
	if st := mustStatus(t, g, entropygate.LaneA); !st.Active {
		t.Fatalf("status = %+v", st)
	}
}

func TestResetClearsIsolationForBothLanes(t *testing.T) {
	g := mustGate(t, stdConfig())
	mustFeed(t, g, entropygate.LaneA, 1, 1, 1, 1)
	mustFeed(t, g, entropygate.LaneB, 2, 2, 2, 2)
	g.Reset()
	for _, lane := range []entropygate.Lane{entropygate.LaneA, entropygate.LaneB} {
		if _, err := g.Feed(lane, []byte{0}); err != nil {
			t.Fatalf("Feed on lane %s after reset: %v", lane, err)
		}
	}
}

func TestIsolatedLaneReportsZeroProbation(t *testing.T) {
	g := mustGate(t, stdConfig())
	g.Reset()
	// Isolate lane A during probation.
	out := mustFeed(t, g, entropygate.LaneA, 4, 4, 4, 4)
	if !out.Isolated || out.ProbationRemaining != 0 {
		t.Fatalf("outcome = %+v", out)
	}
	out, err := g.Feed(entropygate.LaneA, nil)
	if !errors.Is(err, entropygate.ErrLaneIsolated) {
		t.Fatalf("err = %v", err)
	}
	if out.ProbationRemaining != 0 {
		t.Fatalf("isolated lane probation = %d", out.ProbationRemaining)
	}
}

// FuzzChunkingInvariance checks that splitting a stream arbitrarily never
// changes the released prefix or the isolation point, using a whole-stream
// feed as the reference.
func FuzzChunkingInvariance(f *testing.F) {
	f.Add([]byte{0, 1, 2, 3, 4, 5, 6, 7}, uint8(0), uint8(0))
	f.Add([]byte{4, 4, 4, 4}, uint8(0), uint8(0))
	f.Add([]byte{1, 1, 2, 1, 2, 3, 1, 1}, uint8(4), uint8(2))
	f.Add([]byte{}, uint8(0), uint8(0))

	f.Fuzz(func(t *testing.T, data []byte, c1, c2 uint8) {
		cuts := []int{}
		for _, c := range []uint8{c1, c2} {
			if int(c) <= len(data) {
				cuts = append(cuts, int(c))
			}
		}
		sort.Ints(cuts)

		refGate, err := entropygate.New(fuzzConfig)
		if err != nil {
			t.Fatalf("New: %v", err)
		}
		want := feedAll(t, refGate, entropygate.LaneA, data, nil)

		gotGate, err := entropygate.New(fuzzConfig)
		if err != nil {
			t.Fatalf("New: %v", err)
		}
		got := feedAll(t, gotGate, entropygate.LaneA, data, cuts)

		if !bytes.Equal(got.released, want.released) ||
			got.accepted != want.accepted ||
			got.isolated != want.isolated ||
			got.isolation != want.isolation {
			t.Fatalf("data=%v cuts=%v: got released=%v accepted=%d isolated=%v at=%d; want released=%v accepted=%d isolated=%v at=%d",
				data, cuts, got.released, got.accepted, got.isolated, got.isolation,
				want.released, want.accepted, want.isolated, want.isolation)
		}

		// Post-reset behaviour must be chunking-invariant too.
		for _, g := range []*entropygate.Gate{refGate, gotGate} {
			g.Reset()
		}
		wantR := feedAll(t, refGate, entropygate.LaneB, data, nil)
		gotR := feedAll(t, gotGate, entropygate.LaneB, data, cuts)
		if !bytes.Equal(gotR.released, wantR.released) ||
			gotR.accepted != wantR.accepted ||
			gotR.isolated != wantR.isolated ||
			gotR.isolation != wantR.isolation {
			t.Fatalf("after reset data=%v cuts=%v: got released=%v accepted=%d isolated=%v at=%d; want released=%v accepted=%d isolated=%v at=%d",
				data, cuts, gotR.released, gotR.accepted, gotR.isolated, gotR.isolation,
				wantR.released, wantR.accepted, wantR.isolated, wantR.isolation)
		}
	})
}
