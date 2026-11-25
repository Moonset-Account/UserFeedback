package zonetransfer

import "fmt"

// ExamplePlanTransfer_serialWrap demonstrates a history whose SOA serials
// walk 0xFFFFFFFE -> 0xFFFFFFFF -> 0: the last step crosses the 32-bit
// boundary and must still chain as a forward increment.
func ExamplePlanTransfer_serialWrap() {
	r1 := rec("a.example.", "A", "192.0.2.1")
	r2 := rec("b.example.", "A", "192.0.2.2")
	r3 := rec("c.example.", "A", "192.0.2.3")
	r4 := rec("d.example.", "A", "192.0.2.4")
	r5 := rec("e.example.", "A", "192.0.2.5")

	snapshots := []Snapshot{
		snap("v1", 0xFFFFFFFE, r1, r2),
		snap("v2", 0xFFFFFFFF, r1, r2, r3),
		snap("v3", 0x00000000, r1, r2, r3, r4, r5),
	}
	deltas := []Delta{
		delta("v1", "v2", 0xFFFFFFFE, 0xFFFFFFFF, nil, []Record{r3}, 2),
		delta("v2", "v3", 0xFFFFFFFF, 0x00000000, nil, []Record{r4, r5}, 3),
	}

	plan, _ := PlanTransfer(snapshots, deltas, "v3", 0xFFFFFFFE)
	fmt.Println("kind:", plan.Kind)
	for _, d := range plan.Chain {
		fmt.Printf("step %s(serial %d) -> %s(serial %d), %d bytes\n",
			d.FromVersion, d.FromSerial, d.ToVersion, d.ToSerial, d.Bytes)
	}
	fmt.Println("total bytes:", plan.Bytes)

	// The receiver serves v1 throughout and only commits after the final
	// version verifies.
	rcv := NewReceiver(snapshots[0])
	_ = rcv.Apply(plan, func(step int, served Snapshot, _ []Record) {
		fmt.Printf("after step %d still serving: %s\n", step, served.Version)
	})
	fmt.Println("committed:", rcv.Current().Version, "serial", rcv.Current().Serial)
	// Output:
	// kind: IXFR
	// step v1(serial 4294967294) -> v2(serial 4294967295), 2 bytes
	// step v2(serial 4294967295) -> v3(serial 0), 3 bytes
	// total bytes: 5
	// after step 1 still serving: v1
	// after step 2 still serving: v1
	// committed: v3 serial 0
}

// ExamplePlanTransfer_mismatchedShorterEdge adds a one-byte edge that claims
// to start at the backup's version but cannot replay against its actual
// content. The planner discards it and keeps the longer, honest chain.
func ExamplePlanTransfer_mismatchedShorterEdge() {
	r1 := rec("a.example.", "A", "192.0.2.1")
	r2 := rec("b.example.", "A", "192.0.2.2")
	r3 := rec("c.example.", "A", "192.0.2.3")
	r4 := rec("d.example.", "A", "192.0.2.4")
	r5 := rec("e.example.", "A", "192.0.2.5")

	snapshots := []Snapshot{
		snap("v1", 0xFFFFFFFE, r1, r2),
		snap("v2", 0xFFFFFFFF, r1, r2, r3),
		snap("v3", 0x00000000, r1, r2, r3, r4, r5),
	}
	deltas := []Delta{
		delta("v1", "v2", 0xFFFFFFFE, 0xFFFFFFFF, nil, []Record{r3}, 2),
		delta("v2", "v3", 0xFFFFFFFF, 0x00000000, nil, []Record{r4, r5}, 3),
		// One byte, seemingly cheapest, but it deletes r3 which does not
		// exist in v1: replaying it against the backup's real zone fails.
		delta("v1", "v3", 0xFFFFFFFE, 0x00000000,
			[]Record{r3}, []Record{r3, r4, r5}, 1),
	}

	plan, _ := PlanTransfer(snapshots, deltas, "v3", 0xFFFFFFFE)
	fmt.Println("kind:", plan.Kind)
	fmt.Println("total bytes:", plan.Bytes, "(the 1-byte edge was rejected)")
	// Output:
	// kind: IXFR
	// total bytes: 5 (the 1-byte edge was rejected)
}
