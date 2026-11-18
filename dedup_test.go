package dedup

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time          { return c.t }
func (c *fakeClock) advance(d time.Duration) { c.t = c.t.Add(d) }

func newTestFilter(t *testing.T, window uint64, overlap time.Duration, clk *fakeClock) *Filter {
	t.Helper()
	cfg := Config{WindowSize: window, Overlap: overlap}
	if clk != nil {
		cfg.Now = clk.now
	}
	return NewFilter(cfg)
}

// Acceptable result: first-time in-order and in-window out-of-order
// packets are accepted.
func TestAcceptsFirstTimeInOrderAndOutOfOrder(t *testing.T) {
	f := newTestFilter(t, 8, 0, nil)
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	for seq := uint64(1); seq <= 5; seq++ {
		if !f.Accept("sa1", 1, seq) {
			t.Fatalf("in-order seq %d rejected", seq)
		}
	}
	if !f.Accept("sa1", 1, 7) {
		t.Fatal("seq 7 rejected")
	}
	if !f.Accept("sa1", 1, 6) {
		t.Fatal("first-time out-of-order seq 6 inside window rejected")
	}
	if !f.Received("sa1", 1, 6) {
		t.Fatal("seq 6 not recorded as received")
	}
	if f.Received("sa1", 1, 8) {
		t.Fatal("seq 8 reported received before it arrived")
	}
}

// Rejected result: a packet already received is refused.
func TestRejectsDuplicate(t *testing.T) {
	f := newTestFilter(t, 8, 0, nil)
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	if !f.Accept("sa1", 1, 42) {
		t.Fatal("first submission rejected")
	}
	if f.Accept("sa1", 1, 42) {
		t.Fatal("duplicate accepted")
	}
	if f.Accept("sa1", 1, 42) {
		t.Fatal("duplicate accepted on third attempt")
	}
}

// Boundary: the packet exactly at the window's oldest edge is accepted,
// the one just behind it is rejected, and advancing the window pushes a
// previously in-window packet out.
func TestWindowBoundary(t *testing.T) {
	f := newTestFilter(t, 8, 0, nil)
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	if !f.Accept("sa1", 1, 10) {
		t.Fatal("first packet rejected")
	}
	// Window now covers 3..10.
	if !f.Accept("sa1", 1, 3) {
		t.Fatal("seq at oldest in-window edge (diff == size-1) rejected")
	}
	if f.Accept("sa1", 1, 2) {
		t.Fatal("seq one behind the window (diff == size) accepted")
	}
	// Advance highest to 11: window becomes 4..11, pushing 3 out.
	if !f.Accept("sa1", 1, 11) {
		t.Fatal("seq 11 rejected")
	}
	if f.Accept("sa1", 1, 3) {
		t.Fatal("seq pushed behind the window accepted")
	}
	if !f.Accept("sa1", 1, 4) {
		t.Fatal("seq at new oldest edge rejected")
	}
}

// A large jump clears stale bits: sequence numbers far below the new
// window must not be reported as received.
func TestLargeJumpClearsWindow(t *testing.T) {
	f := newTestFilter(t, 8, 0, nil)
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	if !f.Accept("sa1", 1, 5) {
		t.Fatal("seq 5 rejected")
	}
	if !f.Accept("sa1", 1, 1000) {
		t.Fatal("large forward jump rejected")
	}
	if f.Received("sa1", 1, 5) {
		t.Fatal("stale bit survived a jump larger than the window")
	}
	if f.Accept("sa1", 1, 5) {
		t.Fatal("seq far behind the window accepted")
	}
}

// Interleaved generations: the old generation advances only inside the
// overlap period; the new generation is unaffected by the old one's
// expiry, and the two windows are independent.
func TestOldGenerationBoundedByOverlap(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1_700_000_000, 0)}
	f := newTestFilter(t, 8, 10*time.Second, clk)
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	if !f.Accept("sa1", 1, 100) {
		t.Fatal("old gen seq 100 rejected before rotation")
	}
	if err := f.Rotate("sa1", 2); err != nil {
		t.Fatalf("Rotate: %v", err)
	}

	clk.advance(5 * time.Second) // inside the overlap period
	if !f.Accept("sa1", 1, 101) {
		t.Fatal("old gen rejected inside overlap period")
	}
	if !f.Accept("sa1", 2, 1) {
		t.Fatal("new gen rejected inside overlap period")
	}
	// Same sequence number is tracked independently per generation.
	if !f.Accept("sa1", 2, 100) {
		t.Fatal("new gen rejected seq already used by old gen")
	}
	if !f.Accept("sa1", 2, 99) {
		t.Fatal("new gen rejected seq 99")
	}
	if !f.Accept("sa1", 1, 99) {
		t.Fatal("old gen rejected in-window seq only used by new gen")
	}

	clk.advance(5 * time.Second) // exactly at the overlap deadline
	if f.Accept("sa1", 1, 102) {
		t.Fatal("old gen accepted at the overlap deadline")
	}

	clk.advance(time.Second) // past the overlap period
	if f.Accept("sa1", 1, 98) {
		t.Fatal("old gen accepted unseen in-window seq after overlap expired")
	}
	if !f.Accept("sa1", 2, 101) {
		t.Fatal("new gen rejected after old gen overlap expired")
	}
}

// Rotation bookkeeping errors.
func TestRotationValidation(t *testing.T) {
	f := newTestFilter(t, 8, 0, nil)
	if err := f.Rotate("missing", 2); err != ErrUnknownSA {
		t.Fatalf("Rotate on unknown SA: got %v", err)
	}
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	if err := f.AddSA("sa1", 1); err != ErrSAExists {
		t.Fatalf("duplicate AddSA: got %v", err)
	}
	if err := f.Rotate("sa1", 1); err != ErrGenerationExists {
		t.Fatalf("Rotate to existing generation: got %v", err)
	}
}

// Unknown SAs and generations are rejected.
func TestUnknownSAAndGenerationRejected(t *testing.T) {
	f := newTestFilter(t, 8, 0, nil)
	if f.Accept("ghost", 1, 1) {
		t.Fatal("packet for unknown SA accepted")
	}
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	if f.Accept("sa1", 9, 1) {
		t.Fatal("packet for unknown generation accepted")
	}
	if f.Received("ghost", 1, 1) || f.Received("sa1", 9, 1) {
		t.Fatal("unknown SA/generation reported as received")
	}
}

// Concurrency: two threads submitting the same packet — exactly one wins.
func TestConcurrentSamePacketSingleAccept(t *testing.T) {
	f := newTestFilter(t, 64, 0, nil)
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	const submitters = 32
	var wg sync.WaitGroup
	var accepted int64
	start := make(chan struct{})
	for i := 0; i < submitters; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			if f.Accept("sa1", 1, 42) {
				atomic.AddInt64(&accepted, 1)
			}
		}()
	}
	close(start)
	wg.Wait()
	if got := atomic.LoadInt64(&accepted); got != 1 {
		t.Fatalf("same packet accepted %d times, want exactly 1", got)
	}
}

// Concurrency with interleaved duplicates: every distinct in-window
// sequence number is accepted exactly once across all threads.
func TestConcurrentInterleavedDuplicates(t *testing.T) {
	const (
		workers = 8
		seqs    = 500
		rounds  = 2
	)
	f := newTestFilter(t, 1024, 0, nil)
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	var wg sync.WaitGroup
	var accepted int64
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for r := 0; r < rounds; r++ {
				for seq := uint64(1); seq <= seqs; seq++ {
					if f.Accept("sa1", 1, seq) {
						atomic.AddInt64(&accepted, 1)
					}
				}
			}
		}(w)
	}
	wg.Wait()
	if got := atomic.LoadInt64(&accepted); got != seqs {
		t.Fatalf("accepted %d packets, want %d distinct sequence numbers", got, seqs)
	}
	for seq := uint64(1); seq <= seqs; seq++ {
		if !f.Received("sa1", 1, seq) {
			t.Fatalf("seq %d not recorded after concurrent load", seq)
		}
	}
}

// Reload: a restored filter still rejects sequence numbers processed
// before the snapshot, honours the window boundary, and keeps the old
// generation's overlap deadline.
func TestSnapshotRestoreStillRejectsProcessed(t *testing.T) {
	clk := &fakeClock{t: time.Unix(1_700_000_000, 0)}
	cfg := Config{WindowSize: 8, Overlap: 10 * time.Second, Now: clk.now}
	f := NewFilter(cfg)
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	for _, seq := range []uint64{5, 6, 7, 8, 12} {
		if !f.Accept("sa1", 1, seq) {
			t.Fatalf("seq %d rejected before snapshot", seq)
		}
	}
	if err := f.Rotate("sa1", 2); err != nil {
		t.Fatalf("Rotate: %v", err)
	}
	if !f.Accept("sa1", 2, 1) {
		t.Fatal("new gen seq 1 rejected before snapshot")
	}

	snap := f.Snapshot()
	restored, err := Restore(cfg, snap)
	if err != nil {
		t.Fatalf("Restore: %v", err)
	}

	// Duplicates of pre-snapshot packets are still rejected.
	for _, seq := range []uint64{5, 6, 7, 8, 12} {
		if restored.Accept("sa1", 1, seq) {
			t.Fatalf("restored filter accepted already-processed seq %d", seq)
		}
		if !restored.Received("sa1", 1, seq) {
			t.Fatalf("restored filter lost record of seq %d", seq)
		}
	}
	// Window boundary survived the reload: highest is 12 with size 8, so
	// the window covers 5..12; 9 is unseen inside it, 4 is behind it.
	if !restored.Accept("sa1", 1, 9) {
		t.Fatal("restored filter rejected in-window unseen seq 9")
	}
	if restored.Accept("sa1", 1, 4) {
		t.Fatal("restored filter accepted seq behind the window")
	}
	// New generation state survived too.
	if restored.Accept("sa1", 2, 1) {
		t.Fatal("restored filter accepted duplicate in new generation")
	}
	if !restored.Accept("sa1", 2, 2) {
		t.Fatal("restored filter rejected fresh seq in new generation")
	}
	// The old generation's overlap deadline is still enforced.
	clk.advance(11 * time.Second)
	if restored.Accept("sa1", 1, 10) {
		t.Fatal("restored filter accepted old gen packet after overlap expired")
	}
	if !restored.Accept("sa1", 2, 3) {
		t.Fatal("restored filter rejected new gen after old gen expiry")
	}
}

// A snapshot is a deep copy: later traffic on the original filter must
// not leak into a filter restored from the earlier snapshot.
func TestSnapshotIsDeepCopy(t *testing.T) {
	f := newTestFilter(t, 8, 0, nil)
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	if !f.Accept("sa1", 1, 1) {
		t.Fatal("seq 1 rejected")
	}
	snap := f.Snapshot()
	if !f.Accept("sa1", 1, 2) {
		t.Fatal("seq 2 rejected")
	}
	restored, err := Restore(Config{WindowSize: 8}, snap)
	if err != nil {
		t.Fatalf("Restore: %v", err)
	}
	if !restored.Accept("sa1", 1, 2) {
		t.Fatal("restored filter sees traffic that post-dates the snapshot")
	}
	if restored.Accept("sa1", 1, 1) {
		t.Fatal("restored filter accepted pre-snapshot duplicate")
	}
}

// Restoring with a mismatched window size is refused.
func TestRestoreWindowSizeMismatch(t *testing.T) {
	f := newTestFilter(t, 8, 0, nil)
	if err := f.AddSA("sa1", 1); err != nil {
		t.Fatalf("AddSA: %v", err)
	}
	snap := f.Snapshot()
	if _, err := Restore(Config{WindowSize: 16}, snap); err != ErrWindowSizeMismatch {
		t.Fatalf("Restore with mismatched window size: got %v", err)
	}
}
