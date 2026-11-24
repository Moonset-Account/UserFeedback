package dataflow

import (
	"context"
	"errors"
	"testing"
	"time"
)

// TestDAGScriptedDeadlockPrefix drives the specific B-first interleaving
// described in the README: B grabs the only pool slot, publishes into edge2,
// drops its producer ref, then waits for A's input; A and C cannot get pool
// slots. The graph A->B->C is acyclic.
func TestDAGScriptedDeadlockPrefix(t *testing.T) {
	cfg := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
	d, err := NewDriver(chainGraph(), chainPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	mustStep(t, d, "B") // alloc b: takes the only pool slot
	mustStep(t, d, "B") // publish b -> edge2
	mustStep(t, d, "B") // release producer ref; block b still occupies pool

	if got := d.Enabled(); len(got) != 0 {
		t.Fatalf("expected no enabled action, got %+v", got)
	}
	_, err = d.StepAny()
	if !errors.Is(err, ErrNoEnabledAction) {
		t.Fatalf("StepAny err=%v, want ErrNoEnabledAction", err)
	}

	blockers := d.Blockers()
	if len(blockers) != 3 {
		t.Fatalf("blockers=%d (%+v), want 3", len(blockers), blockers)
	}
	byOp := map[OpID]Blocker{}
	for _, b := range blockers {
		byOp[b.Op] = b
	}
	a := byOp["A"]
	if a.InstrIndex != 0 || len(a.Waits) != 1 || a.Waits[0].Resource != ResourcePoolSlot {
		t.Fatalf("A should wait for pool slot, got %+v", a)
	}
	b := byOp["B"]
	if b.InstrIndex != 3 || len(b.Waits) != 1 || b.Waits[0].Resource != ResourceEdgeItem || b.Waits[0].Edge != 1 {
		t.Fatalf("B should wait for an item on edge 1, got %+v", b)
	}
	c := byOp["C"]
	if c.InstrIndex != 0 || len(c.Waits) != 1 || c.Waits[0].Resource != ResourcePoolSlot {
		t.Fatalf("C should wait for pool slot, got %+v", c)
	}
}

// TestVerifyFindsShortestDeadlock shows exhaustive search returns the
// globally shortest stuck trajectory: C runs its output-slot Alloc first and
// immediately wedges the single-slot pool, giving a length-1 trace even
// though the B-first prefix takes 3 steps to deadlock.
func TestVerifyFindsShortestDeadlock(t *testing.T) {
	cfg := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
	v, err := Verify(chainGraph(), chainPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if v.Robust {
		t.Fatal("pool=1 must not be robust for the alloc-before-take chain")
	}
	dl := v.Deadlock
	if dl == nil {
		t.Fatal("expected deadlock report")
	}
	if dl.Length != 1 || len(dl.Trace) != 1 {
		t.Fatalf("shortest deadlock length=%d trace=%d, want 1", dl.Length, len(dl.Trace))
	}
	first := dl.Trace[0]
	if first.Op != "C" || first.InstrIndex != 0 || first.Instr.Kind != InstrAlloc {
		t.Fatalf("shortest trace should be C's first Alloc, got %+v", first)
	}
	if len(dl.Blockers) != 3 {
		t.Fatalf("deadlock blockers=%d, want A/B/C: %+v", len(dl.Blockers), dl.Blockers)
	}
	kinds := map[OpID]ResourceKind{}
	for _, b := range dl.Blockers {
		if len(b.Waits) == 0 {
			t.Fatalf("blocker %s has no waits", b.Op)
		}
		kinds[b.Op] = b.Waits[0].Resource
	}
	if kinds["A"] != ResourcePoolSlot || kinds["B"] != ResourcePoolSlot || kinds["C"] != ResourceEdgeItem {
		t.Fatalf("unexpected wait kinds: %+v", kinds)
	}

	// The returned trace must replay into exactly the reported terminal
	// state, and that state must indeed have no enabled action.
	rd, err := Replay(chainGraph(), chainPrograms(), cfg, dl.Trace, dl.Length)
	if err != nil {
		t.Fatalf("deadlock trace does not replay: %v", err)
	}
	if len(rd.Enabled()) != 0 || rd.AllDone() {
		t.Fatal("replayed deadlock trace did not end in a stuck unfinished state")
	}
	if err := rd.CheckInvariants(); err != nil {
		t.Fatal(err)
	}
}

// TestPoolTwoAlsoDeadlocks shows a subtler interleaving the search finds:
// A first parks block a in edge1, then C takes the second pool slot with c.
// B cannot allocate b (pool full) before consuming edge1, while C waits for
// B's output on edge2. Length 4, two blockers.
func TestPoolTwoAlsoDeadlocks(t *testing.T) {
	cfg := Config{Pool: 2, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}

	// Script the exact prefix.
	d, err := NewDriver(chainGraph(), chainPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	for _, op := range []OpID{"A", "A", "A", "C"} {
		mustStep(t, d, op)
	}
	if len(d.Enabled()) != 0 {
		t.Fatalf("prefix should be stuck, enabled=%+v", d.Enabled())
	}
	bs := d.Blockers()
	byOp := map[OpID]Blocker{}
	for _, b := range bs {
		byOp[b.Op] = b
	}
	b := byOp["B"]
	if b.InstrIndex != 0 || b.Waits[0].Resource != ResourcePoolSlot {
		t.Fatalf("B should wait for a pool slot before its alloc, got %+v", b)
	}
	c := byOp["C"]
	if c.InstrIndex != 1 || c.Waits[0].Resource != ResourceEdgeItem || c.Waits[0].Edge != 2 {
		t.Fatalf("C should wait for an item on edge2, got %+v", c)
	}

	// The verifier must independently return this as its shortest witness.
	v, err := Verify(chainGraph(), chainPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if v.Robust {
		t.Fatal("pool=2 must not be robust")
	}
	if v.Deadlock.Length != 4 {
		t.Fatalf("shortest pool=2 deadlock length=%d, want 4", v.Deadlock.Length)
	}
	ops := []OpID{}
	for _, r := range v.Deadlock.Trace {
		ops = append(ops, r.Op)
	}
	wantOps := []OpID{"A", "A", "A", "C"}
	for i := range wantOps {
		if i >= len(ops) || ops[i] != wantOps[i] {
			t.Fatalf("witness ops=%v, want prefix %v", ops, wantOps)
		}
	}
}

// TestPoolThreeRobustAllInterleavings: there are exactly three Alloc
// instructions in the graph, so pool=3 makes allocation always available;
// exhaustive search must then find every interleaving completable.
func TestPoolThreeRobustAllInterleavings(t *testing.T) {
	cfg := Config{Pool: 3, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
	v, err := Verify(chainGraph(), chainPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if !v.Robust {
		t.Fatalf("pool=3 should be robust; shortest deadlock len=%d blockers=%+v trace=%+v",
			v.Deadlock.Length, v.Deadlock.Blockers, v.Deadlock.Trace)
	}
	if v.StatesVisited == 0 {
		t.Fatal("verifier visited no states")
	}
	t.Logf("pool=3 verification explored %d canonical states for %d total instructions", v.StatesVisited, v.TotalSteps)

	// Re-run the formerly fatal A,A,A,C prefix and force completion.
	d, err := NewDriver(chainGraph(), chainPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	for _, op := range []OpID{"A", "A", "A", "C"} {
		mustStep(t, d, op)
	}
	if len(d.Enabled()) == 0 {
		t.Fatal("A,A,A,C prefix should not deadlock with pool=3")
	}
	runStepAnyToCompletion(t, d, 8) // 12 total - 4 prefix steps
	if len(d.Trace()) != 12 {
		t.Fatalf("total trace length=%d, want 12", len(d.Trace()))
	}
}

// TestFanoutRobustEveryInterleaving checks the headline graph itself.
func TestFanoutRobustEveryInterleaving(t *testing.T) {
	cfg := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
	v, err := Verify(fanoutGraph(), fanoutPrograms(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	if !v.Robust {
		t.Fatalf("fanout min config should be robust, got deadlock: %+v", v.Deadlock)
	}
}

// TestConcurrentDriversCompleteUnderRobustConfig drives every operator from
// its own goroutine repeatedly. The mutex linearizes calls; -race can verify
// memory safety. Every run must finish and preserve invariants.
func TestConcurrentDriversCompleteUnderRobustConfig(t *testing.T) {
	for iter := 0; iter < 60; iter++ {
		cfg := Config{Pool: 3, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
		d, err := NewDriver(chainGraph(), chainPrograms(), cfg)
		if err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		err = RunConcurrent(ctx, d)
		cancel()
		if err != nil {
			t.Fatalf("iter %d: concurrent run failed: %v", iter, err)
		}
		if !d.AllDone() {
			t.Fatalf("iter %d: not all operators done", iter)
		}
		if err := d.CheckInvariants(); err != nil {
			t.Fatalf("iter %d: %v", iter, err)
		}
		if used := d.PoolUsed(); used != 0 {
			t.Fatalf("iter %d: pool leaked %d blocks", iter, used)
		}
	}
}

// TestConcurrentFanoutRefcountStress hammers the shared block refcount path.
func TestConcurrentFanoutRefcountStress(t *testing.T) {
	for iter := 0; iter < 40; iter++ {
		cfg := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
		d, err := NewDriver(fanoutGraph(), fanoutPrograms(), cfg)
		if err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		if err := RunConcurrent(ctx, d); err != nil {
			cancel()
			t.Fatalf("iter %d: %v", iter, err)
		}
		cancel()
		if err := d.CheckInvariants(); err != nil {
			t.Fatalf("iter %d: %v", iter, err)
		}
		if d.PoolUsed() != 0 {
			t.Fatalf("iter %d: pool leak", iter)
		}
	}
}
