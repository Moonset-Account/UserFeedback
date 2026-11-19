package seatledger

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func testKeys() KeyRegistry {
	return KeyRegistry{
		"A": []byte("branch-key-A"),
		"B": []byte("branch-key-B"),
		"C": []byte("branch-key-C"),
	}
}

func newLedger(t *testing.T, name string, seats int) *Ledger {
	t.Helper()
	l, err := Open(name, []byte("branch-key-"+name), nil)
	if err != nil {
		t.Fatalf("Open(%s): %v", name, err)
	}
	if seats > 0 {
		if err := l.Allocate(seats); err != nil {
			t.Fatalf("Allocate(%s, %d): %v", name, seats, err)
		}
	}
	return l
}

func mustAuditOK(t *testing.T, wantIssued int, ledgers ...*Ledger) AuditReport {
	t.Helper()
	rep := Audit(ledgers...)
	if !rep.OK {
		t.Fatalf("audit not balanced: issued=%d explained=%d report=%+v", rep.Issued, rep.Explained, rep)
	}
	if rep.Issued != wantIssued {
		t.Fatalf("audit issued = %d, want %d", rep.Issued, wantIssued)
	}
	return rep
}

// Acceptable result: offline checkout consumes only the locally available
// share, including the exact-availability boundary.
func TestCheckoutRespectsLocalShare(t *testing.T) {
	a := newLedger(t, "A", 10)

	if err := a.Checkout(4); err != nil {
		t.Fatalf("checkout within share rejected: %v", err)
	}
	if got := a.Snapshot(); got.Available != 6 || got.InUse != 4 {
		t.Fatalf("after checkout: %+v, want available=6 inUse=4", got)
	}

	// Boundary: checking out exactly the remaining share succeeds.
	if err := a.Checkout(6); err != nil {
		t.Fatalf("checkout of exact remaining share rejected: %v", err)
	}
	if got := a.Snapshot(); got.Available != 0 || got.InUse != 10 {
		t.Fatalf("after boundary checkout: %+v, want available=0 inUse=10", got)
	}

	// Rejected: one seat beyond the local share.
	if err := a.Checkout(1); !errors.Is(err, ErrInsufficientSeats) {
		t.Fatalf("checkout beyond share: err=%v, want ErrInsufficientSeats", err)
	}

	if err := a.Checkin(3); err != nil {
		t.Fatalf("checkin: %v", err)
	}
	if got := a.Snapshot(); got.Available != 3 || got.InUse != 7 {
		t.Fatalf("after checkin: %+v, want available=3 inUse=7", got)
	}
	mustAuditOK(t, 10, a)
}

// Acceptable result: a full transfer lifecycle moves seats from source to
// receiver while the global total stays constant at every step.
func TestTransferLifecycle(t *testing.T) {
	keys := testKeys()
	a := newLedger(t, "A", 10)
	b := newLedger(t, "B", 5)

	rec, err := a.InitiateTransfer("B", 3)
	if err != nil {
		t.Fatalf("initiate: %v", err)
	}
	if got := a.Snapshot(); got.Available != 7 || got.FrozenOut[rec.ID] != 3 {
		t.Fatalf("after freeze: %+v, want available=7 frozen{%s:3}", got, rec.ID)
	}
	// In flight, still explained at the source.
	mustAuditOK(t, 15, a, b)

	ack, err := b.ReceiveTransfer(rec, keys)
	if err != nil {
		t.Fatalf("receive: %v", err)
	}
	if got := b.Snapshot(); got.Available != 8 {
		t.Fatalf("after credit: %+v, want available=8", got)
	}
	// Accepted but not yet confirmed: explained at the receiver only.
	mustAuditOK(t, 15, a, b)

	if err := a.ConfirmTransfer(ack, keys); err != nil {
		t.Fatalf("confirm: %v", err)
	}
	if got := a.Snapshot(); got.Available != 7 || len(got.FrozenOut) != 0 {
		t.Fatalf("after confirm: %+v, want available=7 no frozen", got)
	}
	mustAuditOK(t, 15, a, b)
}

// Boundary + interleaving: a transfer may freeze exactly the available
// share, never more; while seats are frozen they cannot be checked out.
func TestTransferBoundaryAndFrozenNotCheckoutable(t *testing.T) {
	a := newLedger(t, "A", 6)
	b := newLedger(t, "B", 0)

	if _, err := a.InitiateTransfer("B", 7); !errors.Is(err, ErrInsufficientSeats) {
		t.Fatalf("transfer beyond share: err=%v, want ErrInsufficientSeats", err)
	}
	if _, err := a.InitiateTransfer("B", 0); err == nil {
		t.Fatal("zero-seat transfer accepted, want rejection")
	}

	rec, err := a.InitiateTransfer("B", 6) // exact share boundary
	if err != nil {
		t.Fatalf("freeze exact share: %v", err)
	}
	if err := a.Checkout(1); !errors.Is(err, ErrInsufficientSeats) {
		t.Fatalf("checkout of frozen seats: err=%v, want ErrInsufficientSeats", err)
	}

	keys := testKeys()
	ack, err := b.ReceiveTransfer(rec, keys)
	if err != nil {
		t.Fatalf("receive: %v", err)
	}
	if err := a.ConfirmTransfer(ack, keys); err != nil {
		t.Fatalf("confirm: %v", err)
	}
	mustAuditOK(t, 6, a, b)
	if got := b.Snapshot(); got.Available != 6 {
		t.Fatalf("receiver: %+v, want available=6", got)
	}
}

// Duplicate and out-of-order delivery: records and acks may be repeated or
// reordered; every effect applies exactly once.
func TestDuplicateAndOutOfOrderExchange(t *testing.T) {
	keys := testKeys()
	a := newLedger(t, "A", 10)
	b := newLedger(t, "B", 5)

	r1, err := a.InitiateTransfer("B", 1)
	if err != nil {
		t.Fatalf("initiate r1: %v", err)
	}
	r2, err := a.InitiateTransfer("B", 2)
	if err != nil {
		t.Fatalf("initiate r2: %v", err)
	}

	// Out of order: second record arrives first.
	ack2, err := b.ReceiveTransfer(r2, keys)
	if err != nil {
		t.Fatalf("receive r2: %v", err)
	}
	ack1, err := b.ReceiveTransfer(r1, keys)
	if err != nil {
		t.Fatalf("receive r1: %v", err)
	}
	// Duplicate delivery of r1: credited once, same ack re-issued.
	ack1dup, err := b.ReceiveTransfer(r1, keys)
	if err != nil {
		t.Fatalf("duplicate receive r1: %v", err)
	}
	if ack1dup != ack1 {
		t.Fatalf("duplicate ack differs: %+v vs %+v", ack1dup, ack1)
	}
	if got := b.Snapshot(); got.Available != 8 {
		t.Fatalf("receiver credited more than once: %+v, want available=8", got)
	}

	// Acks confirmed out of order and twice.
	if err := a.ConfirmTransfer(ack2, keys); err != nil {
		t.Fatalf("confirm r2: %v", err)
	}
	if err := a.ConfirmTransfer(ack1, keys); err != nil {
		t.Fatalf("confirm r1: %v", err)
	}
	if err := a.ConfirmTransfer(ack1, keys); err != nil {
		t.Fatalf("duplicate confirm r1: %v", err)
	}

	// Confirmation lost: receiver resends the ack, source confirms again.
	ack1resend, ok := b.AckFor(r1.ID)
	if !ok {
		t.Fatalf("AckFor(%s) not found", r1.ID)
	}
	if err := a.ConfirmTransfer(ack1resend, keys); err != nil {
		t.Fatalf("confirm resent ack: %v", err)
	}

	if got := a.Snapshot(); got.Available != 7 || len(got.FrozenOut) != 0 {
		t.Fatalf("source: %+v, want available=7 no frozen", got)
	}
	mustAuditOK(t, 15, a, b)
}

// Rejected results: tampered, forged or misaddressed records and acks must
// be refused without changing any state.
func TestForgedAndTamperedRecordsRejected(t *testing.T) {
	keys := testKeys()
	a := newLedger(t, "A", 10)
	b := newLedger(t, "B", 5)
	c := newLedger(t, "C", 0)

	rec, err := a.InitiateTransfer("B", 3)
	if err != nil {
		t.Fatalf("initiate: %v", err)
	}

	// Tampered count.
	bad := rec
	bad.Count = 4
	if _, err := b.ReceiveTransfer(bad, keys); !errors.Is(err, ErrInvalidSignature) {
		t.Fatalf("tampered count: err=%v, want ErrInvalidSignature", err)
	}
	// Forged signature.
	bad = rec
	bad.Sig = "00"
	if _, err := b.ReceiveTransfer(bad, keys); !errors.Is(err, ErrInvalidSignature) {
		t.Fatalf("forged sig: err=%v, want ErrInvalidSignature", err)
	}
	// Record signed for C delivered to B.
	recC, err := a.InitiateTransfer("C", 1)
	if err != nil {
		t.Fatalf("initiate for C: %v", err)
	}
	if _, err := b.ReceiveTransfer(recC, keys); err == nil {
		t.Fatal("misaddressed record accepted by B")
	}
	if got := b.Snapshot(); got.Available != 5 {
		t.Fatalf("receiver state changed by rejected records: %+v", got)
	}

	// Forged ack must not resolve the frozen transfer.
	fakeAck := Ack{TransferID: rec.ID, From: "A", To: "B", Count: 3, Sig: "00"}
	if err := a.ConfirmTransfer(fakeAck, keys); !errors.Is(err, ErrInvalidSignature) {
		t.Fatalf("forged ack: err=%v, want ErrInvalidSignature", err)
	}
	if got := a.Snapshot(); got.FrozenOut[rec.ID] != 3 {
		t.Fatalf("forged ack unfroze seats: %+v", got)
	}
	// Ack from an unknown branch.
	if _, err := c.ReceiveTransfer(recC, KeyRegistry{}); err == nil {
		t.Fatal("record verified without issuer key")
	}

	mustAuditOK(t, 15, a, b, c)
}

// Timeout safety: a source that stops waiting must not reclaim frozen seats
// unilaterally; the late-arriving record still credits the receiver exactly
// once and the ends never own the same seats.
func TestTimeoutCannotDoubleOwnSeats(t *testing.T) {
	keys := testKeys()
	a := newLedger(t, "A", 6)
	b := newLedger(t, "B", 2)

	rec, err := a.InitiateTransfer("B", 4)
	if err != nil {
		t.Fatalf("initiate: %v", err)
	}

	// A times out waiting for the ack and tries to reclaim the seats.
	if err := a.AbortTransfer(rec.ID); err == nil {
		t.Fatal("unilateral abort of frozen transfer allowed")
	}
	// The frozen seats are not spendable locally either.
	if err := a.Checkout(3); !errors.Is(err, ErrInsufficientSeats) {
		t.Fatalf("checkout into frozen share: err=%v, want ErrInsufficientSeats", err)
	}
	if err := a.Checkout(2); err != nil { // boundary: remaining share usable
		t.Fatalf("checkout of remaining share: %v", err)
	}

	// The record arrives late at B and is credited.
	ack, err := b.ReceiveTransfer(rec, keys)
	if err != nil {
		t.Fatalf("late receive: %v", err)
	}
	// Mid-flight audit: source's frozen seats are explained at the receiver.
	rep := mustAuditOK(t, 8, a, b)
	for _, be := range rep.Branches {
		if be.Branch == "A" && be.CreditedElsewhere != 4 {
			t.Fatalf("branch A explanation: %+v, want CreditedElsewhere=4", be)
		}
	}

	if err := a.ConfirmTransfer(ack, keys); err != nil {
		t.Fatalf("confirm: %v", err)
	}
	snapA, snapB := a.Snapshot(), b.Snapshot()
	if snapA.Available != 0 || snapA.InUse != 2 || len(snapA.FrozenOut) != 0 {
		t.Fatalf("source end state: %+v", snapA)
	}
	if snapB.Available != 6 {
		t.Fatalf("receiver end state: %+v, want available=6", snapB)
	}
	mustAuditOK(t, 8, a, b)
}

// Rejection path: the receiver refuses, the source unfreezes via the signed
// rejection, and the record can never be accepted afterwards.
func TestRejectionReturnsSeatsExactlyOnce(t *testing.T) {
	keys := testKeys()
	a := newLedger(t, "A", 10)
	b := newLedger(t, "B", 5)

	rec, err := a.InitiateTransfer("B", 3)
	if err != nil {
		t.Fatalf("initiate: %v", err)
	}
	rej, err := b.RejectTransfer(rec, keys, "no capacity")
	if err != nil {
		t.Fatalf("reject: %v", err)
	}
	if got := b.Snapshot(); got.Available != 5 {
		t.Fatalf("rejection changed receiver share: %+v", got)
	}
	// In flight until the source applies the rejection.
	mustAuditOK(t, 15, a, b)

	if err := a.ApplyRejection(rej, keys); err != nil {
		t.Fatalf("apply rejection: %v", err)
	}
	if err := a.ApplyRejection(rej, keys); err != nil {
		t.Fatalf("duplicate apply rejection: %v", err)
	}
	if got := a.Snapshot(); got.Available != 10 || len(got.FrozenOut) != 0 {
		t.Fatalf("after unfreeze: %+v, want available=10 no frozen", got)
	}

	// A rejected record can never be accepted later.
	if _, err := b.ReceiveTransfer(rec, keys); err == nil {
		t.Fatal("accepted a previously rejected transfer")
	}
	// Resent rejection stays idempotent.
	if _, ok := b.RejectionFor(rec.ID, "no capacity"); !ok {
		t.Fatalf("RejectionFor(%s) not found", rec.ID)
	}
	mustAuditOK(t, 15, a, b)
}

// Recovery: both ends rebuild from their persisted event prefixes, then
// resume exchanging (duplicate) records without double effects.
func TestRecoveryFromPersistedPrefix(t *testing.T) {
	dir := t.TempDir()
	keys := testKeys()

	open := func(name string) (*Ledger, *Store) {
		st, err := OpenStore(filepath.Join(dir, name+".log"))
		if err != nil {
			t.Fatalf("OpenStore(%s): %v", name, err)
		}
		l, err := Open(name, []byte("branch-key-"+name), st)
		if err != nil {
			t.Fatalf("Open(%s): %v", name, err)
		}
		return l, st
	}

	a, stA := open("A")
	b, stB := open("B")
	if err := a.Allocate(9); err != nil {
		t.Fatalf("allocate A: %v", err)
	}
	if err := b.Allocate(4); err != nil {
		t.Fatalf("allocate B: %v", err)
	}
	rec, err := a.InitiateTransfer("B", 3)
	if err != nil {
		t.Fatalf("initiate: %v", err)
	}
	if _, err := b.ReceiveTransfer(rec, keys); err != nil {
		t.Fatalf("receive: %v", err)
	}
	// Crash: the ack never reaches A.
	stA.Close()
	stB.Close()

	// Both ends recover from their persisted prefixes.
	a2, stA2 := open("A")
	defer stA2.Close()
	b2, stB2 := open("B")
	defer stB2.Close()

	if got := a2.Snapshot(); got.Available != 6 || got.FrozenOut[rec.ID] != 3 {
		t.Fatalf("recovered source: %+v, want available=6 frozen{%s:3}", got, rec.ID)
	}
	if got := b2.Snapshot(); got.Available != 7 {
		t.Fatalf("recovered receiver: %+v, want available=7", got)
	}

	// Duplicate record redelivered after recovery: no double credit.
	if _, err := b2.ReceiveTransfer(rec, keys); err != nil {
		t.Fatalf("duplicate receive after recovery: %v", err)
	}
	if got := b2.Snapshot(); got.Available != 7 {
		t.Fatalf("double credit after recovery: %+v", got)
	}

	// The lost confirmation is resent and the transfer completes.
	ack, ok := b2.AckFor(rec.ID)
	if !ok {
		t.Fatalf("AckFor(%s) after recovery not found", rec.ID)
	}
	if err := a2.ConfirmTransfer(ack, keys); err != nil {
		t.Fatalf("confirm after recovery: %v", err)
	}
	mustAuditOK(t, 13, a2, b2)
}

// Torn tail: recovery keeps only the complete persisted prefix.
func TestRecoveryIgnoresTornTail(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "a.log")

	st, err := OpenStore(path)
	if err != nil {
		t.Fatalf("OpenStore: %v", err)
	}
	a, err := Open("A", []byte("branch-key-A"), st)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	if err := a.Allocate(5); err != nil {
		t.Fatalf("allocate: %v", err)
	}
	if err := a.Checkout(2); err != nil {
		t.Fatalf("checkout: %v", err)
	}
	st.Close()

	// Simulate a crash mid-append: a partial JSON line at the tail.
	f, err := os.OpenFile(path, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatalf("open for torn write: %v", err)
	}
	if _, err := f.WriteString(`{"type":"checki`); err != nil {
		t.Fatalf("torn write: %v", err)
	}
	f.Close()

	st2, err := OpenStore(path)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	defer st2.Close()
	a2, err := Open("A", []byte("branch-key-A"), st2)
	if err != nil {
		t.Fatalf("recover: %v", err)
	}
	if got := a2.Snapshot(); got.Available != 3 || got.InUse != 2 {
		t.Fatalf("recovered from prefix: %+v, want available=3 inUse=2", got)
	}
	// The ledger keeps working on top of the recovered prefix.
	if err := a2.Checkin(2); err != nil {
		t.Fatalf("checkin after recovery: %v", err)
	}
	mustAuditOK(t, 5, a2)
}

// Global audit: with seats simultaneously in use, available, in flight and
// accepted-but-unconfirmed across three branches, every seat is explained
// exactly once and the total equals the initial issuance.
func TestGlobalAuditExplainsEverySeat(t *testing.T) {
	keys := testKeys()
	a := newLedger(t, "A", 10)
	b := newLedger(t, "B", 8)
	c := newLedger(t, "C", 6)

	if err := a.Checkout(3); err != nil {
		t.Fatalf("A checkout: %v", err)
	}
	if err := c.Checkout(1); err != nil {
		t.Fatalf("C checkout: %v", err)
	}

	// A -> B: accepted, not yet confirmed (credited elsewhere).
	recAB, err := a.InitiateTransfer("B", 2)
	if err != nil {
		t.Fatalf("initiate A->B: %v", err)
	}
	ackAB, err := b.ReceiveTransfer(recAB, keys)
	if err != nil {
		t.Fatalf("B receive: %v", err)
	}
	// B -> C: initiated, not yet delivered (in flight at B).
	recBC, err := b.InitiateTransfer("C", 3)
	if err != nil {
		t.Fatalf("initiate B->C: %v", err)
	}

	rep := mustAuditOK(t, 24, a, b, c)
	want := map[string]BranchExplanation{
		"A": {Branch: "A", Issued: 10, Available: 5, InUse: 3, CreditedElsewhere: 2},
		"B": {Branch: "B", Issued: 8, Available: 7, InFlightOut: 3},
		"C": {Branch: "C", Issued: 6, Available: 5, InUse: 1},
	}
	for _, be := range rep.Branches {
		if be != want[be.Branch] {
			t.Fatalf("branch %s explanation = %+v, want %+v", be.Branch, be, want[be.Branch])
		}
	}

	// Complete both transfers; the invariant still holds.
	if err := a.ConfirmTransfer(ackAB, keys); err != nil {
		t.Fatalf("confirm A->B: %v", err)
	}
	ackBC, err := c.ReceiveTransfer(recBC, keys)
	if err != nil {
		t.Fatalf("C receive: %v", err)
	}
	if err := b.ConfirmTransfer(ackBC, keys); err != nil {
		t.Fatalf("confirm B->C: %v", err)
	}
	rep = mustAuditOK(t, 24, a, b, c)
	var avail, inUse int
	for _, be := range rep.Branches {
		avail += be.Available
		inUse += be.InUse
		if be.InFlightOut != 0 || be.CreditedElsewhere != 0 {
			t.Fatalf("branch %s still in flight after completion: %+v", be.Branch, be)
		}
	}
	if avail != 20 || inUse != 4 {
		t.Fatalf("final totals: available=%d inUse=%d, want 20/4", avail, inUse)
	}
}
