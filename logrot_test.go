package logrot

import (
	"errors"
	"testing"
	"time"
)

var t0 = time.Unix(1_700_000_000, 0)

type testClock struct{ t time.Time }

func (c *testClock) now() time.Time { return c.t }

func normal(id string, size int) Event {
	return Event{ID: id, Kind: KindNormal, Timestamp: t0, Size: size, Payload: "n:" + id}
}

func security(id string, size int) Event {
	return Event{ID: id, Kind: KindSecurity, Timestamp: t0, Size: size, Payload: "s:" + id}
}

func mustAppend(t *testing.T, st *Store, ev Event) {
	t.Helper()
	if err := st.Append(ev); err != nil {
		t.Fatalf("Append(%s) failed: %v", ev.ID, err)
	}
}

// Acceptable path: events are received, the segment rotates and seals, the
// security event is confirmed against the segment digest, and the segment
// is cleaned up once the retention period ends.
func TestAppendConfirmCleanupAccepted(t *testing.T) {
	clock := &testClock{t: t0}
	st := New(Config{CapacityBytes: 1000, MaxSegmentBytes: 200, Retention: time.Hour}, clock.now)

	mustAppend(t, st, security("sec-1", 100))
	mustAppend(t, st, normal("n-1", 100))
	mustAppend(t, st, normal("n-2", 50)) // forces rotation, seals seg-1

	sealedID := "seg-1"
	seg, ok := st.Segment(sealedID)
	if !ok || seg.State != StateSealed {
		t.Fatalf("seg-1 should be sealed, got %+v (ok=%v)", seg, ok)
	}
	if seg.Digest == "" {
		t.Fatal("sealed segment must carry a digest")
	}

	if err := st.ConfirmArchive(sealedID, seg.Digest, "sec-1"); err != nil {
		t.Fatalf("confirm with correct digest rejected: %v", err)
	}

	// Retention not over: cleanup must keep the segment.
	clock.t = t0.Add(time.Hour - time.Nanosecond)
	if removed := st.Cleanup(clock.t); len(removed) != 0 {
		t.Fatalf("cleanup before retention end removed %v", removed)
	}
	if _, ok := st.Segment(sealedID); !ok {
		t.Fatal("confirmed segment removed before retention end")
	}

	// Retention over (boundary: exactly SealedAt+Retention): segment goes.
	clock.t = t0.Add(time.Hour)
	removed := st.Cleanup(clock.t)
	if len(removed) != 1 || removed[0] != sealedID {
		t.Fatalf("expected [%s] removed at retention boundary, got %v", sealedID, removed)
	}
	if _, ok := st.Segment(sealedID); ok {
		t.Fatal("segment still present after cleanup")
	}
}

// Rejected path: a confirmation carrying a stale/wrong digest is refused
// and confirms nothing, so the security event keeps the segment retained.
func TestConfirmWithWrongDigestRejected(t *testing.T) {
	clock := &testClock{t: t0}
	st := New(Config{CapacityBytes: 1000, MaxSegmentBytes: 200, Retention: time.Hour}, clock.now)

	mustAppend(t, st, security("sec-1", 100))
	mustAppend(t, st, normal("n-1", 100))
	mustAppend(t, st, normal("n-2", 50)) // seals seg-1

	err := st.ConfirmArchive("seg-1", "deadbeef", "sec-1")
	if !errors.Is(err, ErrDigestMismatch) {
		t.Fatalf("expected ErrDigestMismatch, got %v", err)
	}
	seg, _ := st.Segment("seg-1")
	if len(seg.Confirmed) != 0 {
		t.Fatalf("rejected confirmation must not mark events, got %v", seg.Confirmed)
	}

	// Even far past retention the unconfirmed security event retains the segment.
	clock.t = t0.Add(100 * time.Hour)
	if removed := st.Cleanup(clock.t); len(removed) != 0 {
		t.Fatalf("segment with unconfirmed security event was cleaned: %v", removed)
	}

	// Confirming an active (unsealed) segment and a missing segment are rejected too.
	if err := st.ConfirmArchive(st.ActiveID(), "whatever", "x"); !errors.Is(err, ErrSegmentNotSealed) {
		t.Fatalf("expected ErrSegmentNotSealed, got %v", err)
	}
	if err := st.ConfirmArchive("seg-999", "whatever", "x"); !errors.Is(err, ErrSegmentNotFound) {
		t.Fatalf("expected ErrSegmentNotFound, got %v", err)
	}
}

// Interleaved constraint: re-segmentation invalidates old confirmations.
// Confirmations recorded before the split do not carry over, and the old
// digest cannot be replayed against the new segments.
func TestResegmentInvalidatesOldConfirmation(t *testing.T) {
	clock := &testClock{t: t0}
	st := New(Config{CapacityBytes: 1000, MaxSegmentBytes: 400, Retention: time.Hour}, clock.now)

	mustAppend(t, st, normal("n-1", 100))
	mustAppend(t, st, security("sec-1", 100))
	mustAppend(t, st, normal("n-2", 100))
	mustAppend(t, st, normal("n-3", 200)) // seals seg-1 with 3 events

	old, _ := st.Segment("seg-1")
	oldDigest := old.Digest
	if err := st.ConfirmArchive("seg-1", oldDigest, "sec-1"); err != nil {
		t.Fatalf("pre-split confirm failed: %v", err)
	}

	ids, err := st.Resegment("seg-1", 1)
	if err != nil {
		t.Fatalf("Resegment failed: %v", err)
	}
	if len(ids) != 2 {
		t.Fatalf("expected 2 new segments, got %v", ids)
	}

	// Old segment is gone; old digest is useless everywhere.
	if err := st.ConfirmArchive("seg-1", oldDigest, "sec-1"); !errors.Is(err, ErrSegmentNotFound) {
		t.Fatalf("confirm against removed segment: expected ErrSegmentNotFound, got %v", err)
	}
	if err := st.ConfirmArchive(ids[1], oldDigest, "sec-1"); !errors.Is(err, ErrDigestMismatch) {
		t.Fatalf("old digest replayed against new segment: expected ErrDigestMismatch, got %v", err)
	}

	// The pre-split confirmation of sec-1 must NOT carry over.
	withSec, ok := st.Segment(ids[1])
	if !ok {
		t.Fatalf("new segment %s missing", ids[1])
	}
	if withSec.Confirmed["sec-1"] {
		t.Fatal("confirmation carried over re-segmentation")
	}

	// Past retention: the part without security events is cleaned, the
	// part with the still-unconfirmed security event is retained.
	clock.t = t0.Add(time.Hour)
	removed := st.Cleanup(clock.t)
	if len(removed) != 1 || removed[0] != ids[0] {
		t.Fatalf("expected only %s removed, got %v", ids[0], removed)
	}
	if _, ok := st.Segment(ids[1]); !ok {
		t.Fatal("segment with unconfirmed security event removed")
	}

	// Re-confirm against the NEW digest: then cleanup may proceed.
	if err := st.ConfirmArchive(ids[1], withSec.Digest, "sec-1"); err != nil {
		t.Fatalf("confirm with new digest failed: %v", err)
	}
	if removed := st.Cleanup(clock.t); len(removed) != 1 || removed[0] != ids[1] {
		t.Fatalf("expected %s removed after re-confirmation, got %v", ids[1], removed)
	}
}

// Space pressure: with an unconfirmed security event the store cannot
// reclaim, so Append returns a retryable rejection; once the event is
// confirmed and retention ends, the same Append succeeds. Also pins the
// retention boundary.
func TestNoSpaceRetryableUntilConfirmedAndRetained(t *testing.T) {
	clock := &testClock{t: t0}
	st := New(Config{CapacityBytes: 400, MaxSegmentBytes: 200, Retention: time.Hour}, clock.now)

	mustAppend(t, st, security("sec-1", 150))
	mustAppend(t, st, normal("n-1", 50)) // seg-1 full (200)
	mustAppend(t, st, normal("n-2", 100)) // rotates into seg-2; used=300

	// used=300, appending 200 needs 100 more; seg-1 is not cleanable.
	err := st.Append(normal("n-3", 200))
	var nse *NoSpaceError
	if !errors.As(err, &nse) {
		t.Fatalf("expected *NoSpaceError, got %v", err)
	}
	if !nse.Retryable() {
		t.Fatal("NoSpaceError must be retryable")
	}
	if _, ok := st.Segment("seg-1"); !ok {
		t.Fatal("unconfirmed security segment reclaimed under pressure")
	}

	// Confirm, but stay 1ns before retention end: still rejected.
	seg1, _ := st.Segment("seg-1")
	if err := st.ConfirmArchive("seg-1", seg1.Digest, "sec-1"); err != nil {
		t.Fatalf("confirm failed: %v", err)
	}
	clock.t = t0.Add(time.Hour - time.Nanosecond)
	if err := st.Append(normal("n-3", 200)); !errors.As(err, &nse) {
		t.Fatalf("before retention end expected *NoSpaceError, got %v", err)
	}

	// At exactly the retention boundary the retry succeeds.
	clock.t = t0.Add(time.Hour)
	if err := st.Append(normal("n-3", 200)); err != nil {
		t.Fatalf("retry after retention end failed: %v", err)
	}
	if _, ok := st.Segment("seg-1"); ok {
		t.Fatal("confirmed expired segment not reclaimed to make room")
	}
}

// Reclaim order: ordinary expired segments free space before segments that
// hold (confirmed) security events, even when the security segment is older.
func TestOrdinaryExpiredReclaimedFirst(t *testing.T) {
	clock := &testClock{t: t0}
	st := New(Config{CapacityBytes: 500, MaxSegmentBytes: 200, Retention: time.Hour}, clock.now)

	// seg-1: security segment, sealed EARLIER.
	mustAppend(t, st, security("sec-1", 100))
	mustAppend(t, st, normal("n-1", 100))
	// seg-2: ordinary segment, sealed later.
	clock.t = t0.Add(time.Minute)
	mustAppend(t, st, normal("n-2", 100))
	mustAppend(t, st, normal("n-3", 100))
	// seg-3 (active): 100 bytes used; total used = 500.
	mustAppend(t, st, normal("n-4", 100))

	seg1, _ := st.Segment("seg-1")
	if err := st.ConfirmArchive("seg-1", seg1.Digest, "sec-1"); err != nil {
		t.Fatalf("confirm failed: %v", err)
	}

	// Both segments expired; appending 100 needs only one segment's room.
	clock.t = t0.Add(2 * time.Hour)
	mustAppend(t, st, normal("n-5", 100))

	if _, ok := st.Segment("seg-2"); ok {
		t.Fatal("ordinary expired segment should have been reclaimed first")
	}
	if _, ok := st.Segment("seg-1"); !ok {
		t.Fatal("security segment reclaimed while an ordinary expired segment was available")
	}
}

// Capacity boundary: an event that exactly fills the capacity is accepted;
// one byte more is rejected with a retryable error. An oversized event is
// rejected non-retryably.
func TestCapacityBoundary(t *testing.T) {
	clock := &testClock{t: t0}
	st := New(Config{CapacityBytes: 200, MaxSegmentBytes: 200, Retention: time.Hour}, clock.now)

	mustAppend(t, st, normal("n-1", 200)) // exactly fills capacity
	if st.Used() != 200 {
		t.Fatalf("used = %d, want 200", st.Used())
	}
	var nse *NoSpaceError
	if err := st.Append(normal("n-2", 1)); !errors.As(err, &nse) {
		t.Fatalf("one byte over capacity: expected *NoSpaceError, got %v", err)
	}
	if err := st.Append(normal("huge", 201)); !errors.Is(err, ErrEventTooLarge) {
		t.Fatalf("oversized event: expected ErrEventTooLarge, got %v", err)
	}
}

// Recovery after an interrupted rotation: the snapshot is taken right
// after the new segment was committed but before the old one was sealed.
// Recovery must keep the committed new segment as active and seal the old
// one — not confuse it with a deletable segment.
func TestRecoverInterruptedRotation(t *testing.T) {
	clock := &testClock{t: t0}
	cfg := Config{CapacityBytes: 1000, MaxSegmentBytes: 100, Retention: time.Hour}
	st := New(cfg, clock.now)

	mustAppend(t, st, normal("n-1", 100)) // fills seg-1

	var snap Snapshot
	st.hook = func(e JournalEntry) {
		if e.Op == opActivate && e.SegmentID == "seg-2" {
			snap = st.Snapshot() // crash: new segment committed+activated, old not yet sealed
		}
	}
	mustAppend(t, st, normal("n-2", 10)) // triggers rotation
	st.hook = nil

	rec := Recover(cfg, clock.now, snap)

	old, ok := rec.Segment("seg-1")
	if !ok || old.State != StateSealed || old.Digest == "" {
		t.Fatalf("old segment should be sealed by recovery, got %+v (ok=%v)", old, ok)
	}
	if rec.ActiveID() != "seg-2" {
		t.Fatalf("committed new segment should be active, got %q", rec.ActiveID())
	}
	if _, ok := rec.Segment("seg-2"); !ok {
		t.Fatal("committed new segment lost during recovery")
	}
	// Store keeps working after recovery.
	mustAppend(t, rec, normal("n-3", 10))
	seg2, _ := rec.Segment("seg-2")
	if len(seg2.Events) != 1 || seg2.Events[0].ID != "n-3" {
		t.Fatalf("append after recovery went to wrong segment: %+v", seg2.Events)
	}
}

// Recovery after an interrupted delete: the snapshot is taken right after
// pending_delete was journaled. Recovery must finish the delete, while the
// committed active segment survives.
func TestRecoverInterruptedDelete(t *testing.T) {
	clock := &testClock{t: t0}
	cfg := Config{CapacityBytes: 1000, MaxSegmentBytes: 200, Retention: time.Hour}
	st := New(cfg, clock.now)

	mustAppend(t, st, security("sec-1", 100))
	mustAppend(t, st, normal("n-1", 100))
	mustAppend(t, st, normal("n-2", 50)) // seals seg-1, opens seg-2

	seg1, _ := st.Segment("seg-1")
	if err := st.ConfirmArchive("seg-1", seg1.Digest, "sec-1"); err != nil {
		t.Fatalf("confirm failed: %v", err)
	}
	clock.t = t0.Add(time.Hour)

	var snap Snapshot
	st.hook = func(e JournalEntry) {
		if e.Op == opPendingDelete {
			snap = st.Snapshot() // crash: delete journaled but not finished
		}
	}
	if removed := st.Cleanup(clock.t); len(removed) != 1 {
		t.Fatalf("cleanup removed %v", removed)
	}
	st.hook = nil

	rec := Recover(cfg, clock.now, snap)
	if _, ok := rec.Segment("seg-1"); ok {
		t.Fatal("pending-delete segment survived recovery")
	}
	if _, ok := rec.Segment("seg-2"); !ok {
		t.Fatal("committed active segment lost during recovery")
	}
	if rec.Used() != 50 {
		t.Fatalf("used after recovery = %d, want 50", rec.Used())
	}
}

// Recovery after a crash before commit: a segment whose creation was
// journaled but never committed is discarded, and appends fall back to the
// previous active segment.
func TestRecoverUncommittedSegmentDropped(t *testing.T) {
	clock := &testClock{t: t0}
	cfg := Config{CapacityBytes: 1000, MaxSegmentBytes: 100, Retention: time.Hour}
	st := New(cfg, clock.now)

	mustAppend(t, st, normal("n-1", 100)) // fills seg-1

	var snap Snapshot
	st.hook = func(e JournalEntry) {
		if e.Op == opCreate && e.SegmentID == "seg-2" {
			snap = st.Snapshot() // crash: seg-2 created but never committed
		}
	}
	mustAppend(t, st, normal("n-2", 10))
	st.hook = nil

	rec := Recover(cfg, clock.now, snap)
	if _, ok := rec.Segment("seg-2"); ok {
		t.Fatal("uncommitted segment survived recovery")
	}
	if rec.ActiveID() != "seg-1" {
		t.Fatalf("active should fall back to seg-1, got %q", rec.ActiveID())
	}
	// seg-1 was never sealed by the interrupted rotation; it stays active
	// and the store remains usable.
	mustAppend(t, rec, normal("n-3", 10))
	ids := rec.SegmentIDs()
	if len(ids) != 2 { // seg-1 sealed by the new rotation + a fresh active segment
		t.Fatalf("expected 2 segments after post-recovery rotation, got %v", ids)
	}
}
