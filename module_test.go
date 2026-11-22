package errfeedback

import (
	"errors"
	"sync"
	"testing"
)

// TestReceiverAcceptsOneMessagePerLogicalOrigin: a duplicate (step, source,
// bucket) is dropped, never summed, and the aggregate is unaffected.
func TestReceiverAcceptsOneMessagePerLogicalOrigin(t *testing.T) {
	s := mustSender(t, 5)
	r := NewReceiver()
	spec := StepSpec{Lattice: 2, Rounding: RoundNearestEven, Keep: 2}
	if err := s.Begin(1, spec, layoutA(t), grad(1, 2, 3, 4, 5, 6, 7, 8)); err != nil {
		t.Fatal(err)
	}
	if err := r.Expect(Expect{Step: 1, Attempt: 1, Layout: 100, Plan: 1,
		Sources: []SourceID{5}, Buckets: []BucketID{10, 11, 12, 13}}); err != nil {
		t.Fatal(err)
	}
	pkts, err := s.Packets(1)
	if err != nil {
		t.Fatal(err)
	}
	// Deliver the first packet three times before anything else.
	for i := 0; i < 3; i++ {
		res, err := r.Deliver(pkts[0])
		if i == 0 {
			if err != nil || res != AckAccepted {
				t.Fatalf("first deliver: %v %v", res, err)
			}
		} else {
			if res != AckDuplicate || !errors.Is(err, ErrDuplicateMessage) {
				t.Fatalf("duplicate deliver %d: %v %v", i, res, err)
			}
		}
	}
	accepted, total, _ := r.AcceptedCount(1)
	if accepted != 1 || total != 4 {
		t.Fatalf("accepted=%d total=%d want 1/4", accepted, total)
	}
	if _, ok := r.Published(1); ok {
		t.Fatal("published before all groups")
	}
	for _, m := range pkts[1:] {
		if res, err := r.Deliver(m); err != nil || res != AckAccepted {
			t.Fatalf("deliver bucket %d: %v %v", m.Bucket, res, err)
		}
	}
	pubs, ok := r.Published(1)
	if !ok {
		t.Fatal("not published after all groups")
	}
	// The aggregate must contain each coordinate at most once.
	seen := map[Coord]int{}
	for _, e := range pubs[0].Entries {
		seen[e.Coord]++
	}
	for c, n := range seen {
		if n > 1 {
			t.Fatalf("coord %d aggregated %d times", c, n)
		}
	}
}

// TestReceiverRejectsForeignSourceAndCorruption: messages from an unexpected
// source and corrupted packets never reach the aggregate.
func TestReceiverRejectsForeignSourceAndCorruption(t *testing.T) {
	s := mustSender(t, 5)
	foreign := mustSender(t, 6)
	r := NewReceiver()
	spec := StepSpec{Lattice: 2, Rounding: RoundNearestEven, Keep: 2}
	if err := s.Begin(1, spec, layoutA(t), grad(1, 2, 3, 4, 5, 6, 7, 8)); err != nil {
		t.Fatal(err)
	}
	if err := foreign.Begin(1, spec, layoutA(t), grad(1, 2, 3, 4, 5, 6, 7, 8)); err != nil {
		t.Fatal(err)
	}
	if err := r.Expect(Expect{Step: 1, Attempt: 1, Layout: 100, Plan: 1,
		Sources: []SourceID{5}, Buckets: []BucketID{10, 11, 12, 13}}); err != nil {
		t.Fatal(err)
	}
	foreignPkts, _ := foreign.Packets(1)
	if res, err := r.Deliver(foreignPkts[0]); res != AckRejected || !errors.Is(err, ErrUnknownSource) {
		t.Fatalf("foreign source: %v %v", res, err)
	}
	pkts, _ := s.Packets(1)
	bad := pkts[0]
	bad.Entries = append([]CoordValue(nil), bad.Entries...)
	bad.Entries[0].Value++
	if res, err := r.Deliver(bad); res != AckRejected || !errors.Is(err, ErrIntegrity) {
		t.Fatalf("corrupt: %v %v", res, err)
	}
	if accepted, _, _ := r.AcceptedCount(1); accepted != 0 {
		t.Fatalf("accepted=%d want 0 after rejects", accepted)
	}
}

// TestLateAckAfterSettleDoesNotDoubleWrite is the "旧包的迟到确认不得使残差多写
// 一次" requirement: once a step settles, replaying any of its acks must leave
// the residual untouched.
func TestLateAckAfterSettleDoesNotDoubleWrite(t *testing.T) {
	s := mustSender(t, 9)
	spec := StepSpec{Lattice: 4, Rounding: RoundNearestEven, Keep: 1}
	if err := s.Begin(1, spec, layoutA(t), grad(3, 2, 7, 4, 1, 6, 5, 9)); err != nil {
		t.Fatal(err)
	}
	pkts, err := s.Packets(1)
	if err != nil {
		t.Fatal(err)
	}
	out := ackAll(t, s, pkts)
	if out != ConfirmSettled {
		t.Fatalf("final = %v", out)
	}
	settled := s.Residuals()
	// Replay every ack, including the one that settled, many times.
	for round := 0; round < 5; round++ {
		for _, m := range pkts {
			o, err := s.Confirm(AckFor(m))
			if err != nil {
				t.Fatal(err)
			}
			if o != ConfirmDuplicate {
				t.Fatalf("replayed ack outcome = %v want duplicate", o)
			}
		}
	}
	if got := s.Residuals(); !equalResidual(settled, got) {
		t.Fatalf("residual changed on late acks: %v -> %v", settled, got)
	}
}

// TestStaleAndMismatchedAcksIgnored: wrong attempt, wrong layout, unknown
// bucket, wrong source are all non-committing.
func TestStaleAndMismatchedAcksIgnored(t *testing.T) {
	s := mustSender(t, 9)
	spec := StepSpec{Lattice: 4, Rounding: RoundNearestEven, Keep: 1}
	if err := s.Begin(1, spec, layoutA(t), grad(3, 2, 7, 4, 1, 6, 5, 9)); err != nil {
		t.Fatal(err)
	}
	pkts, _ := s.Packets(1)
	before := s.Residuals()

	badAcks := []Ack{
		{Step: 1, Source: 9, Bucket: 10, Attempt: 99, Layout: 100, Plan: 1},
		{Step: 1, Source: 9, Bucket: 10, Attempt: 1, Layout: 999, Plan: 1},
		{Step: 1, Source: 9, Bucket: 10, Attempt: 1, Layout: 100, Plan: 99},
		{Step: 1, Source: 9, Bucket: 777, Attempt: 1, Layout: 100, Plan: 1},
		{Step: 42, Source: 9, Bucket: 10, Attempt: 1, Layout: 100, Plan: 1},
	}
	for _, a := range badAcks {
		o, err := s.Confirm(a)
		if err != nil {
			t.Fatalf("ack %+v: %v", a, err)
		}
		if o != ConfirmIgnored {
			t.Fatalf("ack %+v outcome = %v want ignored", a, o)
		}
	}
	if _, err := s.Confirm(Ack{Step: 1, Source: 1234, Bucket: 10, Attempt: 1, Layout: 100, Plan: 1}); !errors.Is(err, ErrWrongSource) {
		t.Fatalf("foreign source ack err = %v", err)
	}
	if got := s.Residuals(); !equalResidual(before, got) {
		t.Fatalf("ignored acks changed residual")
	}
	// The good acks still work.
	if out := ackAll(t, s, pkts); out != ConfirmSettled {
		t.Fatalf("good acks after bad ones: %v", out)
	}
}

func equalResidual(a, b map[Coord]int64) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if b[k] != v {
			return false
		}
	}
	return true
}

// TestRecoveryResendsUnconfirmedStep is the "记录可从已确认步恢复并重发未确认数据"
// requirement: rebuild a sender from the journal, confirm the residual matches,
// and resend the open step's packets verbatim.
func TestRecoveryResendsUnconfirmedStep(t *testing.T) {
	s := mustSender(t, 11)
	j := NewJournal(11, coords1to8)
	if err := s.AttachJournal(j); err != nil {
		t.Fatal(err)
	}
	spec := StepSpec{Lattice: 3, Rounding: RoundNearestAway, Keep: 1}
	// Step 1 fully settles.
	if err := s.Begin(1, spec, layoutA(t), grad(4, -7, 3, 8, -2, 5, -9, 1)); err != nil {
		t.Fatal(err)
	}
	pkts1, _ := s.Packets(1)
	ackAll(t, s, pkts1)
	settledResidual := s.Residuals()

	// Step 2 begins, only half its acks arrive, then the process "crashes".
	if err := s.Begin(2, spec, layoutB(t), grad(2, 2, 2, 2, 2, 2, 2, 2)); err != nil {
		t.Fatal(err)
	}
	pkts2, _ := s.Packets(2)
	for _, m := range pkts2[:2] {
		if _, err := s.Confirm(AckFor(m)); err != nil {
			t.Fatal(err)
		}
	}

	entries := j.Entries()
	recovered, err := RecoverSender(entries)
	if err != nil {
		t.Fatalf("RecoverSender: %v", err)
	}
	if recovered.Source() != 11 {
		t.Fatalf("recovered source = %d want 11", recovered.Source())
	}
	if got := recovered.Residuals(); !equalResidual(settledResidual, got) {
		t.Fatalf("recovered residual %v != settled %v", got, settledResidual)
	}
	if status, ok := recovered.Status(2); !ok || status != StepOpen {
		t.Fatalf("step 2 status after recovery = %v,%v want open", status, ok)
	}
	// Resent packets are byte-identical (including checksum) to the originals.
	resend, err := recovered.Packets(2)
	if err != nil {
		t.Fatalf("resend: %v", err)
	}
	if len(resend) != len(pkts2) {
		t.Fatalf("resent %d packets want %d", len(resend), len(pkts2))
	}
	for i := range resend {
		if resend[i].Checksum != pkts2[i].Checksum {
			t.Fatalf("packet %d checksum %d != original %d", i, resend[i].Checksum, pkts2[i].Checksum)
		}
		if resend[i].ComputeChecksum() != pkts2[i].ComputeChecksum() {
			t.Fatalf("packet %d content differs after recovery", i)
		}
	}

	// Delivering the resent packets into a fresh receiver settles step 2, and
	// the settled residual matches a clean (uninterrupted) run.
	recv := NewReceiver()
	if err := recv.Expect(Expect{Step: 2, Attempt: 2, Layout: 200, Plan: 2,
		Sources: []SourceID{11}, Buckets: []BucketID{20, 21, 22, 23}}); err != nil {
		t.Fatal(err)
	}
	for _, m := range resend {
		if res, err := recv.Deliver(m); err != nil || res == AckRejected {
			t.Fatalf("deliver resent: %v %v", res, err)
		}
	}
	ackAll(t, recovered, resend)
	assertSettlement(t, recovered, grad(2, 2, 2, 2, 2, 2, 2, 2), settledResidual, resend, recovered.Residuals())

	// A clean run reproduces the recovered residual exactly.
	clean := mustSender(t, 11)
	if err := clean.Begin(1, spec, layoutA(t), grad(4, -7, 3, 8, -2, 5, -9, 1)); err != nil {
		t.Fatal(err)
	}
	cp1, _ := clean.Packets(1)
	ackAll(t, clean, cp1)
	if err := clean.Begin(2, spec, layoutB(t), grad(2, 2, 2, 2, 2, 2, 2, 2)); err != nil {
		t.Fatal(err)
	}
	cp2, _ := clean.Packets(2)
	ackAll(t, clean, cp2)
	if !equalResidual(clean.Residuals(), recovered.Residuals()) {
		t.Fatalf("recovered %v != clean %v", recovered.Residuals(), clean.Residuals())
	}
}

// TestRecoveryDetectsTampering: a modified or reordered journal is rejected
// rather than replayed into a wrong residual.
func TestRecoveryDetectsTampering(t *testing.T) {
	s := mustSender(t, 12)
	j := NewJournal(12, coords1to8)
	if err := s.AttachJournal(j); err != nil {
		t.Fatal(err)
	}
	spec := StepSpec{Lattice: 2, Rounding: RoundNearestEven, Keep: 2}
	if err := s.Begin(1, spec, layoutA(t), grad(1, 2, 3, 4, 5, 6, 7, 8)); err != nil {
		t.Fatal(err)
	}
	pkts, _ := s.Packets(1)
	ackAll(t, s, pkts)

	good := j.Entries()
	modified := j.Entries()
	modified[0].Gradient[0].Value += 1
	if _, err := RecoverSender(modified); !errors.Is(err, ErrJournalInconsistent) {
		t.Fatalf("tampered gradient: err = %v", err)
	}

	if len(good) >= 2 {
		reordered := j.Entries()
		reordered[0], reordered[1] = reordered[1], reordered[0]
		if _, err := RecoverSender(reordered); !errors.Is(err, ErrJournalInconsistent) {
			t.Fatalf("reordered: err = %v", err)
		}
	}

	// A truncated journal (drop the final confirm) replays to an open step and
	// a different residual, but stays internally consistent. RecoverSender must
	// still return a usable sender.
	if len(good) > 1 {
		trunc, err := RecoverSender(good[:len(good)-1])
		if err != nil {
			t.Fatalf("truncated replay: %v", err)
		}
		if status, ok := trunc.Status(1); !ok || status != StepOpen {
			t.Fatalf("truncated status = %v,%v want open", status, ok)
		}
	}
}

// TestAttemptsRejectReplayedStepIDs: reusing a settled step ID and stale
// attempts are refused.
func TestAttemptsRejectReplayedStepIDs(t *testing.T) {
	s := mustSender(t, 13)
	spec := StepSpec{Lattice: 2, Rounding: RoundNearestEven, Keep: 1}
	if err := s.Begin(1, spec, layoutA(t), grad(1, 1, 1, 1, 1, 1, 1, 1)); err != nil {
		t.Fatal(err)
	}
	pkts, _ := s.Packets(1)
	ackAll(t, s, pkts)
	if err := s.Begin(1, spec, layoutA(t), grad(1, 1, 1, 1, 1, 1, 1, 1)); !errors.Is(err, ErrStepSettled) {
		t.Fatalf("reused settled step: %v", err)
	}
	// Layout ID reuse with a different partition is rejected.
	if err := s.Begin(2, spec, layoutB(t), grad(1, 1, 1, 1, 1, 1, 1, 1)); err != nil {
		t.Fatal(err)
	}
	pkts2, _ := s.Packets(2)
	ackAll(t, s, pkts2)
	reused, err := NewStepPlan(3, 100,
		Bucket{ID: 10, Coords: []Coord{1, 2, 3}},
		Bucket{ID: 11, Coords: []Coord{4, 5, 6, 7, 8}},
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Begin(3, spec, reused, grad(1, 1, 1, 1, 1, 1, 1, 1)); !errors.Is(err, ErrStaleLayout) {
		t.Fatalf("layout reuse: %v", err)
	}
}

// TestPlanMustCoverCoordinates: plans that miss or add coordinates are rejected.
func TestPlanMustCoverCoordinates(t *testing.T) {
	s := mustSender(t, 14)
	spec := StepSpec{Lattice: 2, Rounding: RoundNearestEven, Keep: 1}
	missing, err := NewStepPlan(1, 1, Bucket{ID: 0, Coords: []Coord{1, 2, 3, 4, 5, 6, 7}})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Begin(1, spec, missing, grad(1, 1, 1, 1, 1, 1, 1, 1)); !errors.Is(err, ErrInvalidPlan) {
		t.Fatalf("missing coord: %v", err)
	}
	extra, err := NewStepPlan(2, 2, Bucket{ID: 0, Coords: []Coord{1, 2, 3, 4, 5, 6, 7, 8, 9}})
	if err != nil {
		t.Fatal(err)
	}
	if err := s.Begin(1, spec, extra, grad(1, 1, 1, 1, 1, 1, 1, 1)); !errors.Is(err, ErrInvalidPlan) {
		t.Fatalf("extra coord: %v", err)
	}
	if err := s.Begin(1, spec, layoutA(t), map[Coord]int64{1: 1}); !errors.Is(err, ErrInvalidSpec) {
		t.Fatalf("short gradient: %v", err)
	}
}

// TestRecoveredSenderKeepsJournaling: a recovered sender continues the journal
// chain rather than duplicating or truncating it, and a second recovery of the
// extended log lands on the same residual.
func TestRecoveredSenderKeepsJournaling(t *testing.T) {
	s := mustSender(t, 17)
	j := NewJournal(17, coords1to8)
	if err := s.AttachJournal(j); err != nil {
		t.Fatal(err)
	}
	spec := StepSpec{Lattice: 3, Rounding: RoundNearestEven, Keep: 2}
	if err := s.Begin(1, spec, layoutA(t), grad(1, 4, 2, 8, 5, 3, 7, 6)); err != nil {
		t.Fatal(err)
	}
	p1, _ := s.Packets(1)
	ackAll(t, s, p1)

	rec, err := RecoverSender(j.Entries())
	if err != nil {
		t.Fatal(err)
	}
	nBefore := j.Len()
	if rec == nil {
		t.Fatal("nil recovered sender")
	}
	// Continue on the recovered sender.
	if err := rec.Begin(2, spec, layoutB(t), grad(2, 2, 2, 2, 2, 2, 2, 2)); err != nil {
		t.Fatal(err)
	}
	p2, _ := rec.Packets(2)
	ackAll(t, rec, p2)
	after := rec.Residuals()

	// The recovered sender keeps its own journal, pre-loaded with the recovered
	// entries, and it grows by the new step's entries. A recovery from that
	// extended log must reproduce the same residual.
	rj := rec.Journal()
	if rj == nil {
		t.Fatal("recovered sender has no journal")
	}
	entries := rj.Entries() // same Journal instance was reattached by RecoverSender
	if len(entries) <= nBefore {
		t.Fatalf("recovered journal did not grow: %d -> %d", nBefore, len(entries))
	}
	again, err := RecoverSender(entries)
	if err != nil {
		t.Fatalf("second recovery: %v", err)
	}
	if !equalResidual(after, again.Residuals()) {
		t.Fatalf("second recovery residual %v != %v", again.Residuals(), after)
	}
}

// TestReceiverVoidThenExpect: aborting a step before its expectation arrives
// does not wedge the receiver; a later expectation is recorded and the step
// stays voided.
func TestReceiverVoidThenExpect(t *testing.T) {
	s := mustSender(t, 18)
	r := NewReceiver()
	spec := StepSpec{Lattice: 2, Rounding: RoundNearestEven, Keep: 1}
	if err := s.Begin(1, spec, layoutA(t), grad(1, 2, 3, 4, 5, 6, 7, 8)); err != nil {
		t.Fatal(err)
	}
	pkts, _ := s.Packets(1)
	r.AbortStep(1)
	if err := r.Expect(Expect{Step: 1, Attempt: 1, Layout: 100, Plan: 1,
		Sources: []SourceID{18}, Buckets: []BucketID{10, 11, 12, 13}}); err != nil {
		t.Fatalf("expect after void: %v", err)
	}
	if res, err := r.Deliver(pkts[0]); res != AckRejected || !errors.Is(err, ErrStepVoided) {
		t.Fatalf("deliver to voided: %v %v", res, err)
	}
	if _, ok := r.Published(1); ok {
		t.Fatal("voided step published")
	}
}

// TestConcurrentConfirmOnlySettlesOnce hammers a step with concurrent duplicate
// acks; exactly one call must settle and commit.
func TestConcurrentConfirmOnlySettlesOnce(t *testing.T) {
	s := mustSender(t, 15)
	spec := StepSpec{Lattice: 4, Rounding: RoundNearestEven, Keep: 2}
	if err := s.Begin(1, spec, layoutA(t), grad(1, 5, 2, 8, 3, 9, 4, 7)); err != nil {
		t.Fatal(err)
	}
	pkts, _ := s.Packets(1)
	const workers = 16
	results := make(chan ConfirmOutcome, workers*len(pkts))
	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for _, m := range pkts {
				o, err := s.Confirm(AckFor(m))
				if err != nil {
					t.Errorf("confirm: %v", err)
					return
				}
				results <- o
			}
		}()
	}
	wg.Wait()
	close(results)
	settled := 0
	for o := range results {
		if o == ConfirmSettled {
			settled++
		}
	}
	if settled != 1 {
		t.Fatalf("ConfirmSettled count = %d want 1", settled)
	}
}

// TestConcurrentDeliverDedupes runs duplicate deliveries concurrently; the
// aggregate must still contain each coordinate once.
func TestConcurrentDeliverDedupes(t *testing.T) {
	s := mustSender(t, 16)
	r := NewReceiver()
	spec := StepSpec{Lattice: 2, Rounding: RoundNearestEven, Keep: 2}
	if err := s.Begin(1, spec, layoutA(t), grad(1, 2, 3, 4, 5, 6, 7, 8)); err != nil {
		t.Fatal(err)
	}
	if err := r.Expect(Expect{Step: 1, Attempt: 1, Layout: 100, Plan: 1,
		Sources: []SourceID{16}, Buckets: []BucketID{10, 11, 12, 13}}); err != nil {
		t.Fatal(err)
	}
	pkts, _ := s.Packets(1)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for _, m := range pkts {
				_, _ = r.Deliver(m)
			}
		}()
	}
	wg.Wait()
	pubs, ok := r.Published(1)
	if !ok {
		t.Fatal("not published")
	}
	want := sentMap(pkts)
	got := map[Coord]int64{}
	for _, e := range pubs[0].Entries {
		got[e.Coord] = e.Value
	}
	if !equalResidual(want, got) {
		t.Fatalf("concurrent aggregate %v != %v", got, want)
	}
}
