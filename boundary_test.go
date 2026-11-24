package dataflow

import (
	"errors"
	"testing"
)

// TestZeroCapacityEdgeIsPermanentBackpressure covers the capacity-range
// boundary 0: a publish target with cap 0 always reports an edge-slot wait,
// Have=Cap=0, and nothing is enqueued.
func TestZeroCapacityEdgeIsPermanentBackpressure(t *testing.T) {
	g := &Graph{Ops: []OpID{"P", "C"}, Edges: []Edge{{ID: 7, From: "P", To: "C"}}}
	progs := map[OpID][]Instr{
		"P": {AllocInstr("b"), PublishInstr("b", 7), ReleaseInstr("b")},
		"C": {TakeInstr(7, "x"), ReleaseInstr("x")},
	}
	d, err := NewDriver(g, progs, Config{Pool: 1, EdgeCaps: map[EdgeID]int{7: 0}})
	if err != nil {
		t.Fatal(err)
	}
	mustStep(t, d, "P") // alloc
	_, err = d.Step("P")
	be, ok := AsBlocked(err)
	if !ok || len(be.Waits) != 1 {
		t.Fatalf("want one blocked error, got %v", err)
	}
	w := be.Waits[0]
	if w.Resource != ResourceEdgeSlot || w.Edge != 7 || w.Cap != 0 || w.Have != 0 {
		t.Fatalf("unexpected zero-cap wait: %+v", w)
	}
	if q := d.Queue(7); len(q) != 0 {
		t.Fatalf("blocked publish enqueued %v", q)
	}

	// Exhaustive verification reports the permanent stuck state.
	v, err := Verify(g, progs, Config{Pool: 1, EdgeCaps: map[EdgeID]int{7: 0}})
	if err != nil {
		t.Fatal(err)
	}
	if v.Robust || v.Deadlock == nil {
		t.Fatal("zero-cap edge cannot be robust")
	}
}

// TestDefaultEdgeRangeAndZeroWeightPool exercises the DefaultEdge branch of
// Configure and confirms a zero pool weight still yields a deterministic
// edge-number tie-break rather than a zero-cost ambiguity.
func TestDefaultEdgeRange(t *testing.T) {
	one := IntRange{Min: 1, Max: 1}
	res, err := Configure(ConfigRequest{
		Graph:       fanoutGraph(),
		Programs:    fanoutPrograms(),
		Pool:        IntRange{Min: 1, Max: 2},
		DefaultEdge: &one,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Feasible {
		t.Fatalf("fanout at pool=1 should be feasible: %+v", res.Diagnostic)
	}
	if res.Best.Config.Pool != 1 {
		t.Fatalf("best pool=%d want 1", res.Best.Config.Pool)
	}
	if res.Candidates != 2 {
		t.Fatalf("candidates=%d want 2 (pool 1..2)", res.Candidates)
	}
}

// TestEmptyProgramsFinishImmediately is a boundary where some operators have
// no instructions and missing program entries default to empty.
func TestEmptyProgramsFinishImmediately(t *testing.T) {
	g := &Graph{Ops: []OpID{"A", "B"}}
	d, err := NewDriver(g, nil, Config{Pool: 0, EdgeCaps: map[EdgeID]int{}})
	if err != nil {
		t.Fatal(err)
	}
	if !d.AllDone() {
		t.Fatal("nil programs should make every op immediately done")
	}
	v, err := Verify(g, nil, Config{Pool: 0, EdgeCaps: map[EdgeID]int{}})
	if err != nil || !v.Robust {
		t.Fatalf("empty graph should verify robust: v=%+v err=%v", v, err)
	}
}

// TestCostBudgetBoundary checks the optional weighted-cost ceiling and
// negative-weight validation.
func TestCostBudgetBoundary(t *testing.T) {
	one := IntRange{Min: 1, Max: 1}
	_, err := Configure(ConfigRequest{
		Graph:       fanoutGraph(),
		Programs:    fanoutPrograms(),
		Pool:        IntRange{Min: 1, Max: 1},
		DefaultEdge: &one,
		CostBudget:  int64Ptr(101), // min cost 102 with pool weight 100
		Weights:     Weights{Pool: int64Ptr(100), EdgeDefault: int64Ptr(1)},
	})
	var cbe *CostBudgetError
	if !errors.As(err, &cbe) {
		t.Fatalf("want *CostBudgetError, got %v", err)
	}
	if cbe.MinCost != 102 {
		t.Fatalf("MinCost=%d want 102", cbe.MinCost)
	}

	// Exactly 102 is admitted and feasible.
	res, err := Configure(ConfigRequest{
		Graph:       fanoutGraph(),
		Programs:    fanoutPrograms(),
		Pool:        IntRange{Min: 1, Max: 1},
		DefaultEdge: &one,
		CostBudget:  int64Ptr(102),
		Weights:     Weights{Pool: int64Ptr(100), EdgeDefault: int64Ptr(1)},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Feasible || res.Best.Cost != 102 {
		t.Fatalf("expected feasible cost 102, got %+v err", res.Best)
	}

	// Negative weights are rejected rather than silently optimized.
	neg := int64(-1)
	_, err = Configure(ConfigRequest{
		Graph:       fanoutGraph(),
		Programs:    fanoutPrograms(),
		Pool:        IntRange{Min: 1, Max: 1},
		DefaultEdge: &one,
		Weights:     Weights{Pool: &neg},
	})
	var pe *ProgramError
	if !errors.As(err, &pe) {
		t.Fatalf("negative weight should be *ProgramError, got %v", err)
	}
}

func int64Ptr(v int64) *int64 { return &v }
