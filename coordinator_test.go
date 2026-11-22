package rangedeleg

import (
	"context"
	"errors"
	"reflect"
	"sync"
	"testing"
	"time"
)

func mustOpen(t *testing.T) *Coordinator {
	t.Helper()
	c, err := Open(t.TempDir())
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = c.Close() })
	return c
}

func mustGrant(t *testing.T, c *Coordinator, client string, off, length int64) Delegation {
	t.Helper()
	d, err := c.Grant(client, off, length)
	if err != nil {
		t.Fatalf("Grant(%s,%d,%d): %v", client, off, length, err)
	}
	return d
}

func ref(d Delegation) DelegationRef {
	return DelegationRef{ID: d.ID, Generation: d.Generation}
}

func mustWriteback(t *testing.T, c *Coordinator, client string, d Delegation, commit CommitID, off int64, data string) WritebackResult {
	t.Helper()
	res, err := c.Writeback(client, ref(d), commit, off, []byte(data))
	if err != nil {
		t.Fatalf("Writeback(%s,commit=%d,off=%d,len=%d): %v", client, commit, off, len(data), err)
	}
	return res
}

func requireBytes(t *testing.T, c *Coordinator, off int64, want string) {
	t.Helper()
	got, err := c.Read(off, len(want))
	if err != nil {
		t.Fatalf("Read(%d,%d): %v", off, len(want), err)
	}
	if string(got) != want {
		t.Fatalf("bytes at [%d,%d): got %q want %q", off, off+int64(len(want)), got, want)
	}
}

func requireLength(t *testing.T, c *Coordinator, want int64) {
	t.Helper()
	if got := c.Length(); got != want {
		t.Fatalf("Length: got %d want %d", got, want)
	}
}

func requireRanges(t *testing.T, label string, got, want []Range) {
	t.Helper()
	if len(got) == 0 && len(want) == 0 {
		return
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%s ranges: got %v want %v", label, got, want)
	}
}

// TestNormalWritebackSegmentsOutOfOrderAndDuplicated covers the normal path:
// one batch delivered as several segments, out of order and with a duplicate.
func TestNormalWritebackSegmentsOutOfOrderAndDuplicated(t *testing.T) {
	c := mustOpen(t)
	d := mustGrant(t, c, "client-a", 0, 16)
	requireLength(t, c, 0)

	// Segment 3 first (out of order), then segment 1, then segment 2.
	mustWriteback(t, c, "client-a", d, 7, 8, "ijklmnop")
	mustWriteback(t, c, "client-a", d, 7, 0, "abcdef")
	res := mustWriteback(t, c, "client-a", d, 7, 6, "gh")
	if res.Duplicate {
		t.Fatalf("first delivery of segment 2 wrongly reported duplicate")
	}

	// Exact duplicate of segment 2: settles once, applies nothing new.
	dup := mustWriteback(t, c, "client-a", d, 7, 6, "gh")
	if !dup.Duplicate {
		t.Fatalf("expected duplicate for replayed segment")
	}
	requireRanges(t, "duplicate accepted", dup.Accepted, []Range{{Off: 6, Len: 2}})

	requireLength(t, c, 16)
	requireBytes(t, c, 0, "abcdefghijklmn")

	// Same commit id at a different offset is a distinct segment, not a
	// duplicate: settlement is keyed by (generation, commit, offset, length).
	third := mustWriteback(t, c, "client-a", d, 7, 14, "op")
	if third.Duplicate {
		t.Fatalf("same commit at a new offset must not be a duplicate")
	}
	requireBytes(t, c, 14, "op")
}

// TestDuplicateWithDifferentContentsConflicts rejects a replay that reuses the
// same settlement key but changes the payload.
func TestDuplicateWithDifferentContentsConflicts(t *testing.T) {
	c := mustOpen(t)
	d := mustGrant(t, c, "client-a", 0, 8)
	mustWriteback(t, c, "client-a", d, 1, 0, "abcd")

	_, err := c.Writeback("client-a", ref(d), 1, 0, []byte("wxyz"))
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("got %v want ErrConflict", err)
	}
	requireBytes(t, c, 0, "abcd")
}

// TestWritebackValidation covers boundary rejections around a delegation.
func TestWritebackValidation(t *testing.T) {
	c := mustOpen(t)
	d := mustGrant(t, c, "client-a", 8, 8)

	cases := []struct {
		name    string
		client  string
		r       DelegationRef
		commit  CommitID
		off     int64
		data    string
		wantErr error
	}{
		{"empty", "client-a", ref(d), 1, 8, "", ErrEmptyWrite},
		{"before range", "client-a", ref(d), 1, 7, "a", ErrOutsideDelegation},
		{"past range", "client-a", ref(d), 1, 14, "abcdef", ErrOutsideDelegation},
		{"wrong client", "client-b", ref(d), 1, 8, "ab", ErrClientMismatch},
		{"unknown id", "client-a", DelegationRef{ID: 999, Generation: 1}, 1, 8, "ab", ErrUnknownDelegation},
		{"stale gen", "client-a", DelegationRef{ID: d.ID, Generation: d.Generation + 1}, 1, 8, "ab", ErrStaleGeneration},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := c.Writeback(tc.client, tc.r, tc.commit, tc.off, []byte(tc.data))
			if !errors.Is(err, tc.wantErr) {
				t.Fatalf("got %v want %v", err, tc.wantErr)
			}
		})
	}

	// Exactly the last delegated byte is allowed.
	res := mustWriteback(t, c, "client-a", d, 1, 14, "xy")
	requireRanges(t, "accepted", res.Accepted, []Range{{Off: 14, Len: 2}})
	requireBytes(t, c, 14, "xy")
}

// TestTruncatePreservesBelowPointAndDiscardsAbove is the core shrink path: a
// dirty batch crosses the truncation point and is split.
func TestTruncatePreservesBelowPointAndDiscardsAbove(t *testing.T) {
	c := mustOpen(t)
	d := mustGrant(t, c, "client-a", 0, 20)
	if err := c.Extend(20); err != nil {
		t.Fatalf("Extend: %v", err)
	}
	// Old content the client is about to overwrite.
	mustWriteback(t, c, "client-a", d, 1, 0, "0123456789abcdefghij")

	h, err := c.Truncate(12)
	if err != nil {
		t.Fatalf("Truncate: %v", err)
	}

	// Truncation is not effective yet: readers still see the old length.
	requireLength(t, c, 20)
	st, ok := h.Status()
	if !ok || st.Effective {
		t.Fatalf("expected pending truncation, got %+v ok=%v", st, ok)
	}
	if len(st.Pending) != 1 || st.Pending[0].ID != d.ID {
		t.Fatalf("expected delegation %d pending recall, got %v", d.ID, st.Pending)
	}

	// A batch of dirty bytes spanning the truncation point.
	res := mustWriteback(t, c, "client-a", d, 2, 0, "ABCDEFGHIJKLMNOPQRST")
	requireRanges(t, "accepted", res.Accepted, []Range{{Off: 0, Len: 12}})
	requireRanges(t, "discarded", res.Discarded, []Range{{Off: 12, Len: 8}})
	if res.SplitAt != 12 {
		t.Fatalf("SplitAt: got %d want 12", res.SplitAt)
	}
	if res.TruncationSeq != h.Seq {
		t.Fatalf("TruncationSeq: got %d want %d", res.TruncationSeq, h.Seq)
	}

	// The discarded tail was never applied.
	requireBytes(t, c, 12, "cdefghij")

	// A segment wholly above the point is discarded, not applied.
	res = mustWriteback(t, c, "client-a", d, 3, 16, "QRST")
	requireRanges(t, "above accepted", res.Accepted, nil)
	requireRanges(t, "above discarded", res.Discarded, []Range{{Off: 16, Len: 4}})
	requireBytes(t, c, 16, "ghij")

	if err := c.ConfirmRecall("client-a", ref(d)); err != nil {
		t.Fatalf("ConfirmRecall: %v", err)
	}

	// Truncation is now effective and the reader sees the new length.
	requireLength(t, c, 12)
	st, ok = h.Status()
	if !ok || !st.Effective || st.EffectiveOrder == 0 {
		t.Fatalf("expected effective truncation, got %+v ok=%v", st, ok)
	}
	requireBytes(t, c, 0, "ABCDEFGHIJKL")
}

// TestRevokedGenerationCannotWriteAndExtensionReadsZero checks the post-recall
// guarantees: the old generation cannot write, the file can be extended, and
// the new region reads as zeros without old receipts resurrecting bytes.
func TestRevokedGenerationCannotWriteAndExtensionReadsZero(t *testing.T) {
	c := mustOpen(t)
	d := mustGrant(t, c, "client-a", 0, 16)
	if err := c.Extend(16); err != nil {
		t.Fatalf("Extend: %v", err)
	}
	mustWriteback(t, c, "client-a", d, 1, 0, "abcdefghijklmnop")

	h, err := c.Truncate(8)
	if err != nil {
		t.Fatalf("Truncate: %v", err)
	}
	mustWriteback(t, c, "client-a", d, 2, 4, "IJKLMNOPQRST") // split at 8
	if err := c.ConfirmRecall("client-a", ref(d)); err != nil {
		t.Fatalf("ConfirmRecall: %v", err)
	}
	if _, err := h.Wait(context.Background()); err != nil {
		t.Fatalf("Wait: %v", err)
	}
	requireLength(t, c, 8)
	requireBytes(t, c, 0, "abcdIJKL")

	// Extend: the newly exposed interval reads as zeros.
	if err := c.Extend(24); err != nil {
		t.Fatalf("Extend after truncation: %v", err)
	}
	requireLength(t, c, 24)
	requireBytes(t, c, 8, "\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")

	// The revoked generation must not write again ...
	if _, err := c.Writeback("client-a", ref(d), 3, 12, []byte("RESURRECT")); !errors.Is(err, ErrDelegationRevoked) {
		t.Fatalf("revoked generation write: got %v want ErrDelegationRevoked", err)
	}
	// ... and replaying the pre-truncation receipts must not restore bytes.
	if _, err := c.Writeback("client-a", ref(d), 1, 0, []byte("abcdefghijklmnop")); !errors.Is(err, ErrDelegationRevoked) {
		t.Fatalf("old receipt replay: got %v want ErrDelegationRevoked", err)
	}
	requireBytes(t, c, 8, "\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
	requireBytes(t, c, 0, "abcdIJKL")

	// A fresh grant of the same range starts a new generation. Old receipts
	// cannot impersonate it.
	d2, err := c.Grant("client-a", 0, 16)
	if err != nil {
		t.Fatalf("regrant: %v", err)
	}
	if d2.ID != d.ID || d2.Generation != d.Generation+1 {
		t.Fatalf("regrant: got id=%d gen=%d want id=%d gen=%d", d2.ID, d2.Generation, d.ID, d.Generation+1)
	}
	if _, err := c.Writeback("client-a", ref(d), 4, 12, []byte("stale")); !errors.Is(err, ErrStaleGeneration) {
		t.Fatalf("old generation after regrant: got %v want ErrStaleGeneration", err)
	}
	mustWriteback(t, c, "client-a", d2, 4, 12, "new!")
	requireBytes(t, c, 12, "new!")
}

// TestTwoIntersectingRangesShrinkThenExtend is the required composite
// scenario: two intersecting delegations with pending dirty data, a shrink
// that segments cross-point batches, then an immediate extend.
func TestTwoIntersectingRangesShrinkThenExtend(t *testing.T) {
	c := mustOpen(t)

	a := mustGrant(t, c, "client-a", 0, 16)
	b := mustGrant(t, c, "client-b", 8, 16)
	if err := c.Extend(24); err != nil {
		t.Fatalf("Extend: %v", err)
	}

	// Both clients have dirty data pending. Interleave the segments of the two
	// batches and deliver them out of order.
	mustWriteback(t, c, "client-a", a, 1, 8, "AAAAAAAA")
	mustWriteback(t, c, "client-b", b, 2, 16, "BBBBBBBB")
	mustWriteback(t, c, "client-b", b, 2, 8, "BBBBBBBB") // overlapping region, later wins
	mustWriteback(t, c, "client-a", a, 1, 0, "AAAAAAAA")
	requireLength(t, c, 24)

	// Shrink to 12: both delegations intersect the discarded region.
	h, err := c.Truncate(12)
	if err != nil {
		t.Fatalf("Truncate: %v", err)
	}
	st, _ := h.Status()
	if len(st.Pending) != 2 {
		t.Fatalf("expected both delegations recalled, got %v", st.Pending)
	}
	// Readers still see the old length while the recall is outstanding.
	requireLength(t, c, 24)
	requireBytes(t, c, 0, "AAAAAAAABBBBBBBB")

	// A batch crossing the truncation point from B is segmented; the part at
	// and above 12 is discarded for this recall.
	res := mustWriteback(t, c, "client-b", b, 3, 8, "0123456789ABCDEF")
	requireRanges(t, "B accepted", res.Accepted, []Range{{Off: 8, Len: 4}})
	requireRanges(t, "B discarded", res.Discarded, []Range{{Off: 12, Len: 12}})
	if res.SplitAt != 12 {
		t.Fatalf("B SplitAt: got %d want 12", res.SplitAt)
	}
	// Duplicates of the split segment settle once.
	dup := mustWriteback(t, c, "client-b", b, 3, 8, "0123456789ABCDEF")
	if !dup.Duplicate {
		t.Fatalf("expected duplicate of split segment")
	}

	// A's cross-point segment: only [0,12) is kept, [12,16) discarded.
	res = mustWriteback(t, c, "client-a", a, 4, 0, "abcdEFGHIJKLMNOP")
	requireRanges(t, "A accepted", res.Accepted, []Range{{Off: 0, Len: 12}})
	requireRanges(t, "A discarded", res.Discarded, []Range{{Off: 12, Len: 4}})

	// Confirm one delegation: still pending, readers still see old length.
	if err := c.ConfirmRecall("client-a", ref(a)); err != nil {
		t.Fatalf("ConfirmRecall A: %v", err)
	}
	requireLength(t, c, 24)
	requireBytes(t, c, 0, "abcdEFGHIJKLBBBB") // [12,16) still holds B's old bytes for now

	st, _ = h.Status()
	if st.Effective || len(st.Pending) != 1 || st.Pending[0].ID != b.ID {
		t.Fatalf("expected only B outstanding, got %+v", st)
	}

	if err := c.ConfirmRecall("client-b", ref(b)); err != nil {
		t.Fatalf("ConfirmRecall B: %v", err)
	}
	st, _ = h.Status()
	if !st.Effective {
		t.Fatalf("expected truncation effective, got %+v", st)
	}
	requireLength(t, c, 12)
	// Retained bytes below the point come from both batches; the overlapping
	// [8,12) keeps B's later write.
	requireBytes(t, c, 0, "abcdEFGHIJKL")

	// Immediately extend: [12,24) must read as zeros.
	if err := c.Extend(24); err != nil {
		t.Fatalf("Extend after shrink: %v", err)
	}
	requireLength(t, c, 24)
	zeroTail := string(make([]byte, 12))
	requireBytes(t, c, 12, zeroTail)

	// Old receipts from both revoked generations cannot resurrect discarded
	// bytes or the pending-but-discarded tails.
	for _, tc := range []struct {
		client string
		d      Delegation
		commit CommitID
		off    int64
		data   string
	}{
		{"client-a", a, 4, 0, "abcdEFGHIJKLMNOP"},
		{"client-b", b, 3, 8, "0123456789ABCDEF"},
		{"client-a", a, 1, 8, "AAAAAAAA"},
	} {
		if _, err := c.Writeback(tc.client, ref(tc.d), tc.commit, tc.off, []byte(tc.data)); !errors.Is(err, ErrDelegationRevoked) {
			t.Fatalf("old receipt %s commit %d: got %v want ErrDelegationRevoked", tc.client, tc.commit, err)
		}
	}
	requireBytes(t, c, 12, zeroTail)
	requireBytes(t, c, 0, "abcdEFGHIJKL")
}

// TestReopenContinuesRecallAndTruncation checks that a controller reopened on
// the same directory keeps tracking outstanding recalls and reports when the
// truncation takes effect.
func TestReopenContinuesRecallAndTruncation(t *testing.T) {
	dir := t.TempDir()
	c1, err := Open(dir)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	d := mustGrant(t, c1, "client-a", 0, 16)
	if err := c1.Extend(16); err != nil {
		t.Fatalf("Extend: %v", err)
	}
	mustWriteback(t, c1, "client-a", d, 1, 0, "abcdefghijklmnop")
	h, err := c1.Truncate(10)
	if err != nil {
		t.Fatalf("Truncate: %v", err)
	}
	seq := h.Seq
	// Durable state survives close.
	if err := c1.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}

	c2, err := Open(dir)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer c2.Close()
	requireLength(t, c2, 16)

	// The pending truncation and recalled delegation are still tracked.
	pending := c2.PendingTruncations()
	if len(pending) != 1 || pending[0].Seq != seq || pending[0].Target != 10 {
		t.Fatalf("pending after reopen: %+v", pending)
	}
	got, ok := c2.Delegation(d.ID)
	if !ok || got.State != DelegationRecalling || got.Generation != d.Generation {
		t.Fatalf("delegation after reopen: %+v ok=%v", got, ok)
	}

	// The reopened controller continues settling writebacks with the split.
	res, err := c2.Writeback("client-a", ref(d), 2, 0, []byte("ABCDEFGHIJKLMNOP"))
	if err != nil {
		t.Fatalf("writeback after reopen: %v", err)
	}
	requireRanges(t, "accepted", res.Accepted, []Range{{Off: 0, Len: 10}})
	requireRanges(t, "discarded", res.Discarded, []Range{{Off: 10, Len: 6}})

	// Wait on the reopened coordinator reports the effective moment.
	waited := make(chan TruncationStatus, 1)
	go func() {
		st, err := c2.WaitTruncation(context.Background(), seq)
		if err != nil {
			t.Errorf("WaitTruncation: %v", err)
		}
		waited <- st
	}()
	if err := c2.ConfirmRecall("client-a", ref(d)); err != nil {
		t.Fatalf("ConfirmRecall after reopen: %v", err)
	}
	select {
	case st := <-waited:
		if !st.Effective || st.EffectiveOrder == 0 {
			t.Fatalf("waited status: %+v", st)
		}
	case <-time.After(2 * time.Second):
		t.Fatalf("WaitTruncation did not observe effectiveness")
	}
	requireLength(t, c2, 10)
	requireBytes(t, c2, 0, "ABCDEFGHIJ")
}

// TestTruncationOrderingAndPendingGuards covers boundary rules for queued
// shrinks and operations blocked while a recall is outstanding.
func TestTruncationOrderingAndPendingGuards(t *testing.T) {
	c := mustOpen(t)
	d := mustGrant(t, c, "client-a", 0, 32)
	if err := c.Extend(32); err != nil {
		t.Fatalf("Extend: %v", err)
	}

	if _, err := c.Truncate(32); !errors.Is(err, ErrNotShrinking) {
		t.Fatalf("truncate to current length: got %v want ErrNotShrinking", err)
	}
	if _, err := c.Truncate(40); !errors.Is(err, ErrNotShrinking) {
		t.Fatalf("truncate to larger: got %v want ErrNotShrinking", err)
	}
	if _, err := c.Truncate(-1); !errors.Is(err, ErrInvalidSize) {
		t.Fatalf("negative truncate: got %v want ErrInvalidSize", err)
	}

	h1, err := c.Truncate(20)
	if err != nil {
		t.Fatalf("Truncate(20): %v", err)
	}
	if _, err := c.Truncate(24); !errors.Is(err, ErrTruncationOrder) {
		t.Fatalf("non-decreasing queued target: got %v want ErrTruncationOrder", err)
	}
	if err := c.Extend(40); !errors.Is(err, ErrTruncationPending) {
		t.Fatalf("extend while pending: got %v want ErrTruncationPending", err)
	}
	if _, err := c.Grant("client-b", 10, 16); !errors.Is(err, ErrTruncationPending) {
		t.Fatalf("grant intersecting pending truncation: got %v want ErrTruncationPending", err)
	}
	// A grant wholly below the truncation point is still allowed.
	if _, err := c.Grant("client-c", 0, 8); err != nil {
		t.Fatalf("grant below point: %v", err)
	}

	h2, err := c.Truncate(8)
	if err != nil {
		t.Fatalf("Truncate(8): %v", err)
	}
	if st, _ := h2.Status(); st.Effective || len(st.Pending) != 0 {
		t.Fatalf("queued truncation must not issue recalls yet: %+v", st)
	}

	// Note: the second grant of client-c [0,8) is below target 8, so it is not
	// recalled by either truncation.
	if err := c.ConfirmRecall("client-a", ref(d)); err != nil {
		t.Fatalf("ConfirmRecall: %v", err)
	}
	if st, _ := h1.Status(); !st.Effective {
		t.Fatalf("h1 should be effective")
	}
	if st, _ := h2.Status(); !st.Effective {
		t.Fatalf("h2 should be effective (no intersecting delegations), got %+v", st)
	}
	requireLength(t, c, 8)
}

// TestConcurrentWritebacksSettleOnceInterleaved drives many segments from
// several goroutines, including duplicates, and checks each settles once.
func TestConcurrentWritebacksSettleOnceInterleaved(t *testing.T) {
	c := mustOpen(t)
	d := mustGrant(t, c, "client-a", 0, 64)
	if err := c.Extend(64); err != nil {
		t.Fatalf("Extend: %v", err)
	}

	const segments = 8
	const segLen = 8
	var wg sync.WaitGroup
	dupCount := make([]int, segments)
	var mu sync.Mutex
	for rep := 0; rep < 3; rep++ { // each segment delivered three times
		for i := 0; i < segments; i++ {
			wg.Add(1)
			go func(i, rep int) {
				defer wg.Done()
				data := make([]byte, segLen)
				for j := range data {
					data[j] = byte('a' + i)
				}
				res, err := c.Writeback("client-a", ref(d), CommitID(100+i), int64(i*segLen), data)
				if err != nil {
					t.Errorf("segment %d rep %d: %v", i, rep, err)
					return
				}
				if res.Duplicate {
					mu.Lock()
					dupCount[i]++
					mu.Unlock()
				}
			}(i, rep)
		}
		wg.Wait()
	}

	// Across three deliveries of each segment, exactly two are duplicates.
	for i, n := range dupCount {
		if n != 2 {
			t.Fatalf("segment %d: %d duplicates, want 2", i, n)
		}
	}
	for i := 0; i < segments; i++ {
		want := ""
		for j := 0; j < segLen; j++ {
			want += string(rune('a' + i))
		}
		requireBytes(t, c, int64(i*segLen), want)
	}
}

// TestClosedCoordinator rejects work after Close.
func TestClosedCoordinator(t *testing.T) {
	c := mustOpen(t)
	d := mustGrant(t, c, "client-a", 0, 8)
	if err := c.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	if _, err := c.Grant("client-b", 0, 8); !errors.Is(err, ErrClosed) {
		t.Fatalf("Grant after close: %v", err)
	}
	if _, err := c.Writeback("client-a", ref(d), 1, 0, []byte("ab")); !errors.Is(err, ErrClosed) {
		t.Fatalf("Writeback after close: %v", err)
	}
	if _, err := c.Truncate(4); !errors.Is(err, ErrClosed) {
		t.Fatalf("Truncate after close: %v", err)
	}
	if _, err := c.WaitTruncation(context.Background(), 1); !errors.Is(err, ErrClosed) {
		t.Fatalf("WaitTruncation after close: %v", err)
	}
}

// TestZeroLengthAndReadBounds covers trivial boundaries.
func TestZeroLengthAndReadBounds(t *testing.T) {
	c := mustOpen(t)
	if _, err := c.Grant("client-a", 0, 0); !errors.Is(err, ErrInvalidSize) {
		t.Fatalf("zero-length grant: %v", err)
	}
	if _, err := c.Grant("", 0, 4); err == nil {
		t.Fatalf("empty client grant should fail")
	}
	d := mustGrant(t, c, "client-a", 0, 4)
	if _, err := c.Read(0, 1); !errors.Is(err, ErrOutOfBounds) {
		t.Fatalf("read before write: %v", err)
	}
	mustWriteback(t, c, "client-a", d, 1, 0, "abcd")
	if got, err := c.Read(4, 0); err != nil || len(got) != 0 {
		t.Fatalf("zero-length read: %q %v", got, err)
	}
	if _, err := c.Read(2, 4); !errors.Is(err, ErrOutOfBounds) {
		t.Fatalf("read past end: %v", err)
	}
}
