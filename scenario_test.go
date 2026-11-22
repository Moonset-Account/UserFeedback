package errfeedback

import (
	"errors"
	"reflect"
	"testing"
)

// coords1to8 is the logical coordinate set shared by the scenario tests.
var coords1to8 = []Coord{1, 2, 3, 4, 5, 6, 7, 8}

// layoutA: four buckets of two coordinates.
func layoutA(t *testing.T) StepPlan {
	t.Helper()
	p, err := NewStepPlan(1, 100,
		Bucket{ID: 10, Coords: []Coord{1, 2}},
		Bucket{ID: 11, Coords: []Coord{3, 4}},
		Bucket{ID: 12, Coords: []Coord{5, 6}},
		Bucket{ID: 13, Coords: []Coord{7, 8}},
	)
	if err != nil {
		t.Fatalf("layoutA: %v", err)
	}
	return p
}

// layoutB regroups the same coordinates so every bucket mixes old buckets.
// This is the repartition-mid-flight plan.
func layoutB(t *testing.T) StepPlan {
	t.Helper()
	p, err := NewStepPlan(2, 200,
		Bucket{ID: 20, Coords: []Coord{1, 8}},
		Bucket{ID: 21, Coords: []Coord{2, 7}},
		Bucket{ID: 22, Coords: []Coord{3, 6}},
		Bucket{ID: 23, Coords: []Coord{4, 5}},
	)
	if err != nil {
		t.Fatalf("layoutB: %v", err)
	}
	return p
}

func mustSender(t *testing.T, src SourceID) *Sender {
	t.Helper()
	s, err := NewSender(src, coords1to8)
	if err != nil {
		t.Fatalf("NewSender: %v", err)
	}
	return s
}

func grad(vals ...int64) map[Coord]int64 {
	m := make(map[Coord]int64, len(vals))
	for i, v := range vals {
		m[Coord(i+1)] = v
	}
	return m
}

// deliverAll ships every packet of an open step to the receiver.
func deliverAll(t *testing.T, s *Sender, r *Receiver, step StepID) []SparseMessage {
	t.Helper()
	pkts, err := s.Packets(step)
	if err != nil {
		t.Fatalf("Packets(%d): %v", step, err)
	}
	for _, m := range pkts {
		res, err := r.Deliver(m)
		if err != nil && !errors.Is(err, ErrDuplicateMessage) {
			t.Fatalf("Deliver step %d bucket %d: %v", step, m.Bucket, err)
		}
		if res == AckRejected {
			t.Fatalf("Deliver step %d bucket %d rejected", step, m.Bucket)
		}
	}
	return pkts
}

func ackAll(t *testing.T, s *Sender, pkts []SparseMessage) ConfirmOutcome {
	t.Helper()
	var last ConfirmOutcome
	for _, m := range pkts {
		out, err := s.Confirm(AckFor(m))
		if err != nil {
			t.Fatalf("Confirm step %d bucket %d: %v", m.Step, m.Bucket, err)
		}
		last = out
	}
	return last
}

func expectContribution(t *testing.T, pubs []Contribution, src SourceID) map[Coord]int64 {
	t.Helper()
	for _, c := range pubs {
		if c.Source == src {
			m := make(map[Coord]int64, len(c.Entries))
			for _, e := range c.Entries {
				m[e.Coord] = e.Value
			}
			return m
		}
	}
	t.Fatalf("source %d not published", src)
	return nil
}

func stepAttempt(t *testing.T, s *Sender, step StepID) uint64 {
	t.Helper()
	a, ok := s.StepAttempt(step)
	if !ok {
		t.Fatalf("no attempt for step %d", step)
	}
	return a
}

// sentMap collects the transmitted sparse values per coordinate.
func sentMap(pkts []SparseMessage) map[Coord]int64 {
	m := make(map[Coord]int64)
	for _, p := range pkts {
		for _, e := range p.Entries {
			m[e.Coord] += e.Value
		}
	}
	for c, v := range m {
		if v == 0 {
			delete(m, c)
		}
	}
	return m
}

// quantizedMap collects the full lattice points chosen for each coordinate.
func quantizedMap(pkts []SparseMessage) map[Coord]int64 {
	m := make(map[Coord]int64)
	for _, p := range pkts {
		for _, e := range p.Quantized {
			m[e.Coord] = e.Value
		}
	}
	return m
}

// assertAggregateMatchesPackets: the receiver-published aggregate equals the
// per-coordinate sum of the transmitted sparse entries.
func assertAggregateMatchesPackets(t *testing.T, agg map[Coord]int64, pkts []SparseMessage) {
	t.Helper()
	if want := sentMap(pkts); !reflect.DeepEqual(agg, want) {
		t.Fatalf("aggregate %v != packet sum %v", agg, want)
	}
}

// assertSubsetOfQuantized: every published value is exactly the sent sparse
// value, which in turn is one of the quantization lattice points.
func assertSubsetOfQuantized(t *testing.T, agg map[Coord]int64, pkts []SparseMessage) {
	t.Helper()
	q := quantizedMap(pkts)
	for c, v := range agg {
		qv, ok := q[c]
		if !ok {
			t.Fatalf("aggregate coord %d has no quantized lattice point", c)
		}
		if qv != v {
			t.Fatalf("aggregate coord %d = %d, quantized lattice = %d", c, v, qv)
		}
	}
}

// assertSettlement proves the core identity per coordinate:
//
//	sent[c] + confirmedResidual[c] == gradient[c] + priorResidual[c]
//
// i.e. the transmitted value plus the residual retained by the sender
// reconstructs the exact input the step should settle. Unsent coordinates have
// sent[c] == 0, so their whole input stays as residual.
func assertSettlement(t *testing.T, s *Sender, gradient, priorResidual map[Coord]int64, pkts []SparseMessage, confirmed map[Coord]int64) {
	t.Helper()
	sent := sentMap(pkts)
	for _, c := range s.Coords() {
		wantInput := gradient[c] + priorResidual[c]
		got := sent[c] + confirmed[c]
		if got != wantInput {
			t.Fatalf("coord %d: sent(%d)+residual(%d)=%d want input %d",
				c, sent[c], confirmed[c], got, wantInput)
		}
	}
}

// TestTwoSourcesInterleavedBucketsAndRepartition is the headline scenario: two
// independent senders, two steps whose buckets interleave differently, and a
// repartition of the coordinate-to-bucket mapping between them.
func TestTwoSourcesInterleavedBucketsAndRepartition(t *testing.T) {
	srcA := mustSender(t, 1)
	srcB := mustSender(t, 2)
	recv := NewReceiver()

	spec := StepSpec{Lattice: 4, Rounding: RoundNearestEven, Keep: 1}
	planA := layoutA(t)
	planB := layoutB(t)

	gradA1 := grad(3, -5, 6, 7, -1, 0, 9, -2)
	gradB1 := grad(-4, 4, 4, -4, 8, -8, 1, 1)

	if err := srcA.Begin(1, spec, planA, gradA1); err != nil {
		t.Fatal(err)
	}
	if err := srcB.Begin(1, spec, planA, gradB1); err != nil {
		t.Fatal(err)
	}
	if err := recv.Expect(Expect{
		Step: 1, Attempt: stepAttempt(t, srcA, 1), Layout: 100, Plan: 1,
		Sources: []SourceID{1, 2}, Buckets: []BucketID{10, 11, 12, 13},
	}); err != nil {
		t.Fatal(err)
	}

	pktsA1 := deliverAll(t, srcA, recv, 1)
	pktsB1 := deliverAll(t, srcB, recv, 1)
	if out := ackAll(t, srcA, pktsA1); out != ConfirmSettled {
		t.Fatalf("srcA step1 final outcome = %v", out)
	}
	if out := ackAll(t, srcB, pktsB1); out != ConfirmSettled {
		t.Fatalf("srcB step1 final outcome = %v", out)
	}
	resA1 := srcA.Residuals()
	resB1 := srcB.Residuals()

	pubs, ok := recv.Published(1)
	if !ok {
		t.Fatal("step 1 not published")
	}
	assertAggregateMatchesPackets(t, expectContribution(t, pubs, 1), pktsA1)
	assertAggregateMatchesPackets(t, expectContribution(t, pubs, 2), pktsB1)
	assertSubsetOfQuantized(t, expectContribution(t, pubs, 1), pktsA1)
	assertSubsetOfQuantized(t, expectContribution(t, pubs, 2), pktsB1)
	assertSettlement(t, srcA, gradA1, map[Coord]int64{}, pktsA1, resA1)
	assertSettlement(t, srcB, gradB1, map[Coord]int64{}, pktsB1, resB1)

	// Step 2 reuses the same coordinates but with layoutB. The residual must
	// follow the logical coordinate, not the old bucket index.
	gradA2 := grad(1, 1, 1, 1, 1, 1, 1, 1)
	gradB2 := grad(-2, -2, -2, -2, -2, -2, -2, -2)

	if err := srcA.Begin(2, spec, planB, gradA2); err != nil {
		t.Fatal(err)
	}
	if err := srcB.Begin(2, spec, planB, gradB2); err != nil {
		t.Fatal(err)
	}
	if err := recv.Expect(Expect{
		Step: 2, Attempt: stepAttempt(t, srcA, 2), Layout: 200, Plan: 2,
		Sources: []SourceID{1, 2}, Buckets: []BucketID{20, 21, 22, 23},
	}); err != nil {
		t.Fatal(err)
	}
	pktsA2 := deliverAll(t, srcA, recv, 2)
	pktsB2 := deliverAll(t, srcB, recv, 2)
	ackAll(t, srcA, pktsA2)
	ackAll(t, srcB, pktsB2)
	resA2 := srcA.Residuals()
	resB2 := srcB.Residuals()

	pubs2, ok := recv.Published(2)
	if !ok {
		t.Fatal("step 2 not published")
	}
	assertSubsetOfQuantized(t, expectContribution(t, pubs2, 1), pktsA2)
	assertSubsetOfQuantized(t, expectContribution(t, pubs2, 2), pktsB2)

	// Reconstruction uses the residual committed by step 1, keyed by logical
	// coordinate, not any bucket index.
	assertSettlement(t, srcA, gradA2, resA1, pktsA2, resA2)
	assertSettlement(t, srcB, gradB2, resB1, pktsB2, resB2)
}

// TestResidualMigratesWithCoordinateOnRepartition proves the repartition rule
// directly: a coordinate whose bucket changes across layouts keeps the same
// compensation, and its value is retransmitted from its new packet.
func TestResidualMigratesWithCoordinateOnRepartition(t *testing.T) {
	s, err := NewSender(7, []Coord{1, 2, 3, 4})
	if err != nil {
		t.Fatal(err)
	}
	recv := NewReceiver()
	// Lattice 6 with truncation: input 5 quantizes to 0, leaving residual 5.
	spec := StepSpec{Lattice: 6, Rounding: RoundTowardZero, Keep: 1}

	p1, err := NewStepPlan(1, 1, Bucket{ID: 0, Coords: []Coord{1, 2}}, Bucket{ID: 1, Coords: []Coord{3, 4}})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Begin(1, spec, p1, map[Coord]int64{1: 5, 2: 1, 3: -6, 4: 2}); err != nil {
		t.Fatal(err)
	}
	if err := recv.Expect(Expect{Step: 1, Attempt: 1, Layout: 1, Plan: 1,
		Sources: []SourceID{7}, Buckets: []BucketID{0, 1}}); err != nil {
		t.Fatal(err)
	}
	ackAll(t, s, deliverAll(t, s, recv, 1))

	if r1, _ := s.Residual(1); r1 != 5 {
		t.Fatalf("residual(1) after step 1 = %d want 5", r1)
	}

	// Under layout 2, coordinate 1 moves from bucket 0 to bucket 1. Adding the
	// gradient 8 gives input 13, which truncates to lattice point 12: the
	// compensation migrated with the coordinate and is now transmitted from
	// the new bucket.
	p2, err := NewStepPlan(2, 2, Bucket{ID: 0, Coords: []Coord{2, 3}}, Bucket{ID: 1, Coords: []Coord{1, 4}})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Begin(2, spec, p2, map[Coord]int64{1: 8, 2: 0, 3: 0, 4: 0}); err != nil {
		t.Fatal(err)
	}
	pkt1, err := s.Packet(2, 1)
	if err != nil {
		t.Fatal(err)
	}
	var q1 int64 = -999
	for _, e := range pkt1.Quantized {
		if e.Coord == 1 {
			q1 = e.Value
		}
	}
	if q1 != 12 {
		t.Fatalf("step2 coord1 quantized = %d want 12 (residual migrated)", q1)
	}
	var sentVal int64
	var sent bool
	for _, e := range pkt1.Entries {
		if e.Coord == 1 {
			sent, sentVal = true, e.Value
		}
	}
	if !sent || sentVal != 12 {
		t.Fatalf("coord 1 should be sent with value 12 from new bucket 1, entries=%v", pkt1.Entries)
	}
}

// TestAbortMidwayThenRepartition: a step is abandoned halfway, its packets are
// dropped, the residual is untouched, the next step repartitions, and only then
// is compensation committed.
func TestAbortMidwayThenRepartition(t *testing.T) {
	s := mustSender(t, 3)
	recv := NewReceiver()
	spec := StepSpec{Lattice: 4, Rounding: RoundNearestEven, Keep: 2}

	if err := s.Begin(1, spec, layoutA(t), grad(3, 3, 3, 3, 3, 3, 3, 3)); err != nil {
		t.Fatal(err)
	}
	if err := recv.Expect(Expect{Step: 1, Attempt: 1, Layout: 100, Plan: 1,
		Sources: []SourceID{3}, Buckets: []BucketID{10, 11, 12, 13}}); err != nil {
		t.Fatal(err)
	}
	ackAll(t, s, deliverAll(t, s, recv, 1))
	resAfter1 := s.Residuals()

	// Step 2 begins under layoutB, one bucket is delivered, then the step is
	// abandoned. Remaining deliveries must be refused.
	if err := s.Begin(2, spec, layoutB(t), grad(9, 9, 9, 9, 9, 9, 9, 9)); err != nil {
		t.Fatal(err)
	}
	if err := recv.Expect(Expect{Step: 2, Attempt: 2, Layout: 200, Plan: 2,
		Sources: []SourceID{3}, Buckets: []BucketID{20, 21, 22, 23}}); err != nil {
		t.Fatal(err)
	}
	pkts, err := s.Packets(2)
	if err != nil {
		t.Fatal(err)
	}
	if res, err := recv.Deliver(pkts[0]); err != nil || res != AckAccepted {
		t.Fatalf("first delivery: %v %v", res, err)
	}
	if _, err := s.Confirm(AckFor(pkts[0])); err != nil {
		t.Fatal(err)
	}
	if status := s.Abort(2); status != StepVoided {
		t.Fatalf("Abort(2)=%v want voided", status)
	}
	recv.AbortStep(2)

	if got := s.Residuals(); !reflect.DeepEqual(resAfter1, got) {
		t.Fatalf("abort changed residual: %v -> %v", resAfter1, got)
	}
	for _, m := range pkts[1:] {
		out, err := s.Confirm(AckFor(m))
		if err != nil {
			t.Fatal(err)
		}
		if out != ConfirmIgnored {
			t.Fatalf("late confirm after abort = %v want ignored", out)
		}
	}
	if got := s.Residuals(); !reflect.DeepEqual(resAfter1, got) {
		t.Fatalf("late confirms changed residual: %v -> %v", resAfter1, got)
	}
	if _, err := s.Packets(2); !errors.Is(err, ErrStepVoided) {
		t.Fatalf("Packets on voided step: %v", err)
	}
	if pubs, ok := recv.Published(2); ok {
		t.Fatalf("voided step published %v", pubs)
	}
	if res, err := recv.Deliver(pkts[1]); res != AckRejected || !errors.Is(err, ErrStepVoided) {
		t.Fatalf("delivery to voided step = %v, %v", res, err)
	}

	// Step 3 re-partitions again and settles. Compensation step 2 never
	// committed is now carried at the coordinate level.
	grad3 := grad(0, 0, 0, 0, 0, 0, 0, 0)
	if err := s.Begin(3, spec, layoutA(t), grad3); err != nil {
		t.Fatal(err)
	}
	if err := recv.Expect(Expect{Step: 3, Attempt: 3, Layout: 100, Plan: 1,
		Sources: []SourceID{3}, Buckets: []BucketID{10, 11, 12, 13}}); err != nil {
		t.Fatal(err)
	}
	pkts3 := deliverAll(t, s, recv, 3)
	ackAll(t, s, pkts3)
	assertSettlement(t, s, grad3, resAfter1, pkts3, s.Residuals())
}
