package dataflow_test

import (
	"errors"
	"fmt"
	"log"

	"dataflow"
)

func chainGraph() *dataflow.Graph {
	return &dataflow.Graph{
		Ops: []dataflow.OpID{"A", "B", "C"},
		Edges: []dataflow.Edge{
			{ID: 1, From: "A", To: "B"},
			{ID: 2, From: "B", To: "C"},
		},
	}
}

// The intermediate B and sink C allocate an output/pool block before
// consuming their input, which makes the acyclic chain scheduling-sensitive.
func chainPrograms() map[dataflow.OpID][]dataflow.Instr {
	return map[dataflow.OpID][]dataflow.Instr{
		"A": {
			dataflow.AllocInstr("a"),
			dataflow.PublishInstr("a", 1),
			dataflow.ReleaseInstr("a"),
		},
		"B": {
			dataflow.AllocInstr("b"),
			dataflow.PublishInstr("b", 2),
			dataflow.ReleaseInstr("b"),
			dataflow.TakeInstr(1, "x"),
			dataflow.ReleaseInstr("x"),
		},
		"C": {
			dataflow.AllocInstr("c"),
			dataflow.TakeInstr(2, "y"),
			dataflow.ReleaseInstr("y"),
			dataflow.ReleaseInstr("c"),
		},
	}
}

// ExampleConfigure finds the minimum-weight buffer settings that make every
// interleaving of the acyclic A->B->C chain run to completion.
func ExampleConfigure() {
	res, err := dataflow.Configure(dataflow.ConfigRequest{
		Graph:    chainGraph(),
		Programs: chainPrograms(),
		Pool:     dataflow.IntRange{Min: 1, Max: 3},
		Edges: map[dataflow.EdgeID]dataflow.IntRange{
			1: {Min: 1, Max: 1},
			2: {Min: 1, Max: 1},
		},
	})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("feasible=%v pool=%d edge1=%d edge2=%d cost=%d\n",
		res.Feasible, res.Best.Config.Pool,
		res.Best.Config.EdgeCaps[1], res.Best.Config.EdgeCaps[2], res.Best.Cost)
	fmt.Printf("candidates=%d\n", res.Candidates)
	// Output:
	// feasible=true pool=3 edge1=1 edge2=1 cost=5
	// candidates=3
}

// ExampleVerify_deadlock shows the shortest counterexample and the resource
// each blocker waits for when the budget cannot buy a safe configuration.
func ExampleVerify_deadlock() {
	cfg := dataflow.Config{Pool: 1, EdgeCaps: map[dataflow.EdgeID]int{1: 1, 2: 1}}
	v, err := dataflow.Verify(chainGraph(), chainPrograms(), cfg)
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("robust=%v\n", v.Robust)
	fmt.Printf("shortest=%d first-op=%s\n", v.Deadlock.Length, v.Deadlock.Trace[0].Op)
	for _, b := range v.Deadlock.Blockers {
		fmt.Printf("%s waits %s\n", b.Op, b.Waits[0].Resource)
	}
	// Output:
	// robust=false
	// shortest=1 first-op=C
	// A waits pool_slot
	// B waits pool_slot
	// C waits edge_item
}

// ExampleDriver_Step drives the fanout of one physical block to two
// consumers and observes that the shared-pool slot is returned only at the
// last reader's release.
func ExampleDriver_Step() {
	g := &dataflow.Graph{
		Ops: []dataflow.OpID{"P", "C1", "C2"},
		Edges: []dataflow.Edge{
			{ID: 1, From: "P", To: "C1"},
			{ID: 2, From: "P", To: "C2"},
		},
	}
	progs := map[dataflow.OpID][]dataflow.Instr{
		"P":  {dataflow.AllocInstr("b"), dataflow.PublishInstr("b", 1, 2), dataflow.ReleaseInstr("b")},
		"C1": {dataflow.TakeInstr(1, "x"), dataflow.ReleaseInstr("x")},
		"C2": {dataflow.TakeInstr(2, "y"), dataflow.ReleaseInstr("y")},
	}
	d, err := dataflow.NewDriver(g, progs, dataflow.Config{Pool: 1, EdgeCaps: map[dataflow.EdgeID]int{1: 1, 2: 1}})
	if err != nil {
		log.Fatal(err)
	}
	step := func(op dataflow.OpID) dataflow.Record {
		rec, err := d.Step(op)
		if err != nil {
			var be *dataflow.BlockedError
			if errors.As(err, &be) {
				log.Fatalf("unexpected block: %v", be)
			}
			log.Fatal(err)
		}
		return *rec
	}
	bid := dataflow.BlockID{Origin: "P", Index: 0}

	step("P") // alloc
	step("P") // fanout publish
	step("P") // release producer ref
	fmt.Printf("after producer: refs=%d pool=%d\n", d.RefCount(bid), d.PoolUsed())

	step("C1") // take returns edge1 slot immediately
	step("C1") // release: C2 still references the block
	fmt.Printf("after C1: refs=%d pool=%d\n", d.RefCount(bid), d.PoolUsed())

	step("C2") // take
	rec := step("C2")
	fmt.Printf("after C2: last=%v pool=%d done=%v\n", rec.Event.Last, d.PoolUsed(), d.AllDone())
	// Output:
	// after producer: refs=2 pool=1
	// after C1: refs=1 pool=1
	// after C2: last=true pool=0 done=true
}
