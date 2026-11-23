package delivery

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
)

// ---------------------------------------------------------------------------
// helpers

func mustEngine(t *testing.T, opts Options) *Engine {
	t.Helper()
	e, err := NewEngine(opts)
	if err != nil {
		t.Fatalf("NewEngine(%+v): %v", opts, err)
	}
	return e
}

func mustEnqueue(t *testing.T, e *Engine, s MessageSpec, now int64) {
	t.Helper()
	if err := e.Enqueue(s, now); err != nil {
		t.Fatalf("Enqueue(%+v, %d): %v", s, now, err)
	}
}

func mustClaim(t *testing.T, e *Engine, queue, req string, now int64) Receipt {
	t.Helper()
	r, err := e.Claim(queue, req, now)
	if err != nil {
		t.Fatalf("Claim(%q, %q, %d): %v", queue, req, now, err)
	}
	return r
}

func mustAck(t *testing.T, e *Engine, receiptID, req string, now int64) AckResult {
	t.Helper()
	res, err := e.Ack(receiptID, req, now)
	if err != nil {
		t.Fatalf("Ack(%q, %q, %d): %v", receiptID, req, now, err)
	}
	return res
}

func spec(id, queue string, visible, expires int64, max int) MessageSpec {
	return MessageSpec{ID: id, Queue: queue, VisibleAt: visible, ExpiresAt: expires, MaxAttempts: max}
}

func mustMessage(t *testing.T, e *Engine, id string, now int64) MessageView {
	t.Helper()
	v, ok := e.Message(id, now)
	if !ok {
		t.Fatalf("Message(%q) not found", id)
	}
	return v
}

func mustReceipt(t *testing.T, e *Engine, id string, now int64) ReceiptView {
	t.Helper()
	v, ok := e.Receipt(id, now)
	if !ok {
		t.Fatalf("Receipt(%q) not found", id)
	}
	return v
}

func countKind(recs []Record, kind string) int {
	n := 0
	for _, r := range recs {
		if r.Kind == kind {
			n++
		}
	}
	return n
}

// ---------------------------------------------------------------------------
// validation and ordering

func TestNewEngineRejectsInvalidOptions(t *testing.T) {
	cases := []Options{
		{LeaseDuration: 0, BackoffBase: 1, BackoffMax: 1},
		{LeaseDuration: -1, BackoffBase: 1, BackoffMax: 1},
		{LeaseDuration: 10, BackoffBase: 0, BackoffMax: 10},
		{LeaseDuration: 10, BackoffBase: 20, BackoffMax: 10},
	}
	for _, opts := range cases {
		if _, err := NewEngine(opts); !errors.Is(err, ErrInvalidOptions) {
			t.Errorf("NewEngine(%+v) err = %v, want ErrInvalidOptions", opts, err)
		}
	}
}

func TestEnqueueValidation(t *testing.T) {
	e := mustEngine(t, DefaultOptions())

	bad := []MessageSpec{
		{ID: "", Queue: "q", VisibleAt: 0, ExpiresAt: 10, MaxAttempts: 1},
		{ID: "m", Queue: "", VisibleAt: 0, ExpiresAt: 10, MaxAttempts: 1},
		{ID: "m", Queue: "q", VisibleAt: 0, ExpiresAt: 10, MaxAttempts: 0},
		{ID: "m", Queue: "q", VisibleAt: 10, ExpiresAt: 10, MaxAttempts: 1},
	}
	for _, s := range bad {
		if err := e.Enqueue(s, 0); !errors.Is(err, ErrInvalidMessage) {
			t.Errorf("Enqueue(%+v) err = %v, want ErrInvalidMessage", s, err)
		}
	}

	valid := spec("m", "q", 0, 10, 1)
	mustEnqueue(t, e, valid, 0)
	if err := e.Enqueue(valid, 0); !errors.Is(err, ErrDuplicateMessage) {
		t.Errorf("duplicate Enqueue err = %v, want ErrDuplicateMessage", err)
	}
	if err := e.Enqueue(spec("late", "q", 0, 10, 1), 10); !errors.Is(err, ErrMessageExpired) {
		t.Errorf("enqueue at deadline err = %v, want ErrMessageExpired", err)
	}
}

func TestClaimOrderingUsesVisibleAtThenEnqueueOrder(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 1000, BackoffBase: 10, BackoffMax: 100})

	// Enqueue order: m1, m2, m3. Visibility order puts m2 and m3 first.
	mustEnqueue(t, e, spec("m1", "q", 50, 1000, 3), 0)
	mustEnqueue(t, e, spec("m2", "q", 10, 1000, 3), 0)
	mustEnqueue(t, e, spec("m3", "q", 10, 1000, 3), 0)
	// A different queue must never leak into q's ordering.
	mustEnqueue(t, e, spec("other", "other-q", 0, 1000, 3), 0)

	if _, err := e.Claim("q", "early", 0); !errors.Is(err, ErrNoMessage) {
		t.Fatalf("claim before visibility err = %v, want ErrNoMessage", err)
	}

	r := mustClaim(t, e, "q", "c1", 10)
	if r.MessageID != "m2" || r.Attempt != 1 {
		t.Fatalf("first claim = %+v, want m2/attempt 1", r)
	}
	r = mustClaim(t, e, "q", "c2", 10)
	if r.MessageID != "m3" {
		t.Fatalf("second claim = %+v, want m3 (enqueue order tiebreak)", r)
	}
	if _, err := e.Claim("q", "c3", 49); !errors.Is(err, ErrNoMessage) {
		t.Fatalf("claim at 49 err = %v, want ErrNoMessage", err)
	}
	r = mustClaim(t, e, "q", "c3", 50)
	if r.MessageID != "m1" {
		t.Fatalf("third claim = %+v, want m1", r)
	}

	// m2's visibility (10) is earlier than m1's (50): the leased receipt exposes it.
	if got := mustReceipt(t, e, r.ReceiptID, 50); got.Status != ReceiptActive {
		t.Fatalf("receipt status = %s, want active", got.Status)
	}
}

func TestClaimReceiptCarriesAttemptNumber(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 100, BackoffBase: 10, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 0, 1000, 5), 0)

	r1 := mustClaim(t, e, "q", "req-1", 0)
	if r1.Attempt != 1 || r1.IssuedAt != 0 || r1.LeaseUntil != 100 {
		t.Fatalf("receipt 1 = %+v", r1)
	}
	if _, err := e.Claim("q", "no-msg", 1); !errors.Is(err, ErrNoMessage) {
		t.Fatalf("second claim while leased err = %v, want ErrNoMessage", err)
	}
	if _, ok := e.LookupClaim("missing"); ok {
		t.Fatal("LookupClaim returned a receipt for an unknown request")
	}
	if got, ok := e.LookupClaim("req-1"); !ok || got != r1 {
		t.Fatalf("LookupClaim = %+v, %v; want %+v, true", got, ok, r1)
	}
}

// ---------------------------------------------------------------------------
// renewal

func TestRenewExtendsAndCapsAtDeadline(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 100, BackoffBase: 10, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 0, 250, 5), 0)
	r := mustClaim(t, e, "q", "claim-1", 0) // lease until 100

	got, err := e.Renew(r.ReceiptID, 100, 10) // 100 -> 200
	if err != nil {
		t.Fatalf("Renew(+100): %v", err)
	}
	if got.LeaseUntil != 200 {
		t.Fatalf("lease = %d, want 200", got.LeaseUntil)
	}

	got, err = e.Renew(r.ReceiptID, 1000, 20) // capped at 250
	if err != nil {
		t.Fatalf("Renew(capped): %v", err)
	}
	if got.LeaseUntil != 250 {
		t.Fatalf("lease = %d, want deadline 250", got.LeaseUntil)
	}

	// Already at the deadline: another renewal is a no-op, not a shortening
	// and not an error.
	got, err = e.Renew(r.ReceiptID, 500, 30)
	if err != nil {
		t.Fatalf("Renew(at deadline): %v", err)
	}
	if got.LeaseUntil != 250 {
		t.Fatalf("lease = %d, want 250", got.LeaseUntil)
	}
	if n := countKind(e.Journal(), RecordRenew); n != 2 {
		t.Fatalf("renew records = %d, want 2", n)
	}

	if _, err := e.Renew("nope", 10, 30); !errors.Is(err, ErrUnknownReceipt) {
		t.Fatalf("unknown receipt err = %v, want ErrUnknownReceipt", err)
	}
	if _, err := e.Renew(r.ReceiptID, 0, 30); !errors.Is(err, ErrInvalidExtension) {
		t.Fatalf("zero extension err = %v, want ErrInvalidExtension", err)
	}
}

func TestRenewRejectsLapsedLeaseAndExpiredMessage(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 0, 100, 2), 0)
	r := mustClaim(t, e, "q", "claim-1", 0) // lease until 10

	if _, err := e.Renew(r.ReceiptID, 5, 10); !errors.Is(err, ErrLeaseExpired) {
		t.Fatalf("renew at expiry err = %v, want ErrLeaseExpired", err)
	}

	// After the lease lapses the message is pending again; a fresh claim
	// supersedes the old receipt.
	mustClaim(t, e, "q", "claim-2", 15)
	if _, err := e.Renew(r.ReceiptID, 5, 16); !errors.Is(err, ErrStaleReceipt) {
		t.Fatalf("renew of superseded receipt err = %v, want ErrStaleReceipt", err)
	}

	// Deadline ends the message; even the current receipt cannot renew.
	e2 := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 100})
	mustEnqueue(t, e2, spec("m2", "q", 0, 20, 3), 0)
	r2 := mustClaim(t, e2, "q", "claim-a", 0)
	e2.Sweep(20)
	if _, err := e2.Renew(r2.ReceiptID, 5, 20); !errors.Is(err, ErrMessageExpired) {
		t.Fatalf("renew after deadline err = %v, want ErrMessageExpired", err)
	}
}

// ---------------------------------------------------------------------------
// lapse, backoff, fencing

func TestLeaseLapseBackoffRedeliveryAndAttemptLimit(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 0, 1000, 3), 0)

	r1 := mustClaim(t, e, "q", "claim-1", 0) // attempt 1, lease until 10
	if r1.Attempt != 1 {
		t.Fatalf("attempt = %d, want 1", r1.Attempt)
	}

	e.Sweep(10) // lapse: visibleAt = 10 + backoff(1) = 15
	v := mustMessage(t, e, "m", 10)
	if v.State != StatePending || v.VisibleAt != 15 || v.CompletedAttempts != 1 {
		t.Fatalf("after lapse = %+v", v)
	}
	if _, err := e.Claim("q", "too-early", 14); !errors.Is(err, ErrNoMessage) {
		t.Fatalf("claim before backoff err = %v, want ErrNoMessage", err)
	}
	if got := mustReceipt(t, e, r1.ReceiptID, 14); !got.Expired || got.Superseded {
		t.Fatalf("old receipt after lapse = %+v, want expired and not superseded", got)
	}

	r2 := mustClaim(t, e, "q", "claim-2", 15) // attempt 2, lease until 25
	if r2.MessageID != "m" || r2.Attempt != 2 {
		t.Fatalf("receipt 2 = %+v", r2)
	}
	if got := mustReceipt(t, e, r1.ReceiptID, 15); !got.Expired || !got.Superseded {
		t.Fatalf("old receipt after reclaim = %+v, want expired and superseded", got)
	}

	e.Sweep(25) // lapse: visibleAt = 25 + backoff(2) = 35
	if v := mustMessage(t, e, "m", 25); v.CompletedAttempts != 2 || v.VisibleAt != 35 {
		t.Fatalf("after second lapse = %+v", v)
	}

	r3 := mustClaim(t, e, "q", "claim-3", 35) // attempt 3, lease until 45
	if r3.Attempt != 3 {
		t.Fatalf("attempt = %d, want 3", r3.Attempt)
	}
	e.Sweep(45) // attempt limit reached
	v = mustMessage(t, e, "m", 45)
	if v.State != StateDead || v.CompletedAttempts != 3 || v.Attempts != 3 {
		t.Fatalf("after exhaustion = %+v", v)
	}

	journal := e.Journal()
	if n := countKind(journal, RecordLapse); n != 2 {
		t.Fatalf("lapse records = %d, want 2", n)
	}
	if n := countKind(journal, RecordDead); n != 1 {
		t.Fatalf("dead records = %d, want 1", n)
	}
	if _, err := e.Claim("q", "claim-4", 46); !errors.Is(err, ErrNoMessage) {
		t.Fatalf("claim after exhaustion err = %v, want ErrNoMessage", err)
	}
}

func TestOldReceiptCannotAckOrRenewNewLease(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 0, 1000, 5), 0)

	old := mustClaim(t, e, "q", "claim-old", 0) // attempt 1, lease until 10
	e.Sweep(10)                                 // lapse, visible at 15
	fresh := mustClaim(t, e, "q", "claim-new", 15)
	if fresh.Attempt != 2 {
		t.Fatalf("fresh attempt = %d, want 2", fresh.Attempt)
	}

	// The stale acknowledgement is processed first, before the new receipt is
	// acknowledged, and still must not confirm the new lease.
	if _, err := e.Ack(old.ReceiptID, "ack-old", 16); !errors.Is(err, ErrStaleReceipt) {
		t.Fatalf("stale ack err = %v, want ErrStaleReceipt", err)
	}
	if _, err := e.Renew(old.ReceiptID, 5, 16); !errors.Is(err, ErrStaleReceipt) {
		t.Fatalf("stale renew err = %v, want ErrStaleReceipt", err)
	}
	if v := mustMessage(t, e, "m", 16); v.State != StateLeased || v.CurrentReceiptID != fresh.ReceiptID {
		t.Fatalf("message after stale ack = %+v, want still leased by fresh receipt", v)
	}

	// The new receipt still acknowledges successfully; the old attempt stays
	// counted as a completed (failed) attempt.
	res := mustAck(t, e, fresh.ReceiptID, "ack-new", 17)
	if res.Attempt != 2 {
		t.Fatalf("ack attempt = %d, want 2", res.Attempt)
	}
	v := mustMessage(t, e, "m", 17)
	if v.State != StateAcked || v.AckedAttempt != 2 || v.Attempts != 2 || v.CompletedAttempts != 2 {
		t.Fatalf("final view = %+v", v)
	}
	if v.VisibleAt != 15 {
		t.Fatalf("visibleAt = %d, want 15", v.VisibleAt)
	}
	if _, err := e.Ack(old.ReceiptID, "ack-old-2", 17); !errors.Is(err, ErrStaleReceipt) {
		t.Fatalf("stale ack after success err = %v, want ErrStaleReceipt", err)
	}
}

func TestStaleAckBeforeAnyReclaimFailsOnlyByLease(t *testing.T) {
	// A receipt whose lease lapsed, before the message is reclaimed, is
	// expired: it cannot acknowledge.
	e := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 0, 1000, 5), 0)
	r := mustClaim(t, e, "q", "claim-1", 0)
	if _, err := e.Ack(r.ReceiptID, "ack-1", 10); !errors.Is(err, ErrLeaseExpired) {
		t.Fatalf("ack at lease end err = %v, want ErrLeaseExpired", err)
	}
	if v := mustMessage(t, e, "m", 10); v.State != StatePending || v.CompletedAttempts != 1 {
		t.Fatalf("view after lapsed ack = %+v", v)
	}
}

// ---------------------------------------------------------------------------
// deadline / lease tie and single terminal record

func TestDeadlineWinsWhenItCoincidesWithLeaseEndAndExpiresOnce(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 100, BackoffBase: 10, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 0, 100, 5), 0)
	r := mustClaim(t, e, "q", "claim-1", 0) // capped exactly at deadline 100
	if r.LeaseUntil != 100 {
		t.Fatalf("lease = %d, want capped at 100", r.LeaseUntil)
	}

	e.Sweep(100)
	v := mustMessage(t, e, "m", 100)
	if v.State != StateExpired {
		t.Fatalf("state = %s, want expired (deadline wins over lease end)", v.State)
	}
	if v.CompletedAttempts != 1 || v.Attempts != 1 {
		t.Fatalf("view = %+v, want 1 attempt completed", v)
	}

	// Sweeping repeatedly at and past the deadline must not record another
	// terminal transition.
	e.Sweep(100)
	e.Sweep(500)
	e.Sweep(1000)
	journal := e.Journal()
	if n := countKind(journal, RecordExpire); n != 1 {
		t.Fatalf("expire records = %d, want exactly 1", n)
	}
	if n := countKind(journal, RecordDead); n != 0 {
		t.Fatalf("dead records = %d, want 0", n)
	}
	if n := countKind(journal, RecordLapse); n != 0 {
		t.Fatalf("lapse records = %d, want 0", n)
	}

	if got := mustReceipt(t, e, r.ReceiptID, 100); got.Status != ReceiptExpired || !got.Expired {
		t.Fatalf("receipt = %+v, want expired", got)
	}
	if _, err := e.Ack(r.ReceiptID, "ack-1", 100); !errors.Is(err, ErrMessageExpired) {
		t.Fatalf("ack at deadline err = %v, want ErrMessageExpired", err)
	}
	if _, err := e.Claim("q", "claim-2", 100); !errors.Is(err, ErrNoMessage) {
		t.Fatalf("claim at deadline err = %v, want ErrNoMessage", err)
	}
}

func TestNeverLeasedMessageExpiresOnce(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 50, 100, 2), 0)
	e.Sweep(100)
	v := mustMessage(t, e, "m", 100)
	if v.State != StateExpired || v.CompletedAttempts != 0 || v.Attempts != 0 {
		t.Fatalf("view = %+v", v)
	}
	e.Sweep(101)
	if n := countKind(e.Journal(), RecordExpire); n != 1 {
		t.Fatalf("expire records = %d, want 1", n)
	}
}

// ---------------------------------------------------------------------------
// request-id replay and crash recovery

func TestClaimRequestIDReturnsSameReceiptAndDetectsConflicts(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 100, BackoffBase: 10, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 0, 1000, 5), 0)
	mustEnqueue(t, e, spec("n", "q2", 0, 1000, 5), 0)

	first := mustClaim(t, e, "q", "req-abc", 0)
	again := mustClaim(t, e, "q", "req-abc", 1) // time moved, still replay
	if again != first {
		t.Fatalf("replayed claim = %+v, want %+v", again, first)
	}

	// Replaying the request after its lease has lapsed still yields the same
	// (now expired) receipt rather than leasing another message.
	e.Sweep(100)
	replayAfterLapse := mustClaim(t, e, "q", "req-abc", 120)
	if replayAfterLapse != first {
		t.Fatalf("replayed claim after lapse = %+v, want %+v", replayAfterLapse, first)
	}
	if got := mustReceipt(t, e, first.ReceiptID, 120); !got.Expired || got.Superseded {
		t.Fatalf("receipt after lapse = %+v, want expired, not superseded", got)
	}
	if v := mustMessage(t, e, first.MessageID, 120); v.Attempts != 1 || v.CompletedAttempts != 1 {
		t.Fatalf("view after replayed lapse = %+v", v)
	}
	if n := countKind(e.Journal(), RecordClaim); n != 1 {
		t.Fatalf("claim records = %d, want 1", n)
	}
	if _, err := e.Claim("q2", "req-abc", 1); !errors.Is(err, ErrRequestConflict) {
		t.Fatalf("cross-queue reuse err = %v, want ErrRequestConflict", err)
	}
	if _, err := e.Claim("", "req", 1); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("empty queue err = %v, want ErrInvalidRequest", err)
	}
	if _, err := e.Claim("q", "", 1); !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("empty request id err = %v, want ErrInvalidRequest", err)
	}
}

// TestRecoverWhenClaimRecordedButResponseLost models the crash window between
// persisting the claim record and sending the receipt to the caller.
func TestRecoverWhenClaimRecordedButResponseLost(t *testing.T) {
	opts := Options{LeaseDuration: 100, BackoffBase: 10, BackoffMax: 100}
	e := mustEngine(t, opts)
	mustEnqueue(t, e, spec("m", "q", 0, 1000, 5), 0)

	claimed := mustClaim(t, e, "q", "claim-req-1", 0)

	// The journal was fsynced, the response never left the process.
	data, err := EncodeJournal(e.Journal())
	if err != nil {
		t.Fatalf("EncodeJournal: %v", err)
	}
	records, err := DecodeJournal(data)
	if err != nil {
		t.Fatalf("DecodeJournal: %v", err)
	}
	restarted, err := NewEngineFromJournal(records)
	if err != nil {
		t.Fatalf("NewEngineFromJournal: %v", err)
	}

	// The caller retries with the same request id and receives the identical
	// receipt, with no new claim record and no second attempt.
	replayed := mustClaim(t, restarted, "q", "claim-req-1", 1)
	if replayed != claimed {
		t.Fatalf("replayed receipt = %+v, want %+v", replayed, claimed)
	}
	got, ok := restarted.LookupClaim("claim-req-1")
	if !ok || got != claimed {
		t.Fatalf("LookupClaim after restart = %+v, %v", got, ok)
	}
	if n := countKind(restarted.Journal(), RecordClaim); n != 1 {
		t.Fatalf("claim records after restart = %d, want 1", n)
	}
	if v := mustMessage(t, restarted, "m", 1); v.State != StateLeased ||
		v.Attempts != 1 || v.CurrentReceiptID != claimed.ReceiptID || v.LeaseUntil != 100 {
		t.Fatalf("recovered view = %+v", v)
	}

	// The timed lease still behaves normally after recovery.
	restarted.Sweep(100)
	if v := mustMessage(t, restarted, "m", 100); v.State != StatePending || v.VisibleAt != 110 {
		t.Fatalf("view after lapsed recovered lease = %+v", v)
	}
	if got := mustReceipt(t, restarted, claimed.ReceiptID, 100); !got.Expired {
		t.Fatalf("recovered receipt = %+v, want expired", got)
	}
}

// TestRecoverWhenAckRecordedButConnectionDropped models the crash window
// between persisting the acknowledgement and delivering its response.
func TestRecoverWhenAckRecordedButConnectionDropped(t *testing.T) {
	opts := Options{LeaseDuration: 100, BackoffBase: 10, BackoffMax: 100}
	e := mustEngine(t, opts)
	mustEnqueue(t, e, spec("m", "q", 0, 1000, 5), 0)
	r := mustClaim(t, e, "q", "claim-1", 0)
	acked := mustAck(t, e, r.ReceiptID, "ack-1", 10)

	data, err := EncodeJournal(e.Journal())
	if err != nil {
		t.Fatalf("EncodeJournal: %v", err)
	}
	records, err := DecodeJournal(data)
	if err != nil {
		t.Fatalf("DecodeJournal: %v", err)
	}
	restarted, err := NewEngineFromJournal(records)
	if err != nil {
		t.Fatalf("NewEngineFromJournal: %v", err)
	}

	// Lookup replays the durable outcome without touching the state machine.
	if got, ok := restarted.LookupAck("ack-1"); !ok || got != acked {
		t.Fatalf("LookupAck after restart = %+v, %v; want %+v", got, ok, acked)
	}
	// Retrying the same ack (same request id) is idempotent.
	again := mustAck(t, restarted, r.ReceiptID, "ack-1", 11)
	if again != acked {
		t.Fatalf("replayed ack = %+v, want %+v", again, acked)
	}
	before := len(restarted.Journal())
	_ = mustAck(t, restarted, r.ReceiptID, "ack-1", 12)
	if len(restarted.Journal()) != before {
		t.Fatal("idempotent ack must not append journal records")
	}

	v := mustMessage(t, restarted, "m", 12)
	if v.State != StateAcked || v.AckedAttempt != 1 || v.CompletedAttempts != 1 || v.Attempts != 1 {
		t.Fatalf("view after ack recovery = %+v", v)
	}
	if n := countKind(restarted.Journal(), RecordAck); n != 1 {
		t.Fatalf("ack records = %d, want 1", n)
	}
	// Acking the same receipt without the request id is also idempotent.
	if res, err := restarted.Ack(r.ReceiptID, "ack-other", 13); err != nil || res != acked {
		t.Fatalf("direct re-ack = %+v, %v; want %+v", res, err, acked)
	}
	// A different receipt can never acknowledge this message.
	if _, err := restarted.Ack("rcpt-999", "ack-2", 13); !errors.Is(err, ErrUnknownReceipt) {
		t.Fatalf("unknown receipt ack err = %v, want ErrUnknownReceipt", err)
	}
}

func TestAckRequestIDConflictAndExpiredAck(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 100, BackoffBase: 10, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 0, 1000, 5), 0)
	r := mustClaim(t, e, "q", "claim-1", 0)
	mustAck(t, e, r.ReceiptID, "ack-1", 10)

	if _, err := e.Ack(r.ReceiptID, "ack-2", 11); err != nil {
		t.Fatalf("second ack request for the same receipt: %v", err)
	}
	if _, err := e.Ack("rcpt-unknown", "ack-1", 11); !errors.Is(err, ErrRequestConflict) {
		t.Fatalf("reused ack request id err = %v, want ErrRequestConflict", err)
	}

	e2 := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 100})
	mustEnqueue(t, e2, spec("m2", "q", 0, 20, 5), 0)
	r2 := mustClaim(t, e2, "q", "claim-2", 0)
	e2.Sweep(20)
	if _, err := e2.Ack(r2.ReceiptID, "ack-3", 20); !errors.Is(err, ErrMessageExpired) {
		t.Fatalf("ack after deadline err = %v, want ErrMessageExpired", err)
	}
}

// ---------------------------------------------------------------------------
// observability

func TestExpiredReceiptsAndCompletedAttemptsAreObservable(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 100})
	mustEnqueue(t, e, spec("m", "q", 0, 1000, 5), 0)

	r1 := mustClaim(t, e, "q", "claim-1", 0)
	e.Sweep(10)
	r2 := mustClaim(t, e, "q", "claim-2", 15)
	e.Sweep(25)
	r3 := mustClaim(t, e, "q", "claim-3", 35)
	mustAck(t, e, r3.ReceiptID, "ack-3", 36)

	old1 := mustReceipt(t, e, r1.ReceiptID, 36)
	old2 := mustReceipt(t, e, r2.ReceiptID, 36)
	current := mustReceipt(t, e, r3.ReceiptID, 36)

	if old1.Status != ReceiptExpired || !old1.Expired || !old1.Superseded {
		t.Fatalf("receipt 1 = %+v", old1)
	}
	if old2.Status != ReceiptExpired || !old2.Expired || !old2.Superseded {
		t.Fatalf("receipt 2 = %+v", old2)
	}
	if current.Status != ReceiptAcked || current.Expired {
		t.Fatalf("receipt 3 = %+v", current)
	}
	// Receipts remain queryable by their original identity.
	if old1.Attempt != 1 || old2.Attempt != 2 || current.Attempt != 3 {
		t.Fatalf("attempt numbers = %d, %d, %d", old1.Attempt, old2.Attempt, current.Attempt)
	}

	v := mustMessage(t, e, "m", 36)
	if v.Attempts != 3 || v.CompletedAttempts != 3 || v.AckedAttempt != 3 || v.State != StateAcked {
		t.Fatalf("final view = %+v", v)
	}

	// Claim results stay retrievable for all three request ids.
	for i, want := range []Receipt{r1, r2, r3} {
		req := fmt.Sprintf("claim-%d", i+1)
		if got, ok := e.LookupClaim(req); !ok || got != want {
			t.Fatalf("LookupClaim(%q) = %+v, %v; want %+v", req, got, ok, want)
		}
	}
}

// ---------------------------------------------------------------------------
// journal persistence and determinism

func buildScriptedEngine(t *testing.T) *Engine {
	t.Helper()
	e := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 20})
	mustEnqueue(t, e, spec("a", "q", 0, 200, 3), 0)
	mustEnqueue(t, e, spec("b", "q", 5, 300, 2), 0)
	ra := mustClaim(t, e, "q", "ca", 0) // a, attempt 1
	if _, err := e.Renew(ra.ReceiptID, 5, 5); err != nil {
		t.Fatalf("Renew: %v", err)
	}
	e.Sweep(15) // a lapses (lease 15), visible 20
	rb := mustClaim(t, e, "q", "cb", 15)
	mustAck(t, e, rb.ReceiptID, "ab", 16)
	ra2 := mustClaim(t, e, "q", "ca2", 20) // a, attempt 2
	mustAck(t, e, ra2.ReceiptID, "aa2", 21)
	return e
}

func TestJournalReplaysToIdenticalState(t *testing.T) {
	e := buildScriptedEngine(t)
	journal := e.Journal()

	if journal[0].Kind != RecordConfig || journal[0].Options == nil {
		t.Fatalf("first record = %+v, want config", journal[0])
	}
	for i, rec := range journal {
		if rec.Seq != int64(i+1) {
			t.Fatalf("record %d seq = %d", i, rec.Seq)
		}
	}

	restarted, err := NewEngineFromJournal(journal)
	if err != nil {
		t.Fatalf("NewEngineFromJournal: %v", err)
	}
	if !reflect.DeepEqual(journal, restarted.Journal()) {
		t.Fatal("replayed journal differs from the original")
	}
	for _, id := range []string{"a", "b"} {
		orig := mustMessage(t, e, id, 100)
		replay := mustMessage(t, restarted, id, 100)
		if !reflect.DeepEqual(orig, replay) {
			t.Fatalf("message %q differs:\n orig=%+v\n replay=%+v", id, orig, replay)
		}
	}

	// Replay is deterministic: replaying the replayed journal yields the same
	// encoded bytes.
	b1, err := EncodeJournal(restarted.Journal())
	if err != nil {
		t.Fatalf("EncodeJournal: %v", err)
	}
	again, err := NewEngineFromJournal(restarted.Journal())
	if err != nil {
		t.Fatalf("second NewEngineFromJournal: %v", err)
	}
	b2, err := EncodeJournal(again.Journal())
	if err != nil {
		t.Fatalf("EncodeJournal: %v", err)
	}
	if string(b1) != string(b2) {
		t.Fatal("journal encoding is not deterministic across replays")
	}
}

func TestJournalFilePersistence(t *testing.T) {
	e := buildScriptedEngine(t)
	path := filepath.Join(t.TempDir(), "journal.ndjson")

	if err := e.SaveJournal(path); err != nil {
		t.Fatalf("SaveJournal: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("Stat: %v", err)
	}
	if info.Size() == 0 {
		t.Fatal("journal file is empty")
	}

	restarted, err := LoadJournal(path)
	if err != nil {
		t.Fatalf("LoadJournal: %v", err)
	}
	if !reflect.DeepEqual(e.Journal(), restarted.Journal()) {
		t.Fatal("loaded journal differs from the original")
	}
	va := mustMessage(t, restarted, "a", 100)
	vb := mustMessage(t, restarted, "b", 100)
	if va.State != StateAcked || vb.State != StateAcked {
		t.Fatalf("states = %s/%s, want acked/acked", va.State, vb.State)
	}
	if got, ok := restarted.LookupClaim("ca2"); !ok || got.Attempt != 2 {
		t.Fatalf("LookupClaim after file reload = %+v, %v", got, ok)
	}
	if got, ok := restarted.LookupAck("aa2"); !ok || got.Attempt != 2 {
		t.Fatalf("LookupAck after file reload = %+v, %v", got, ok)
	}
}

func TestCorruptJournalsAreRejected(t *testing.T) {
	valid := func() []Record {
		e := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 20})
		mustEnqueue(t, e, spec("m", "q", 0, 100, 2), 0)
		mustClaim(t, e, "q", "c", 0)
		return e.Journal()
	}

	if _, err := NewEngineFromJournal(nil); !errors.Is(err, ErrCorruptJournal) {
		t.Errorf("empty journal err = %v", err)
	}
	if _, err := DecodeJournal([]byte("")); !errors.Is(err, ErrCorruptJournal) {
		t.Errorf("empty decode err = %v", err)
	}
	if _, err := DecodeJournal([]byte("{not json}\n")); !errors.Is(err, ErrCorruptJournal) {
		t.Errorf("bad json decode err = %v", err)
	}

	// Missing config.
	recs := valid()
	if _, err := NewEngineFromJournal(recs[1:]); !errors.Is(err, ErrCorruptJournal) {
		t.Errorf("missing config err = %v", err)
	}

	// Out-of-order sequence.
	recs = valid()
	recs[2].Seq = 99
	if _, err := NewEngineFromJournal(recs); !errors.Is(err, ErrCorruptJournal) {
		t.Errorf("bad seq err = %v", err)
	}

	// Unknown record kind.
	recs = valid()
	recs = append(recs, Record{Seq: 4, Kind: "bogus"})
	if _, err := NewEngineFromJournal(recs); !errors.Is(err, ErrCorruptJournal) {
		t.Errorf("unknown kind err = %v", err)
	}

	// An ack recorded after the lease had already lapsed is invalid.
	recs = valid()
	recs = append(recs, Record{
		Seq:       int64(len(recs) + 1),
		Kind:      RecordAck,
		At:        50,
		MessageID: "m",
		RequestID: "a",
		ReceiptID: "rcpt-1",
		Attempt:   1,
	})
	if _, err := NewEngineFromJournal(recs); !errors.Is(err, ErrCorruptJournal) {
		t.Errorf("late ack err = %v", err)
	}

	// A duplicate terminal transition is rejected.
	e := mustEngine(t, Options{LeaseDuration: 10, BackoffBase: 5, BackoffMax: 20})
	mustEnqueue(t, e, spec("m", "q", 0, 20, 2), 0)
	mustClaim(t, e, "q", "c", 0)
	e.Sweep(20)
	dup := e.Journal()
	dup = append(dup, Record{
		Seq:       int64(len(dup) + 1),
		Kind:      RecordExpire,
		At:        21,
		MessageID: "m",
		Attempt:   1,
	})
	if _, err := NewEngineFromJournal(dup); !errors.Is(err, ErrCorruptJournal) {
		t.Errorf("duplicate expire err = %v", err)
	}
}

// ---------------------------------------------------------------------------
// concurrency

func TestConcurrentClaimsYieldDistinctLeases(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 1000, BackoffBase: 10, BackoffMax: 100})
	const messages = 30
	for i := 0; i < messages; i++ {
		mustEnqueue(t, e, spec(fmt.Sprintf("m-%02d", i), "q", 0, 10000, 3), 0)
	}

	var mu sync.Mutex
	claimed := make(map[string]Receipt)
	var wg sync.WaitGroup
	const workers = 8
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; ; i++ {
				req := fmt.Sprintf("w%d-c%d", w, i)
				r, err := e.Claim("q", req, 0)
				if errors.Is(err, ErrNoMessage) {
					return
				}
				if err != nil {
					t.Errorf("Claim: %v", err)
					return
				}
				mu.Lock()
				if prev, exists := claimed[r.MessageID]; exists {
					t.Errorf("message %q claimed twice: %+v and %+v", r.MessageID, prev, r)
				}
				claimed[r.MessageID] = r
				mu.Unlock()
				if _, err := e.Ack(r.ReceiptID, "ack-"+req, 0); err != nil {
					t.Errorf("Ack(%q): %v", r.MessageID, err)
				}
			}
		}(w)
	}
	wg.Wait()

	if len(claimed) != messages {
		t.Fatalf("claimed %d messages, want %d", len(claimed), messages)
	}
	for id := range claimed {
		v := mustMessage(t, e, id, 0)
		if v.State != StateAcked || v.Attempts != 1 || v.CompletedAttempts != 1 {
			t.Fatalf("message %q view = %+v", id, v)
		}
	}
	if n := countKind(e.Journal(), RecordClaim); n != messages {
		t.Fatalf("claim records = %d, want %d", n, messages)
	}
	if n := countKind(e.Journal(), RecordAck); n != messages {
		t.Fatalf("ack records = %d, want %d", n, messages)
	}
}

func TestConcurrentSameRequestIDIsIdempotent(t *testing.T) {
	e := mustEngine(t, Options{LeaseDuration: 1000, BackoffBase: 10, BackoffMax: 100})
	mustEnqueue(t, e, spec("m1", "q", 0, 10000, 3), 0)
	mustEnqueue(t, e, spec("m2", "q", 0, 10000, 3), 0)

	var wg sync.WaitGroup
	results := make([]Receipt, 16)
	for i := range results {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			r, err := e.Claim("q", "shared-request", 0)
			if err != nil {
				t.Errorf("Claim: %v", err)
				return
			}
			results[i] = r
		}(i)
	}
	wg.Wait()

	for i := 1; i < len(results); i++ {
		if results[i] != results[0] {
			t.Fatalf("results[%d] = %+v, want %+v", i, results[i], results[0])
		}
	}
	if n := countKind(e.Journal(), RecordClaim); n != 1 {
		t.Fatalf("claim records = %d, want 1", n)
	}
	if v := mustMessage(t, e, results[0].MessageID, 0); v.Attempts != 1 {
		t.Fatalf("attempts = %d, want 1", v.Attempts)
	}

	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := e.Ack(results[0].ReceiptID, "shared-ack", 1); err != nil {
				t.Errorf("Ack: %v", err)
			}
		}()
	}
	wg.Wait()
	if n := countKind(e.Journal(), RecordAck); n != 1 {
		t.Fatalf("ack records = %d, want 1", n)
	}
	if v := mustMessage(t, e, results[0].MessageID, 1); v.CompletedAttempts != 1 {
		t.Fatalf("completed attempts = %d, want 1", v.CompletedAttempts)
	}
}
