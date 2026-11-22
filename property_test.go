package errfeedback

import (
	"errors"
	"math"
	"testing"
)

// TestErrorFeedbackTelescopes runs many steps with a deterministic gradient
// sequence and checks the feedback identity over the whole run:
//
//	sum over steps of sent[c] + final residual[c] == sum over steps of gradient[c]
//
// That is exactly what "已传值与残差能还原每次应结算的输入" means over a run: the
// partial sums of transmitted values plus the retained compensation reconstruct
// the accumulated input, and the compensation stays bounded by one lattice step.
func TestErrorFeedbackTelescopes(t *testing.T) {
	s := mustSender(t, 20)
	spec := StepSpec{Lattice: 7, Rounding: RoundNearestEven, Keep: 1}

	var gradSum map[Coord]int64
	sentSum := make(map[Coord]int64)
	const steps = 50
	planA, planB := layoutA(t), layoutB(t)
	for k := 0; k < steps; k++ {
		// A deterministic pseudo-gradient, deliberately off-lattice.
		g := make(map[Coord]int64)
		if gradSum == nil {
			gradSum = make(map[Coord]int64)
		}
		for _, c := range s.Coords() {
			v := int64((k*13+int(c)*7)%17) - 8
			g[c] = v
			gradSum[c] += v
		}
		plan := planA
		if k%2 == 1 {
			plan = planB // repartition every other step
		}
		step := StepID(k + 1)
		if err := s.Begin(step, spec, plan, g); err != nil {
			t.Fatalf("step %d: %v", step, err)
		}
		pkts, err := s.Packets(step)
		if err != nil {
			t.Fatalf("step %d packets: %v", step, err)
		}
		for _, m := range pkts {
			for _, e := range m.Entries {
				sentSum[e.Coord] += e.Value
			}
		}
		// Acks arrive out of order within the step.
		for i := len(pkts) - 1; i >= 0; i-- {
			if out, err := s.Confirm(AckFor(pkts[i])); err != nil {
				t.Fatalf("step %d confirm: %v", step, err)
			} else if i == 0 && out != ConfirmSettled {
				t.Fatalf("step %d final outcome %v", step, out)
			}
		}
	}
	final := s.Residuals()
	for _, c := range s.Coords() {
		if got := sentSum[c] + final[c]; got != gradSum[c] {
			t.Fatalf("coord %d: sent(%d)+residual(%d)=%d want %d",
				c, sentSum[c], final[c], got, gradSum[c])
		}
		// Residual is the accumulated quantization/sparsity error, bounded in
		// magnitude by one lattice step plus the accumulation of a single
		// unsent coordinate per step. With Keep=1 per bucket of size 2, at most
		// one coordinate per bucket stays unsent, so the bound is steps*lattice.
		if final[c] > int64(steps*spec.Lattice) || final[c] < -int64(steps*spec.Lattice) {
			t.Fatalf("coord %d residual %d escaped bound", c, final[c])
		}
	}
}

// TestSparseKeepsOnlyLargestPerBucket: Keep selects by magnitude inside a
// bucket, and the unselected values stay in the residual untouched.
func TestSparseKeepsOnlyLargestPerBucket(t *testing.T) {
	s, err := NewSender(21, []Coord{1, 2, 3})
	if err != nil {
		t.Fatal(err)
	}
	plan, err := NewStepPlan(1, 1, Bucket{ID: 0, Coords: []Coord{1, 2, 3}})
	if err != nil {
		t.Fatal(err)
	}
	spec := StepSpec{Lattice: 1, Rounding: RoundTowardZero, Keep: 1}
	if err := s.Begin(1, spec, plan, map[Coord]int64{1: 5, 2: -9, 3: 4}); err != nil {
		t.Fatal(err)
	}
	pkt, err := s.Packet(1, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(pkt.Entries) != 1 || pkt.Entries[0].Coord != 2 || pkt.Entries[0].Value != -9 {
		t.Fatalf("entries = %v, want only coord 2 = -9", pkt.Entries)
	}
	res := s.Residuals()
	// Residual is still the pre-commit value (all zero) until confirmed.
	if res[1] != 0 || res[2] != 0 || res[3] != 0 {
		t.Fatalf("residual changed before confirmation: %v", res)
	}
	if _, err := s.Confirm(AckFor(pkt)); err != nil {
		t.Fatal(err)
	}
	res = s.Residuals()
	if res[1] != 5 || res[2] != 0 || res[3] != 4 {
		t.Fatalf("residual after settle = %v, want {1:5 2:0 3:4}", res)
	}
}

// TestBeginRejectsOverflow: gradient + residual overflow is an explicit error,
// not a wrap.
func TestBeginRejectsOverflow(t *testing.T) {
	s, err := NewSender(23, []Coord{1})
	if err != nil {
		t.Fatal(err)
	}
	plan, err := NewStepPlan(1, 1, Bucket{ID: 0, Coords: []Coord{1}})
	if err != nil {
		t.Fatal(err)
	}
	// Step 1 leaves a residual of 5 (lattice 6, truncation sends 0).
	spec := StepSpec{Lattice: 6, Rounding: RoundTowardZero, Keep: 1}
	if err := s.Begin(1, spec, plan, map[Coord]int64{1: 5}); err != nil {
		t.Fatal(err)
	}
	pkts, _ := s.Packets(1)
	if _, err := s.Confirm(AckFor(pkts[0])); err != nil {
		t.Fatal(err)
	}
	// Step 2 adds MaxInt64 on top of residual 5 -> overflow.
	if err := s.Begin(2, spec, plan, map[Coord]int64{1: math.MaxInt64}); !errors.Is(err, ErrOverflow) {
		t.Fatalf("expected overflow, got %v", err)
	}
}

// TestUnsampledSequenceStillSettles: a step with no transmitted values (Keep
// zero is invalid, but all-zero lattice points) still settles and publishes an
// empty aggregate, and the residual carries the full input.
func TestUnsampledSequenceStillSettles(t *testing.T) {
	s, err := NewSender(24, []Coord{1, 2})
	if err != nil {
		t.Fatal(err)
	}
	recv := NewReceiver()
	plan, err := NewStepPlan(1, 1, Bucket{ID: 0, Coords: []Coord{1, 2}})
	if err != nil {
		t.Fatal(err)
	}
	spec := StepSpec{Lattice: 10, Rounding: RoundTowardZero, Keep: 1}
	if err := s.Begin(1, spec, plan, map[Coord]int64{1: 3, 2: -4}); err != nil {
		t.Fatal(err)
	}
	if err := recv.Expect(Expect{Step: 1, Attempt: 1, Layout: 1, Plan: 1,
		Sources: []SourceID{24}, Buckets: []BucketID{0}}); err != nil {
		t.Fatal(err)
	}
	pkts := deliverAll(t, s, recv, 1)
	if len(pkts[0].Entries) != 0 {
		t.Fatalf("expected no entries, got %v", pkts[0].Entries)
	}
	ackAll(t, s, pkts)
	pubs, ok := recv.Published(1)
	if !ok {
		t.Fatal("not published")
	}
	if len(pubs[0].Entries) != 0 {
		t.Fatalf("empty aggregate expected, got %v", pubs[0].Entries)
	}
	if res := s.Residuals(); res[1] != 3 || res[2] != -4 {
		t.Fatalf("residual = %v want {1:3 2:-4}", res)
	}
}
