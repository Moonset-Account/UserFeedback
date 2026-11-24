package reinject

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"reflect"
	"sync"
	"testing"
)

const (
	custA   = "customer-a"
	custB   = "customer-b"
	keyA    = "key-customer-a"
	keyB    = "key-customer-b"
	poison  = MessageID("msg-poison-3")
	repair1 = "repair-biz-1"
)

func baseDeadLetter() DeadLetter {
	return DeadLetter{
		MessageID:       poison,
		Key:             keyA,
		SeqInKey:        3,
		CustomerID:      custA,
		OriginalPayload: []byte(`{"bill":"bad"}`),
	}
}

func baseRepair() Repair {
	return Repair{
		RepairID:        repair1,
		MessageID:       poison,
		CustomerID:      custA,
		RepairedPayload: []byte(`{"bill":"fixed"}`),
	}
}

func newHarness(t *testing.T) (*Engine, *MemoryProgress, *FakeBroker, *FakeReview) {
	t.Helper()
	progress := NewMemoryProgress()
	broker := &FakeBroker{}
	review := &FakeReview{}
	engine, err := NewEngine(progress, broker, review)
	if err != nil {
		t.Fatalf("NewEngine: %v", err)
	}
	return engine, progress, broker, review
}

func wantAction(t *testing.T, d Decision, g Group, kind ActionKind) GroupAction {
	t.Helper()
	a, ok := d.ActionFor(g)
	if !ok {
		t.Fatalf("no action recorded for group %q; got %+v", g, d.Actions)
	}
	if a.Action != kind {
		t.Fatalf("group %q action = %s, want %s", g, a.Action, kind)
	}
	return a
}

// TestPerGroupRouting covers the headline scenario: group A is still stuck
// before the poisoned position while group B has irrevocably skipped it.
func TestPerGroupRouting(t *testing.T) {
	engine, progress, broker, review := newHarness(t)

	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})
	progress.MarkSkip("B", keyA, 3)
	progress.Set(AckProgress{Group: "B", Key: keyA, CommittedSeq: 5, HasCommitted: true,
		SkippedSeq: map[uint64]bool{3: true}})

	decision, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"B", "A"})
	if err != nil {
		t.Fatalf("Redeliver: %v", err)
	}
	if len(decision.Actions) != 2 {
		t.Fatalf("actions = %d, want 2", len(decision.Actions))
	}

	aAction := wantAction(t, decision, "A", ResumeInPlace)
	if aAction.SeqInKey != 3 || aAction.MessageID != poison || !bytes.Equal(aAction.Payload, []byte(`{"bill":"fixed"}`)) {
		t.Fatalf("unexpected resume action: %+v", aAction)
	}

	bAction := wantAction(t, decision, "B", RouteToReview)
	if bAction.SeqInKey != 3 || bAction.CustomerID != custA || !bytes.Equal(bAction.Payload, []byte(`{"bill":"fixed"}`)) {
		t.Fatalf("unexpected review action: %+v", bAction)
	}

	rein := broker.Reinstated()
	if len(rein) != 1 || rein[0].group != "A" || rein[0].seq != 3 || !bytes.Equal(rein[0].payload, baseRepair().RepairedPayload) {
		t.Fatalf("unexpected reinstate calls: %+v", rein)
	}

	calls := review.Calls()
	if len(calls) != 1 || calls[0].group != "B" || calls[0].seq != 3 {
		t.Fatalf("unexpected review calls: %+v", calls)
	}

	// The decision payloads must be detached copies.
	decision.Actions[0].Payload[0] = 'X'
	again, ok := engine.StoredDecision(repair1)
	if !ok {
		t.Fatal("decision not stored")
	}
	stored, _ := again.ActionFor("A")
	if bytes.HasPrefix(stored.Payload, []byte("X")) {
		t.Fatal("stored decision payload was mutated through returned slice")
	}
}

// TestPositionBoundaries covers every boundary around the in-key sequence:
// no progress at all, exactly at seq-1, at the position itself, and beyond it.
func TestPositionBoundaries(t *testing.T) {
	cases := []struct {
		name     string
		prog     AckProgress
		markSkip bool
		want     ActionKind
	}{
		{
			name: "never consumed key resumes in place",
			prog: AckProgress{Group: "G0", Key: keyA},
			want: ResumeInPlace,
		},
		{
			name: "committed before seq resumes in place",
			prog: AckProgress{Group: "G1", Key: keyA, CommittedSeq: 2, HasCommitted: true},
			want: ResumeInPlace,
		},
		{
			name: "committed zero with seq zero is at position",
			prog: AckProgress{Group: "G2", Key: keyA, CommittedSeq: 0, HasCommitted: true},
			want: RouteToReview,
		},
		{
			name:     "explicit skip at position routes to review",
			prog:     AckProgress{Group: "G3", Key: keyA, CommittedSeq: 5, HasCommitted: true},
			markSkip: true,
			want:     RouteToReview,
		},
		{
			name: "committed past position implicitly routes to review",
			prog: AckProgress{Group: "G4", Key: keyA, CommittedSeq: 9, HasCommitted: true},
			want: RouteToReview,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			engine, progress, broker, review := newHarness(t)
			dead := baseDeadLetter()
			if tc.prog.Group == "G2" {
				dead.SeqInKey = 0 // boundary at the first position of the key
			}
			progress.Set(tc.prog)
			if tc.markSkip {
				progress.MarkSkip(tc.prog.Group, keyA, dead.SeqInKey)
			}

			repair := baseRepair()
			repair.RepairID = "repair-" + string(tc.prog.Group)
			decision, err := engine.Redeliver(context.Background(), dead, repair, []Group{tc.prog.Group})
			if err != nil {
				t.Fatalf("Redeliver: %v", err)
			}
			wantAction(t, decision, tc.prog.Group, tc.want)

			switch tc.want {
			case ResumeInPlace:
				if len(broker.Reinstated()) != 1 || len(review.Calls()) != 0 {
					t.Fatalf("resume case side effects wrong: rein=%d review=%d",
						len(broker.Reinstated()), len(review.Calls()))
				}
			case RouteToReview:
				if len(review.Calls()) != 1 || len(broker.Reinstated()) != 0 {
					t.Fatalf("review case side effects wrong: rein=%d review=%d",
						len(broker.Reinstated()), len(review.Calls()))
				}
			}
		})
	}
}

// TestRetryReusesDecision proves a retried interface call reuses the prior
// business decision, including when the retry names a different group list,
// and that side effects happen exactly once.
func TestRetryReusesDecision(t *testing.T) {
	engine, progress, broker, review := newHarness(t)
	progress.MarkSkip("B", keyA, 3)
	progress.Set(AckProgress{Group: "B", Key: keyA, CommittedSeq: 5, HasCommitted: true,
		SkippedSeq: map[uint64]bool{3: true}})
	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})

	first, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"A", "B"})
	if err != nil {
		t.Fatalf("first Redeliver: %v", err)
	}

	// Retry names only one group; the original decision must still come back.
	second, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"A"})
	if err != nil {
		t.Fatalf("retry Redeliver: %v", err)
	}
	if len(second.Actions) != len(first.Actions) {
		t.Fatalf("retry changed action count: %d vs %d", len(second.Actions), len(first.Actions))
	}
	for i := range first.Actions {
		if !reflect.DeepEqual(first.Actions[i], second.Actions[i]) {
			t.Fatalf("retry decision differs at %d:\nfirst=%+v\nretry=%+v", i, first.Actions[i], second.Actions[i])
		}
	}

	// Many retries must not produce any further side effect.
	for i := 0; i < 5; i++ {
		if _, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), nil); err != nil {
			t.Fatalf("retry %d: %v", i, err)
		}
	}
	if got := len(broker.Reinstated()); got != 1 {
		t.Fatalf("reinstate side effects = %d, want exactly 1", got)
	}
	if got := len(review.Calls()); got != 1 {
		t.Fatalf("review side effects = %d, want exactly 1", got)
	}
	if ids := engine.RecordedRepairIDs(); len(ids) != 1 || ids[0] != repair1 {
		t.Fatalf("recorded repair ids = %v", ids)
	}
}

// TestRetryWithDifferentRepairIsIndependent shows distinct repair IDs are
// independent business decisions even when they target the same message.
func TestRetryWithDifferentRepairIsIndependent(t *testing.T) {
	engine, progress, broker, review := newHarness(t)
	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})

	first := baseRepair()
	if _, err := engine.Redeliver(context.Background(), baseDeadLetter(), first, []Group{"A"}); err != nil {
		t.Fatalf("first: %v", err)
	}
	second := baseRepair()
	second.RepairID = "repair-biz-2"
	second.RepairedPayload = []byte(`{"bill":"fixed-again"}`)
	if _, err := engine.Redeliver(context.Background(), baseDeadLetter(), second, []Group{"A"}); err != nil {
		t.Fatalf("second: %v", err)
	}
	if got := len(broker.Reinstated()); got != 2 {
		t.Fatalf("reinstate calls = %d, want 2 for two repair IDs", got)
	}
	if ids := engine.RecordedRepairIDs(); len(ids) != 2 {
		t.Fatalf("recorded repair ids = %v, want 2", ids)
	}
	_ = review
}

// TestProgressRetained verifies the module never rewinds acknowledged
// progress: skipped group B keeps its committed sequence and skip marker.
func TestProgressRetained(t *testing.T) {
	engine, progress, broker, review := newHarness(t)
	progress.MarkSkip("B", keyA, 3)
	progress.Set(AckProgress{Group: "B", Key: keyA, CommittedSeq: 5, HasCommitted: true,
		SkippedSeq: map[uint64]bool{3: true}})

	if _, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"B"}); err != nil {
		t.Fatalf("Redeliver: %v", err)
	}
	after, err := progress.Progress(context.Background(), "B", keyA)
	if err != nil {
		t.Fatalf("Progress: %v", err)
	}
	if !after.HasCommitted || after.CommittedSeq != 5 {
		t.Fatalf("committed progress changed: %+v", after)
	}
	if !after.HasIrrevocableSkip(3) {
		t.Fatal("irrevocable skip marker was lost")
	}
	if len(broker.Reinstated()) != 0 {
		t.Fatal("skipped group must not be reinstated")
	}
	if len(review.Calls()) != 1 {
		t.Fatal("skipped group repair must be routed to review")
	}
}

// TestOtherCustomersKeepFlowing interleaves a repair with normal deliveries
// for another customer's messages; those messages keep using the regular
// delivery path and are neither reinstated nor reviewed.
func TestOtherCustomersKeepFlowing(t *testing.T) {
	engine, progress, broker, _ := newHarness(t)
	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})

	otherMessages := []struct {
		id   MessageID
		key  string
		seq  uint64
		cust string
	}{
		{"msg-b-10", keyB, 10, custB},
		{"msg-b-11", keyB, 11, custB},
		{"msg-a-4", keyA, 4, custA}, // another customer-a bill behind the poison is normal traffic too
	}

	// Interleave: other delivery, repair, other delivery, repair retry, other delivery.
	if err := broker.Deliver(context.Background(), otherMessages[0].id, otherMessages[0].key,
		otherMessages[0].seq, otherMessages[0].cust, []byte("b10")); err != nil {
		t.Fatal(err)
	}
	if _, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"A"}); err != nil {
		t.Fatalf("Redeliver: %v", err)
	}
	if err := broker.Deliver(context.Background(), otherMessages[1].id, otherMessages[1].key,
		otherMessages[1].seq, otherMessages[1].cust, []byte("b11")); err != nil {
		t.Fatal(err)
	}
	if _, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"A"}); err != nil {
		t.Fatalf("retry: %v", err)
	}
	if err := broker.Deliver(context.Background(), otherMessages[2].id, otherMessages[2].key,
		otherMessages[2].seq, otherMessages[2].cust, []byte("a4")); err != nil {
		t.Fatal(err)
	}

	delivered := broker.Delivered()
	if len(delivered) != 3 {
		t.Fatalf("normal deliveries = %d, want 3", len(delivered))
	}
	for i, want := range otherMessages {
		if delivered[i].id != want.id || delivered[i].customerID != want.cust || delivered[i].seq != want.seq {
			t.Fatalf("delivery %d = %+v, want %+v", i, delivered[i], want)
		}
	}
	if got := broker.Reinstated(); len(got) != 1 || got[0].group != "A" {
		t.Fatalf("repair must reinstate exactly once: %+v", got)
	}
}

// TestInterleavedDeadLetters repairs two different poisoned messages while
// normal deliveries happen in between, proving per-key sequence state stays
// independent and each repair keeps its own decision.
func TestInterleavedDeadLetters(t *testing.T) {
	engine, progress, broker, review := newHarness(t)

	firstDead := baseDeadLetter() // keyA seq 3
	secondDead := DeadLetter{MessageID: "msg-poison-7", Key: keyA, SeqInKey: 7, CustomerID: custA,
		OriginalPayload: []byte(`{"bill":"bad-7"}`)}

	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})
	progress.MarkSkip("B", keyA, 3)
	progress.Set(AckProgress{Group: "B", Key: keyA, CommittedSeq: 6, HasCommitted: true,
		SkippedSeq: map[uint64]bool{3: true}})

	repair1Req := baseRepair()
	if _, err := engine.Redeliver(context.Background(), firstDead, repair1Req, []Group{"A", "B"}); err != nil {
		t.Fatalf("first repair: %v", err)
	}
	if err := broker.Deliver(context.Background(), "msg-b-20", keyB, 20, custB, []byte("b20")); err != nil {
		t.Fatal(err)
	}

	// B advances further and now also skips seq 7; A remains before 7.
	progress.MarkSkip("B", keyA, 7)
	progress.Set(AckProgress{Group: "B", Key: keyA, CommittedSeq: 8, HasCommitted: true,
		SkippedSeq: map[uint64]bool{3: true, 7: true}})

	repair2Req := Repair{RepairID: "repair-biz-2", MessageID: secondDead.MessageID,
		CustomerID: custA, RepairedPayload: []byte(`{"bill":"fixed-7"}`)}
	decision2, err := engine.Redeliver(context.Background(), secondDead, repair2Req, []Group{"A", "B"})
	if err != nil {
		t.Fatalf("second repair: %v", err)
	}
	wantAction(t, decision2, "A", ResumeInPlace)
	wantAction(t, decision2, "B", RouteToReview)

	// Retry of the first repair still returns its original two-group decision.
	retry1, err := engine.Redeliver(context.Background(), firstDead, repair1Req, []Group{"A", "B"})
	if err != nil {
		t.Fatalf("retry first repair: %v", err)
	}
	if len(retry1.Actions) != 2 {
		t.Fatalf("first repair decision changed on retry: %+v", retry1.Actions)
	}

	if got := len(broker.Reinstated()); got != 2 {
		t.Fatalf("reinstate calls = %d, want one per repair for group A", got)
	}
	if got := len(review.Calls()); got != 2 {
		t.Fatalf("review calls = %d, want one per repair for group B", got)
	}
	if delivered := broker.Delivered(); len(delivered) != 1 || delivered[0].id != "msg-b-20" {
		t.Fatalf("interleaved normal delivery lost: %+v", broker.Delivered())
	}

	// B's acknowledged progress through seq 8 is retained.
	bprog, _ := progress.Progress(context.Background(), "B", keyA)
	if bprog.CommittedSeq != 8 || !bprog.HasIrrevocableSkip(3) || !bprog.HasIrrevocableSkip(7) {
		t.Fatalf("group B progress not retained: %+v", bprog)
	}
}

// TestConcurrentSameRepair fires many identical retries concurrently and
// requires a single stored decision with side effects performed at most the
// number of groups' worth of times (exactly once here).
func TestConcurrentSameRepair(t *testing.T) {
	engine, progress, broker, review := newHarness(t)
	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})

	const n = 32
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"A"})
			errs <- err
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent Redeliver: %v", err)
		}
	}
	if got := len(broker.Reinstated()); got != 1 {
		t.Fatalf("reinstate calls = %d under concurrency, want exactly 1", got)
	}
	if got := len(review.Calls()); got != 0 {
		t.Fatalf("review calls = %d, want 0", got)
	}
	if ids := engine.RecordedRepairIDs(); len(ids) != 1 {
		t.Fatalf("stored decisions = %v, want one", ids)
	}
}

// TestConcurrentFailureReleasesReservation makes the in-flight call fail while
// waiters are blocked on it: no decision is stored, the failed side effect is
// not recorded, and a later retry performs the repair exactly once.
func TestConcurrentFailureReleasesReservation(t *testing.T) {
	engine, progress, broker, review := newHarness(t)
	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})
	boom := errors.New("broker unavailable")
	broker.FailNextReinstate(boom)

	const n = 8
	var wg sync.WaitGroup
	errs := make(chan error, n)
	start := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			_, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"A"})
			errs <- err
		}()
	}
	close(start)
	wg.Wait()
	close(errs)

	failures, successes := 0, 0
	for err := range errs {
		switch {
		case errors.Is(err, boom):
			failures++
		case err == nil:
			successes++
		default:
			t.Fatalf("unexpected err %v", err)
		}
	}
	if failures == 0 {
		t.Fatal("at least one caller must observe the original side effect failure")
	}
	if successes+failures != n {
		t.Fatalf("failures=%d successes=%d, total must be %d", failures, successes, n)
	}

	// Converge the module: if a waiter already took over successfully there
	// is a stored decision; otherwise an explicit retry must do it. Either way
	// the repair ends with exactly one reinstatement side effect.
	if _, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"A"}); err != nil {
		t.Fatalf("final retry: %v", err)
	}
	decision, ok := engine.StoredDecision(repair1)
	if !ok || len(decision.Actions) != 1 {
		t.Fatalf("repair must converge to one stored decision: ok=%v decision=%+v", ok, decision)
	}
	if got := len(broker.Reinstated()); got != 1 {
		t.Fatalf("reinstate calls = %d, want exactly 1 across failure and retries", got)
	}
	if len(review.Calls()) != 0 {
		t.Fatal("resume path must not route to review")
	}
}

// TestValidationFailures exercises the defensive checks that must fail before
// any side effect runs.
func TestValidationFailures(t *testing.T) {
	engine, _, broker, review := newHarness(t)
	baseDead := baseDeadLetter()
	baseReq := baseRepair()

	cases := []struct {
		name    string
		dead    DeadLetter
		req     Repair
		wantErr error
	}{
		{"empty dead letter id", DeadLetter{MessageID: ""}, baseReq, ErrEmptyDeadLetter},
		{"empty repair id", baseDead, Repair{MessageID: poison, CustomerID: custA}, ErrEmptyRepairID},
		{"message mismatch", baseDead, Repair{RepairID: "x", MessageID: "other", CustomerID: custA}, ErrMessageMismatch},
		{"customer mismatch", baseDead, Repair{RepairID: "x", MessageID: poison, CustomerID: custB}, ErrCustomerMismatch},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := engine.Redeliver(context.Background(), tc.dead, tc.req, []Group{"A"})
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("err = %v, want %v", err, tc.wantErr)
			}
			if len(broker.Reinstated()) != 0 || len(broker.Delivered()) != 0 || len(review.Calls()) != 0 {
				t.Fatal("validation failure must not perform side effects")
			}
		})
	}

	if _, err := NewEngine(nil, broker, review); !errors.Is(err, ErrNilProgress) {
		t.Fatalf("nil progress err = %v", err)
	}
	if _, err := NewEngine(NewMemoryProgress(), nil, review); !errors.Is(err, ErrNilBroker) {
		t.Fatalf("nil broker err = %v", err)
	}
	if _, err := NewEngine(NewMemoryProgress(), broker, nil); !errors.Is(err, ErrNilReview) {
		t.Fatalf("nil review err = %v", err)
	}
}

// TestSideEffectFailureNotRecorded requires that when a side effect fails, no
// decision is stored and a retry can still perform the full repair.
func TestSideEffectFailureNotRecorded(t *testing.T) {
	t.Run("broker failure", func(t *testing.T) {
		engine, progress, broker, review := newHarness(t)
		progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})
		boom := errors.New("broker unavailable")
		broker.FailNextReinstate(boom)

		_, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"A"})
		if !errors.Is(err, boom) {
			t.Fatalf("err = %v, want %v", err, boom)
		}
		if _, ok := engine.StoredDecision(repair1); ok {
			t.Fatal("failed repair must not be recorded as a decision")
		}
		if _, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"A"}); err != nil {
			t.Fatalf("retry after failure: %v", err)
		}
		if got := len(broker.Reinstated()); got != 1 {
			t.Fatalf("reinstate after retry = %d, want 1", got)
		}
		if len(review.Calls()) != 0 {
			t.Fatal("resume path must not route to review")
		}
	})

	t.Run("review failure", func(t *testing.T) {
		engine, progress, broker, review := newHarness(t)
		progress.MarkSkip("B", keyA, 3)
		progress.Set(AckProgress{Group: "B", Key: keyA, CommittedSeq: 5, HasCommitted: true,
			SkippedSeq: map[uint64]bool{3: true}})
		boom := errors.New("review source down")
		review.FailNextSend(boom)

		_, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"B"})
		if !errors.Is(err, boom) {
			t.Fatalf("err = %v, want %v", err, boom)
		}
		if _, ok := engine.StoredDecision(repair1); ok {
			t.Fatal("failed review routing must not be recorded")
		}
		if _, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"B"}); err != nil {
			t.Fatalf("retry after failure: %v", err)
		}
		if got := len(review.Calls()); got != 1 {
			t.Fatalf("review calls after retry = %d, want 1", got)
		}
		if len(broker.Reinstated()) != 0 {
			t.Fatal("skipped group must never be reinstated")
		}
	})
}

// TestEmptyGroupList records an empty decision without side effects and still
// benefits from idempotent retries.
func TestEmptyGroupList(t *testing.T) {
	engine, _, broker, review := newHarness(t)
	decision, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), nil)
	if err != nil {
		t.Fatalf("Redeliver: %v", err)
	}
	if len(decision.Actions) != 0 {
		t.Fatalf("actions = %d, want 0", len(decision.Actions))
	}
	if len(broker.Reinstated()) != 0 || len(review.Calls()) != 0 {
		t.Fatal("no groups means no side effects")
	}
	retry, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), []Group{"A"})
	if err != nil {
		t.Fatalf("retry: %v", err)
	}
	if len(retry.Actions) != 0 {
		t.Fatal("retry must reuse the recorded empty decision")
	}
}

// TestContextCanceled ensures a canceled context aborts before side effects.
func TestContextCanceled(t *testing.T) {
	engine, progress, broker, review := newHarness(t)
	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := engine.Redeliver(ctx, baseDeadLetter(), baseRepair(), []Group{"A"}); !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if len(broker.Reinstated()) != 0 || len(review.Calls()) != 0 {
		t.Fatal("canceled context must not perform side effects")
	}
	if _, ok := engine.StoredDecision(repair1); ok {
		t.Fatal("canceled repair must not be recorded")
	}
}

// TestDuplicateGroupsDeduped makes a repeated group in one call produce a
// single action and a single side effect.
func TestDuplicateGroupsDeduped(t *testing.T) {
	engine, progress, broker, _ := newHarness(t)
	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})

	decision, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(),
		[]Group{"A", "A", "A"})
	if err != nil {
		t.Fatalf("Redeliver: %v", err)
	}
	if len(decision.Actions) != 1 {
		t.Fatalf("actions = %d, want 1 after dedupe", len(decision.Actions))
	}
	if got := len(broker.Reinstated()); got != 1 {
		t.Fatalf("reinstate calls = %d, want 1", got)
	}
}

func TestActionKindString(t *testing.T) {
	if ResumeInPlace.String() != "resume_in_place" || RouteToReview.String() != "route_to_review" {
		t.Fatal("unexpected action kind strings")
	}
	if ActionKind(0).String() != "unknown" {
		t.Fatal("zero action kind must render unknown")
	}
}

func TestProgressPositionHelpers(t *testing.T) {
	if !(AckProgress{}).BeforePosition(0) {
		t.Fatal("group without committed progress is before every position")
	}
	if !(AckProgress{HasCommitted: true, CommittedSeq: 2}).BeforePosition(3) {
		t.Fatal("committed seq 2 must be before position 3")
	}
	if (AckProgress{HasCommitted: true, CommittedSeq: 3}).BeforePosition(3) {
		t.Fatal("committed seq 3 must not be before position 3")
	}
	p := AckProgress{SkippedSeq: map[uint64]bool{3: true}}
	if !p.HasIrrevocableSkip(3) || p.HasIrrevocableSkip(4) {
		t.Fatal("skip marker lookup wrong")
	}
}

func TestStoredDecisionMissing(t *testing.T) {
	engine, _, _, _ := newHarness(t)
	if d, ok := engine.StoredDecision("nope"); ok {
		t.Fatalf("unexpected stored decision: %+v", d)
	}
}

func TestDecisionActionFor(t *testing.T) {
	d := Decision{Actions: []GroupAction{{Group: "A", Action: ResumeInPlace}}}
	if a, ok := d.ActionFor("A"); !ok || a.Action != ResumeInPlace {
		t.Fatal("existing group lookup failed")
	}
	if _, ok := d.ActionFor("Z"); ok {
		t.Fatal("missing group should report false")
	}
}

// TestMultipleGroupsMixedOrdering checks deterministic action ordering and
// correct payload routing across several groups in one request.
func TestMultipleGroupsMixedOrdering(t *testing.T) {
	engine, progress, broker, review := newHarness(t)
	groups := []Group{"D", "A", "C", "B"}
	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true}) // before
	progress.Set(AckProgress{Group: "D", Key: keyA})                                      // never consumed
	progress.MarkSkip("B", keyA, 3)
	progress.Set(AckProgress{Group: "B", Key: keyA, CommittedSeq: 4, HasCommitted: true,
		SkippedSeq: map[uint64]bool{3: true}})
	progress.Set(AckProgress{Group: "C", Key: keyA, CommittedSeq: 9, HasCommitted: true}) // past, implicit skip

	decision, err := engine.Redeliver(context.Background(), baseDeadLetter(), baseRepair(), groups)
	if err != nil {
		t.Fatalf("Redeliver: %v", err)
	}
	wantOrder := []Group{"A", "B", "C", "D"}
	if len(decision.Actions) != len(wantOrder) {
		t.Fatalf("action count = %d", len(decision.Actions))
	}
	for i, g := range wantOrder {
		if decision.Actions[i].Group != g {
			t.Fatalf("action %d group = %q, want %q (full: %+v)", i, decision.Actions[i].Group, g, decision.Actions)
		}
	}
	wantAction(t, decision, "A", ResumeInPlace)
	wantAction(t, decision, "B", RouteToReview)
	wantAction(t, decision, "C", RouteToReview)
	wantAction(t, decision, "D", ResumeInPlace)

	if got := len(broker.Reinstated()); got != 2 {
		t.Fatalf("reinstate calls = %d, want 2", got)
	}
	if got := len(review.Calls()); got != 2 {
		t.Fatalf("review calls = %d, want 2", got)
	}
}

// TestPayloadsAreDefensiveCopies ensures callers cannot corrupt engine state
// by mutating the payload slices they hand in or receive.
func TestPayloadsAreDefensiveCopies(t *testing.T) {
	engine, progress, _, _ := newHarness(t)
	progress.Set(AckProgress{Group: "A", Key: keyA, CommittedSeq: 2, HasCommitted: true})

	dead := baseDeadLetter()
	req := baseRepair()
	decision, err := engine.Redeliver(context.Background(), dead, req, []Group{"A"})
	if err != nil {
		t.Fatal(err)
	}
	req.RepairedPayload[0] = 'Z'
	dead.OriginalPayload[0] = 'Z'

	stored, ok := engine.StoredDecision(repair1)
	if !ok {
		t.Fatal("decision missing")
	}
	if bytes.Equal(stored.DeadLetter.OriginalPayload, dead.OriginalPayload) {
		t.Fatal("dead letter payload should be an independent copy")
	}
	a, _ := stored.ActionFor("A")
	if bytes.HasPrefix(a.Payload, []byte("Z")) {
		t.Fatal("repaired payload should be an independent copy")
	}
	if bytes.HasPrefix(decision.Actions[0].Payload, []byte("Z")) {
		t.Fatal("returned decision payload should be an independent copy")
	}
}

func ExampleEngine() {
	progress := NewMemoryProgress()
	broker := &FakeBroker{}
	review := &FakeReview{}
	engine, _ := NewEngine(progress, broker, review)

	progress.Set(AckProgress{Group: "group-a", Key: "k1", CommittedSeq: 2, HasCommitted: true})
	dead := DeadLetter{MessageID: "msg-3", Key: "k1", SeqInKey: 3, CustomerID: "cust-1",
		OriginalPayload: []byte("broken")}
	repair := Repair{RepairID: "repair-1", MessageID: "msg-3", CustomerID: "cust-1",
		RepairedPayload: []byte("fixed")}

	decision, _ := engine.Redeliver(context.Background(), dead, repair, []Group{"group-a"})
	action, _ := decision.ActionFor("group-a")
	fmt.Println(action.Action)
	// Output: resume_in_place
}
