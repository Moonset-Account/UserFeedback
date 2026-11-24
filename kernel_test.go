package dataflow

import (
	"errors"
	"testing"
)

// TestFanoutLastReaderReturnsPool is the headline semantic test: one physical
// block fans out to two downstream references; taking a reference returns
// its edge slot at once, but the pool slot is returned only when the last
// reader releases.
func TestFanoutLastReaderReturnsPool(t *testing.T) {
	d, err := NewDriver(fanoutGraph(), fanoutPrograms(), Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}})
	if err != nil {
		t.Fatal(err)
	}
	bid := BlockID{Origin: "P", Index: 0}

	// 0: 申请输出槽 -> sole pool slot occupied.
	rec := mustStep(t, d, "P")
	if rec.Event.Kind != EventAllocated || rec.Event.Block != bid {
		t.Fatalf("unexpected alloc record: %+v", rec.Event)
	}
	if d.PoolUsed() != 1 || d.RefCount(bid) != 1 {
		t.Fatalf("after alloc: pool=%d refs=%d, want 1/1", d.PoolUsed(), d.RefCount(bid))
	}
	if h := d.Holders(bid); len(h) != 1 || h["P"] != 1 {
		t.Fatalf("after alloc holders=%v, want P:1", h)
	}

	// 1: atomic fanout publish to both edges.
	rec = mustStep(t, d, "P")
	if rec.Event.Kind != EventPublished || len(rec.Event.Targets) != 2 {
		t.Fatalf("unexpected publish record: %+v", rec.Event)
	}
	if got := d.Queue(1); len(got) != 1 || got[0] != bid {
		t.Fatalf("edge1 queue=%v, want [%s]", got, bid)
	}
	if got := d.Queue(2); len(got) != 1 || got[0] != bid {
		t.Fatalf("edge2 queue=%v, want [%s]", got, bid)
	}
	// Producer ref + two queued consumer refs.
	if d.RefCount(bid) != 3 {
		t.Fatalf("refs after fanout=%d, want 3", d.RefCount(bid))
	}
	if d.PoolUsed() != 1 {
		t.Fatalf("pool used=%d, want 1 (one physical block)", d.PoolUsed())
	}

	// 2: producer drops its own reference; two downstream refs survive.
	rec = mustStep(t, d, "P")
	if rec.Event.Last {
		t.Fatal("producer release must not be the last reference")
	}
	if d.RefCount(bid) != 2 || d.PoolUsed() != 1 {
		t.Fatalf("after producer release refs=%d pool=%d, want 2/1", d.RefCount(bid), d.PoolUsed())
	}
	if h := d.Holders(bid); len(h) != 0 {
		t.Fatalf("producer should hold nothing, holders=%v", h)
	}

	// C1 takes: edge slot returns immediately, reference converts to a hold.
	rec = mustStep(t, d, "C1")
	if rec.Event.Kind != EventTaken || rec.Event.Edge != 1 {
		t.Fatalf("unexpected take record: %+v", rec.Event)
	}
	if d.EdgeUsed(1) != 0 {
		t.Fatalf("edge1 slot not returned on take: used=%d", d.EdgeUsed(1))
	}
	if d.RefCount(bid) != 2 {
		t.Fatalf("take must not change refcount, got %d want 2", d.RefCount(bid))
	}
	if h := d.Holders(bid); h["C1"] != 1 {
		t.Fatalf("holders after C1 take=%v, want C1:1", h)
	}

	// C1 releases: one downstream reader remains -> pool slot NOT returned.
	rec = mustStep(t, d, "C1")
	if rec.Event.Last {
		t.Fatal("C1 must not be the last reference while C2's copy is outstanding")
	}
	if d.RefCount(bid) != 1 || d.PoolUsed() != 1 {
		t.Fatalf("after C1 release refs=%d pool=%d, want 1/1 (last reader C2)", d.RefCount(bid), d.PoolUsed())
	}

	// C2 take returns edge2 slot; then C2 release is the last one.
	mustStep(t, d, "C2")
	if d.EdgeUsed(2) != 0 {
		t.Fatalf("edge2 slot not returned on take: used=%d", d.EdgeUsed(2))
	}
	rec = mustStep(t, d, "C2")
	if !rec.Event.Last {
		t.Fatal("C2 release must be reported as the last reference")
	}
	if d.PoolUsed() != 0 {
		t.Fatalf("pool used=%d after last release, want 0", d.PoolUsed())
	}
	if d.RefCount(bid) != 0 {
		t.Fatalf("block should be gone, refs=%d", d.RefCount(bid))
	}
	if h := d.Holders(bid); len(h) != 0 {
		t.Fatalf("holders of freed block=%v, want empty", h)
	}
	if err := d.CheckInvariants(); err != nil {
		t.Fatal(err)
	}
}

// TestAtomicFanoutAllOrNothing verifies that a fanout publish needing a full
// target edge blocks, acquires no edge slots and enqueues nothing anywhere;
// once the blocking edge drains, all targets are acquired together.
func TestAtomicFanoutAllOrNothing(t *testing.T) {
	g := fanoutGraph()
	progs := map[OpID][]Instr{
		"P": {
			AllocInstr("a"),
			PublishInstr("a", 1), // prefill edge 1
			AllocInstr("b"),
			PublishInstr("b", 1, 2), // must be all-or-nothing
			ReleaseInstr("b"),
			ReleaseInstr("a"),
		},
		"C1": {
			TakeInstr(1, "x"), ReleaseInstr("x"),
			TakeInstr(1, "z"), ReleaseInstr("z"),
		},
		"C2": {TakeInstr(2, "y"), ReleaseInstr("y")},
	}
	d, err := NewDriver(g, progs, Config{Pool: 2, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}})
	if err != nil {
		t.Fatal(err)
	}

	mustStep(t, d, "P") // alloc a
	mustStep(t, d, "P") // publish a -> edge1
	mustStep(t, d, "P") // alloc b

	before := d.Export()
	_, err = d.Step("P") // blocked fanout
	if err == nil {
		t.Fatal("expected blocked fanout publish")
	}
	be, ok := AsBlocked(err)
	if !ok {
		t.Fatalf("expected *BlockedError, got %T: %v", err, err)
	}
	if len(be.Waits) != 1 {
		t.Fatalf("waits=%v, want exactly one full edge (edge 1)", be.Waits)
	}
	w := be.Waits[0]
	if w.Resource != ResourceEdgeSlot || w.Edge != 1 || w.Have != 1 || w.Cap != 1 {
		t.Fatalf("unexpected wait: %+v", w)
	}

	// Blocked call must not mutate state.
	after := d.Export()
	if !snapEqual(before, after) {
		t.Fatalf("state changed by blocked Step:\nbefore=%+v\nafter =%+v", before, after)
	}
	if d.pc["P"] != 3 {
		t.Fatalf("pc advanced on blocked step: %d", d.pc["P"])
	}
	// No partial acquisition: edge2 must still be empty.
	if q := d.Queue(2); len(q) != 0 {
		t.Fatalf("partial fanout enqueue into edge2: %v", q)
	}
	if q := d.Queue(1); len(q) != 1 || q[0].Index != 0 {
		t.Fatalf("edge1 should still hold only block a, got %v", q)
	}

	// Drain one reference from edge1: FIFO says it is block a, and taking
	// immediately frees the edge1 slot.
	recTake := mustStep(t, d, "C1")
	aID := BlockID{Origin: "P", Index: 0}
	if recTake.Event.Block != aID {
		t.Fatalf("FIFO violation: first take got %s, want %s", recTake.Event.Block, aID)
	}
	if d.EdgeUsed(1) != 0 {
		t.Fatal("edge1 slot should be free after take")
	}

	// Now the atomic fanout succeeds on both edges.
	rec := mustStep(t, d, "P")
	if rec.Event.Kind != EventPublished {
		t.Fatalf("want published, got %+v", rec.Event)
	}
	bid := BlockID{Origin: "P", Index: 2}
	if q := d.Queue(1); len(q) != 1 || q[0] != bid {
		t.Fatalf("edge1 after fanout=%v, want [%s]", q, bid)
	}
	if q := d.Queue(2); len(q) != 1 || q[0] != bid {
		t.Fatalf("edge2 after fanout=%v, want [%s]", q, bid)
	}
	if err := d.CheckInvariants(); err != nil {
		t.Fatal(err)
	}
}

// TestPoolSlotBlocking checks the pool-slot wait report at capacity zero/one.
func TestPoolSlotBlocking(t *testing.T) {
	g := &Graph{Ops: []OpID{"A"}, Edges: nil}
	progs := map[OpID][]Instr{"A": {AllocInstr("x"), ReleaseInstr("x")}}

	// Pool capacity 0: alloc must report a pool-slot wait.
	d, err := NewDriver(g, progs, Config{Pool: 0, EdgeCaps: map[EdgeID]int{}})
	if err != nil {
		t.Fatal(err)
	}
	_, err = d.Step("A")
	be, ok := AsBlocked(err)
	if !ok || len(be.Waits) != 1 || be.Waits[0].Resource != ResourcePoolSlot {
		t.Fatalf("want single pool_slot block, got %v", err)
	}
	if be.Waits[0].Cap != 0 || be.Waits[0].Have != 0 {
		t.Fatalf("unexpected pool wait counts: %+v", be.Waits[0])
	}
	if d.PoolUsed() != 0 {
		t.Fatal("blocked alloc must not consume a pool slot")
	}

	// Capacity 1 lets it run and the final release returns the slot.
	d2, err := NewDriver(g, progs, Config{Pool: 1, EdgeCaps: map[EdgeID]int{}})
	if err != nil {
		t.Fatal(err)
	}
	mustStep(t, d2, "A")
	if d2.PoolUsed() != 1 {
		t.Fatal("alloc should occupy pool")
	}
	rec := mustStep(t, d2, "A")
	if !rec.Event.Last || d2.PoolUsed() != 0 {
		t.Fatal("last release should return the pool slot")
	}
}

// TestDoneOperatorAndUnknownOp checks terminal/error behavior.
func TestDoneOperatorAndUnknownOp(t *testing.T) {
	g := &Graph{Ops: []OpID{"A"}}
	d, err := NewDriver(g, map[OpID][]Instr{"A": {}}, Config{Pool: 1, EdgeCaps: map[EdgeID]int{}})
	if err != nil {
		t.Fatal(err)
	}
	if !d.OperatorDone("A") || !d.AllDone() {
		t.Fatal("empty program should start done")
	}
	_, err = d.Step("A")
	if !errors.Is(err, ErrOperatorDone) {
		t.Fatalf("want ErrOperatorDone, got %v", err)
	}
	_, err = d.Step("Ghost")
	var pe *ProgramError
	if !errors.As(err, &pe) {
		t.Fatalf("want *ProgramError for unknown op, got %T %v", err, err)
	}
}

// TestDriverMaxFiveOperators enforces the step-interface cap while the
// verifier remains usable for a six-operator tiny graph.
func TestDriverMaxFiveOperators(t *testing.T) {
	ops := []OpID{"o0", "o1", "o2", "o3", "o4", "o5"}
	g := &Graph{Ops: ops}
	cfg := Config{Pool: 1, EdgeCaps: map[EdgeID]int{}}
	_, err := NewDriver(g, nil, cfg)
	if err == nil {
		t.Fatal("NewDriver must reject 6 operators")
	}
	var pe *ProgramError
	if !errors.As(err, &pe) {
		t.Fatalf("want *ProgramError, got %T", err)
	}

	// Verification is the configurator's engine and is not driver-capped.
	v, err := Verify(g, nil, cfg)
	if err != nil {
		t.Fatalf("Verify should accept 6 empty operators: %v", err)
	}
	if !v.Robust {
		t.Fatal("six empty operators should be trivially robust")
	}
}

// TestInvalidPrograms validates representative specification errors.
func TestInvalidPrograms(t *testing.T) {
	good := fanoutGraph()
	cfg := Config{Pool: 1, EdgeCaps: map[EdgeID]int{1: 1, 2: 1}}
	cases := []struct {
		name string
		g    *Graph
		p    map[OpID][]Instr
	}{
		{
			"take from edge owned by someone else",
			good,
			map[OpID][]Instr{"C1": {TakeInstr(2, "x")}},
		},
		{
			"publish edge with wrong producer",
			good,
			map[OpID][]Instr{"C1": {AllocInstr("h"), PublishInstr("h", 1)}},
		},
		{
			"publish unbound handle",
			&Graph{Ops: []OpID{"A"}, Edges: []Edge{{ID: 1, From: "A", To: "A"}}},
			map[OpID][]Instr{"A": {PublishInstr("nope", 1)}},
		},
		{
			"double bind handle",
			&Graph{Ops: []OpID{"A"}},
			map[OpID][]Instr{"A": {AllocInstr("h"), AllocInstr("h")}},
		},
		{
			"duplicate fanout target",
			&Graph{Ops: []OpID{"A", "B"}, Edges: []Edge{{ID: 1, From: "A", To: "B"}, {ID: 2, From: "A", To: "B"}}},
			map[OpID][]Instr{"A": {AllocInstr("h"), PublishInstr("h", 1, 1)}, "B": {}},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := NewDriver(tc.g, tc.p, cfg)
			if err == nil {
				t.Fatal("expected validation error")
			}
			var pe *ProgramError
			if !errors.As(err, &pe) {
				t.Fatalf("want *ProgramError, got %T: %v", err, err)
			}
		})
	}
}

func snapEqual(a, b Snapshot) bool {
	return canonicalSnap(a) == canonicalSnap(b)
}

func canonicalSnap(s Snapshot) string {
	return s.CanonicalKey(sortedEdgeOrderFromSnap(s))
}

func sortedEdgeOrderFromSnap(s Snapshot) []EdgeID {
	out := make([]EdgeID, 0, len(s.Queues))
	for e := range s.Queues {
		out = append(out, e)
	}
	// reuse simple sort
	for i := 0; i < len(out); i++ {
		for j := i + 1; j < len(out); j++ {
			if out[j] < out[i] {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}
