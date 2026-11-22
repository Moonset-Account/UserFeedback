package errfeedback

import "testing"

// TestGoldenSequenceTwoSourcesInterleavedAbortRepartition is the single
// end-to-end sequence the task asks for, checked against hand-computed numbers:
//
//   - two logical sources,
//   - two mutually interleaved buckets,
//   - a step that is abandoned halfway,
//   - a repartition afterwards,
//
// with the invariant checked explicitly at every step:
//
//	sent[c] + residualAfter[c] == gradient[c] + residualBefore[c]
//
// "已传值与残差能还原每次应结算的输入".
//
// The rule is Lattice=4, RoundNearestAway, Keep=1.
func TestGoldenSequenceTwoSourcesInterleavedAbortRepartition(t *testing.T) {
	coords := []Coord{1, 2, 3, 4}
	newSrc := func(id SourceID) *Sender {
		s, err := NewSender(id, coords)
		if err != nil {
			t.Fatalf("NewSender(%d): %v", id, err)
		}
		return s
	}
	interleaved, err := NewStepPlan(10, 300,
		Bucket{ID: 0, Coords: []Coord{1, 3}},
		Bucket{ID: 1, Coords: []Coord{2, 4}},
	)
	if err != nil {
		t.Fatal(err)
	}
	split, err := NewStepPlan(20, 400,
		Bucket{ID: 0, Coords: []Coord{1, 2}},
		Bucket{ID: 1, Coords: []Coord{3, 4}},
	)
	if err != nil {
		t.Fatal(err)
	}
	spec := StepSpec{Lattice: 4, Rounding: RoundNearestAway, Keep: 1}

	srcA := newSrc(1)
	srcB := newSrc(2)
	recv := NewReceiver()

	expectAll := func(step StepID, attempt uint64, layout LayoutID, plan PlanID) {
		t.Helper()
		if err := recv.Expect(Expect{
			Step: step, Attempt: attempt, Layout: layout, Plan: plan,
			Sources: []SourceID{1, 2}, Buckets: []BucketID{0, 1},
		}); err != nil {
			t.Fatalf("Expect step %d: %v", step, err)
		}
	}

	// assertStep checks the exact sent map against the hand-computed values,
	// that the sender's actual committed residual equals the hand-computed
	// expected residual, and the reconstruction identity for every coordinate.
	assertStep := func(name string, s *Sender, pkts []SparseMessage, sentWant, grad, before, afterWant map[Coord]int64) {
		t.Helper()
		if got := sentMap(pkts); !equalMap(got, sentWant) {
			t.Fatalf("%s sent = %v want %v", name, got, sentWant)
		}
		actual := s.Residuals()
		if !equalMap(actual, afterWant) {
			t.Fatalf("%s actual residual = %v want %v", name, actual, afterWant)
		}
		for _, c := range coords {
			if sentWant[c]+actual[c] != grad[c]+before[c] {
				t.Fatalf("%s coord %d: sent(%d)+residual(%d) != grad(%d)+before(%d)",
					name, c, sentWant[c], actual[c], grad[c], before[c])
			}
		}
	}

	// ---- Step 1: interleaved buckets ----
	gradA1 := map[Coord]int64{1: 3, 2: -6, 3: 7, 4: 1}
	gradB1 := map[Coord]int64{1: -7, 2: 5, 3: 2, 4: 6}
	zero := map[Coord]int64{1: 0, 2: 0, 3: 0, 4: 0}

	if err := srcA.Begin(1, spec, interleaved, gradA1); err != nil {
		t.Fatal(err)
	}
	if err := srcB.Begin(1, spec, interleaved, gradB1); err != nil {
		t.Fatal(err)
	}
	expectAll(1, 1, 300, 10)

	// Interleave delivery: A/b1, B/b0, A/b0, B/b1.
	deliverOne(t, recv, srcA, 1, 1)
	deliverOne(t, recv, srcB, 1, 0)
	deliverOne(t, recv, srcA, 1, 0)
	deliverOne(t, recv, srcB, 1, 1)
	pktsA1, _ := srcA.Packets(1)
	pktsB1, _ := srcB.Packets(1)
	ackAll(t, srcA, pktsA1)
	ackAll(t, srcB, pktsB1)

	assertStep("step1 A", srcA, pktsA1,
		map[Coord]int64{2: -8, 3: 8}, gradA1, zero,
		map[Coord]int64{1: 3, 2: 2, 3: -1, 4: 1})
	assertStep("step1 B", srcB, pktsB1,
		map[Coord]int64{1: -8, 4: 8}, gradB1, zero,
		map[Coord]int64{1: 1, 2: 5, 3: 2, 4: -2})
	resA1, resB1 := srcA.Residuals(), srcB.Residuals()
	assertAggregateMatchesPackets(t, expectContribution(t, mustPublish(t, recv, 1), 1), pktsA1)
	assertAggregateMatchesPackets(t, expectContribution(t, mustPublish(t, recv, 1), 2), pktsB1)

	// ---- Step 2: repartition to split buckets ----
	gradA2 := map[Coord]int64{1: 5, 2: -3, 3: 1, 4: 2}
	gradB2 := map[Coord]int64{1: 1, 2: 1, 3: 1, 4: 1}
	if err := srcA.Begin(2, spec, split, gradA2); err != nil {
		t.Fatal(err)
	}
	if err := srcB.Begin(2, spec, split, gradB2); err != nil {
		t.Fatal(err)
	}
	expectAll(2, 2, 400, 20)
	pktsA2 := deliverAll(t, srcA, recv, 2)
	pktsB2 := deliverAll(t, srcB, recv, 2)
	ackAll(t, srcA, pktsA2)
	ackAll(t, srcB, pktsB2)

	assertStep("step2 A", srcA, pktsA2,
		map[Coord]int64{1: 8, 4: 4}, gradA2, resA1,
		map[Coord]int64{1: 0, 2: -1, 3: 0, 4: -1})
	assertStep("step2 B", srcB, pktsB2,
		map[Coord]int64{2: 8, 3: 4}, gradB2, resB1,
		map[Coord]int64{1: 2, 2: -2, 3: -1, 4: -1})
	resA2, resB2 := srcA.Residuals(), srcB.Residuals()

	// ---- Step 3: abandoned halfway, interleaved layout ----
	gradA3 := map[Coord]int64{1: 9, 2: 9, 3: 9, 4: 9}
	gradB3 := map[Coord]int64{1: -9, 2: -9, 3: -9, 4: -9}
	if err := srcA.Begin(3, spec, interleaved, gradA3); err != nil {
		t.Fatal(err)
	}
	if err := srcB.Begin(3, spec, interleaved, gradB3); err != nil {
		t.Fatal(err)
	}
	expectAll(3, 3, 300, 10)
	// Accept and confirm only bucket 0 for each source, then abandon.
	deliverOne(t, recv, srcA, 3, 0)
	deliverOne(t, recv, srcB, 3, 0)
	pA3, _ := srcA.Packet(3, 0)
	pB3, _ := srcB.Packet(3, 0)
	if out, err := srcA.Confirm(AckFor(pA3)); err != nil || out != ConfirmRecorded {
		t.Fatalf("A step3 partial confirm: %v %v", out, err)
	}
	if out, err := srcB.Confirm(AckFor(pB3)); err != nil || out != ConfirmRecorded {
		t.Fatalf("B step3 partial confirm: %v %v", out, err)
	}
	if srcA.Abort(3) != StepVoided || srcB.Abort(3) != StepVoided {
		t.Fatal("abort did not void")
	}
	recv.AbortStep(3)
	if !equalMap(srcA.Residuals(), resA2) || !equalMap(srcB.Residuals(), resB2) {
		t.Fatalf("abort changed residual: A %v B %v", srcA.Residuals(), srcB.Residuals())
	}
	if _, ok := recv.Published(3); ok {
		t.Fatal("aborted step 3 was published")
	}

	// ---- Step 4: repartition again, settle ----
	gradA4 := map[Coord]int64{1: 1, 2: 2, 3: 3, 4: 4}
	gradB4 := map[Coord]int64{1: -1, 2: -2, 3: -3, 4: -4}
	if err := srcA.Begin(4, spec, split, gradA4); err != nil {
		t.Fatal(err)
	}
	if err := srcB.Begin(4, spec, split, gradB4); err != nil {
		t.Fatal(err)
	}
	expectAll(4, 4, 400, 20)
	pktsA4 := deliverAll(t, srcA, recv, 4)
	pktsB4 := deliverAll(t, srcB, recv, 4)
	ackAll(t, srcA, pktsA4)
	ackAll(t, srcB, pktsB4)

	assertStep("step4 A", srcA, pktsA4,
		map[Coord]int64{3: 4}, gradA4, resA2,
		map[Coord]int64{1: 1, 2: 1, 3: -1, 4: 3})
	assertStep("step4 B", srcB, pktsB4,
		map[Coord]int64{2: -4, 3: -4}, gradB4, resB2,
		map[Coord]int64{1: 1, 2: 0, 3: 0, 4: -5})

	// Final receiver aggregates equal what each source transmitted.
	pubs4 := mustPublish(t, recv, 4)
	assertAggregateMatchesPackets(t, expectContribution(t, pubs4, 1), pktsA4)
	assertAggregateMatchesPackets(t, expectContribution(t, pubs4, 2), pktsB4)
}

func mustPublish(t *testing.T, r *Receiver, step StepID) []Contribution {
	t.Helper()
	pubs, ok := r.Published(step)
	if !ok {
		t.Fatalf("step %d not published", step)
	}
	return pubs
}

// deliverOne sends one bucket packet of a step to the receiver.
func deliverOne(t *testing.T, r *Receiver, s *Sender, step StepID, bucket BucketID) {
	t.Helper()
	m, err := s.Packet(step, bucket)
	if err != nil {
		t.Fatalf("Packet(%d,%d): %v", step, bucket, err)
	}
	if res, err := r.Deliver(m); err != nil || res != AckAccepted {
		t.Fatalf("Deliver(%d,%d): %v %v", step, bucket, res, err)
	}
}

func equalMap(a, b map[Coord]int64) bool {
	if len(a) != len(b) {
		// Treat absent and zero identically.
		all := make(map[Coord]struct{}, len(a)+len(b))
		for k := range a {
			all[k] = struct{}{}
		}
		for k := range b {
			all[k] = struct{}{}
		}
		for k := range all {
			if a[k] != b[k] {
				return false
			}
		}
		return true
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}
