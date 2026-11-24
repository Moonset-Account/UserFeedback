package dataflow

import (
	"errors"
	"testing"
)

func intPtr(v int) *int64 {
	x := int64(v)
	return &x
}

func budgetPtr(v int) *int { return &v }

// TestConfigurePicksMinimalRobustPool checks the end-to-end optimization on
// the DAG chain: pool 1 and 2 are not robust, pool=3 with edge slots 1/1 is,
// so the optimum is pool 3 and both edge capacities at their minimum.
func TestConfigurePicksMinimalRobustPool(t *testing.T) {
	res, err := Configure(ConfigRequest{
		Graph:    chainGraph(),
		Programs: chainPrograms(),
		Pool:     IntRange{Min: 1, Max: 3},
		Edges: map[EdgeID]IntRange{
			1: {Min: 1, Max: 1},
			2: {Min: 1, Max: 1},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Feasible || res.Best == nil {
		t.Fatalf("expected feasible configuration, diagnostic=%+v", res.Diagnostic)
	}
	want := Config{Pool: 3, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
	if !configsEqual(res.Best.Config, want) {
		t.Fatalf("best=%+v, want %+v", res.Best.Config, want)
	}
	if res.Best.Cost != 5 { // 3 + 1 + 1 with default unit weights
		t.Fatalf("cost=%d, want 5", res.Best.Cost)
	}
	if res.Candidates != 3 {
		t.Fatalf("candidates=%d, want 3", res.Candidates)
	}
}

// TestConfigureInfeasibleReturnsShortestTrace makes the pool unhelpfully
// small across the whole range and requires the shortest deadlock trace plus
// every blocker's resource.
func TestConfigureInfeasibleReturnsShortestTrace(t *testing.T) {
	res, err := Configure(ConfigRequest{
		Graph:    chainGraph(),
		Programs: chainPrograms(),
		Pool:     IntRange{Min: 1, Max: 1},
		Edges: map[EdgeID]IntRange{
			1: {Min: 1, Max: 2},
			2: {Min: 1, Max: 2},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if res.Feasible {
		t.Fatalf("expected infeasible, got best=%+v", res.Best)
	}
	if res.Candidates != 4 {
		t.Fatalf("candidates=%d want 4", res.Candidates)
	}
	dl := res.Diagnostic
	if dl == nil {
		t.Fatal("infeasible result must carry a diagnostic")
	}
	if dl.Length != 1 {
		t.Fatalf("diagnostic trace length=%d, want 1 (C allocates the only pool slot first)", dl.Length)
	}
	if dl.Trace[0].Op != "C" {
		t.Fatalf("shortest trace op=%q, want C", dl.Trace[0].Op)
	}
	if !configsEqual(dl.Config, Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}) {
		t.Fatalf("diagnostic config should be cost-tie winner (1,1,1), got %+v", dl.Config)
	}
	if len(dl.Blockers) != 3 {
		t.Fatalf("blockers=%d want 3", len(dl.Blockers))
	}
	// Every attempt must be recorded as non-robust with a length-1 witness.
	for _, a := range res.Attempts {
		if a.Robust {
			t.Fatalf("attempt %+v unexpectedly robust", a.Config)
		}
		if a.ShortestDeadlock != 1 {
			t.Fatalf("attempt %+v shortest=%d want 1", a.Config, a.ShortestDeadlock)
		}
	}
}

// TestConfigureBudgetBoundaries exercises exact-budget and below-minimum
// boundary behavior.
func TestConfigureBudgetBoundaries(t *testing.T) {
	base := func(budget *int) ConfigRequest {
		return ConfigRequest{
			Graph:    chainGraph(),
			Programs: chainPrograms(),
			Pool:     IntRange{Min: 1, Max: 3},
			Edges: map[EdgeID]IntRange{
				1: {Min: 1, Max: 2},
				2: {Min: 1, Max: 2},
			},
			Budget: budget,
		}
	}

	// Minimum possible total is pool1 + edge1 + edge2 = 3.
	_, err := Configure(base(budgetPtr(2)))
	var nc *NoCandidateError
	if !errors.As(err, &nc) {
		t.Fatalf("budget=2 should yield *NoCandidateError, got %v", err)
	}
	if nc.MinRequired != 3 || nc.Budget != 2 {
		t.Fatalf("NoCandidateError=%+v, want min=3 budget=2", nc)
	}

	// Budget exactly 3 admits only (1,1,1), which is not robust.
	res3, err := Configure(base(budgetPtr(3)))
	if err != nil {
		t.Fatal(err)
	}
	if res3.Candidates != 1 || res3.Feasible {
		t.Fatalf("budget=3: candidates=%d feasible=%v, want 1 false", res3.Candidates, res3.Feasible)
	}

	// Budget 4 admits three pool=1 variants and (2,1,1); pool 1 and 2 are
	// both non-robust, so still infeasible.
	res4, err := Configure(base(budgetPtr(4)))
	if err != nil {
		t.Fatal(err)
	}
	if res4.Candidates != 4 {
		t.Fatalf("budget=4 candidates=%d want 4", res4.Candidates)
	}
	if res4.Feasible {
		t.Fatalf("budget=4 must still be infeasible, best=%+v", res4.Best)
	}

	// Budget 5 admits the robust (3,1,1) setting.
	res5, err := Configure(base(budgetPtr(5)))
	if err != nil {
		t.Fatal(err)
	}
	if res5.Candidates != 8 {
		t.Fatalf("budget=5 candidates=%d want 8", res5.Candidates)
	}
	if !res5.Feasible {
		t.Fatal("budget=5 should admit robust pool=3,edge=1,1")
	}
	if !configsEqual(res5.Best.Config, Config{Pool: 3, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}) {
		t.Fatalf("best=%+v", res5.Best.Config)
	}

	// Nil budget admits the full Cartesian product: 3 * 2 * 2 = 12.
	resAll, err := Configure(base(nil))
	if err != nil {
		t.Fatal(err)
	}
	if resAll.Candidates != 12 {
		t.Fatalf("unlimited candidates=%d want 12", resAll.Candidates)
	}
}

// TestCompareConfigsEdgeNumberTieBreak directly verifies the tie rule on
// crossing capacity vectors with equal weighted cost: edge #1 is compared
// before edge #2 and the smaller value at the first differing edge wins.
func TestCompareConfigsEdgeNumberTieBreak(t *testing.T) {
	edges := []EdgeID{1, 2}
	// Equal unit edge weights; pool identical.
	w := Weights{Pool: intPtr(1), EdgeDefault: intPtr(1)}
	x := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 3}} // cost 1+1+3=5
	y := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 2, 2: 2}} // cost 1+2+2=5
	if c := CompareConfigs(x, y, edges, w); c != -1 {
		t.Fatalf("CompareConfigs(x,y)=%d, want -1 (edge 1: 1<2)", c)
	}
	if c := CompareConfigs(y, x, edges, w); c != 1 {
		t.Fatalf("CompareConfigs(y,x)=%d, want +1", c)
	}
	if c := CompareConfigs(x, x, edges, w); c != 0 {
		t.Fatalf("identical configs compare=%d want 0", c)
	}

	// Weighted costs are honored before tie-breaking: make edge 2 expensive
	// so y is cheaper despite its larger edge-1 capacity.
	w2 := Weights{Pool: intPtr(0), Edge: map[EdgeID]int64{1: 1, 2: 10}}
	cheap1 := Config{Pool: 0, EdgeCaps: map[EdgeID]int{1: 2, 2: 1}} // 2+10=12
	cheap2 := Config{Pool: 0, EdgeCaps: map[EdgeID]int{1: 1, 2: 2}} // 1+20=21
	if c := CompareConfigs(cheap1, cheap2, edges, w2); c != -1 {
		t.Fatalf("weighted cost should dominate tie-break, cmp=%d", c)
	}
}

// TestConfigureTieBreakEndToEnd creates a weighted-cost tie across all
// candidates (zero edge weights) and checks the chosen vector is the
// edge-number lexicographic minimum. Edges are declared out of numeric order
// to prove numeric edge numbers drive the ordering.
func TestConfigureTieBreakEndToEnd(t *testing.T) {
	for _, g := range []*Graph{chainGraph(), chainGraphScrambled()} {
		res, err := Configure(ConfigRequest{
			Graph:    g,
			Programs: chainPrograms(),
			Pool:     IntRange{Min: 3, Max: 3}, // fixed at a robust value
			Edges: map[EdgeID]IntRange{
				1: {Min: 1, Max: 2},
				2: {Min: 1, Max: 2},
			},
			Weights: Weights{
				Pool:        intPtr(1),
				EdgeDefault: intPtr(0), // all edge settings tie on cost
			},
		})
		if err != nil {
			t.Fatal(err)
		}
		if !res.Feasible {
			t.Fatalf("expected feasible: %+v", res.Diagnostic)
		}
		want := Config{Pool: 3, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
		if !configsEqual(res.Best.Config, want) {
			t.Fatalf("tie-break winner=%+v, want lex-min %+v", res.Best.Config, want)
		}
		if res.Best.Cost != 3 {
			t.Fatalf("cost=%d want 3 (only pool weighted)", res.Best.Cost)
		}
	}
}

// TestConfigureWeightedTradeoff uses non-uniform weights and checks the
// objective actually steers capacity toward the cheaper edge.
func TestConfigureWeightedTradeoff(t *testing.T) {
	// Fanout graph is robust already at (pool=1,e1=1,e2=1), so that unique
	// coordinatewise minimum must win regardless of weights.
	res, err := Configure(ConfigRequest{
		Graph:    fanoutGraph(),
		Programs: fanoutPrograms(),
		Pool:     IntRange{Min: 1, Max: 3},
		Edges: map[EdgeID]IntRange{
			1: {Min: 1, Max: 3},
			2: {Min: 1, Max: 3},
		},
		Weights: Weights{Pool: intPtr(100), EdgeDefault: intPtr(1)},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.Feasible {
		t.Fatal("fanout min config should be feasible")
	}
	want := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
	if !configsEqual(res.Best.Config, want) {
		t.Fatalf("best=%+v want %+v", res.Best.Config, want)
	}
	if res.Best.Cost != 102 {
		t.Fatalf("cost=%d want 100+1+1=102", res.Best.Cost)
	}
}

func configsEqual(a, b Config) bool {
	if a.Pool != b.Pool || len(a.EdgeCaps) != len(b.EdgeCaps) {
		return false
	}
	for e, v := range a.EdgeCaps {
		if b.EdgeCaps[e] != v {
			return false
		}
	}
	return true
}
