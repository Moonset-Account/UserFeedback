package pimutex

import (
	"errors"
	"sync"
	"testing"
)

// helpers -----------------------------------------------------------------

func newModel(t *testing.T, threads map[ThreadID]int, locks []LockID) *Model {
	t.Helper()
	m := New()
	for id, base := range threads {
		if err := m.AddThread(id, base); err != nil {
			t.Fatalf("AddThread(%q, %d): %v", id, base, err)
		}
	}
	for _, id := range locks {
		if err := m.AddLock(id); err != nil {
			t.Fatalf("AddLock(%q): %v", id, err)
		}
	}
	return m
}

func mustEff(t *testing.T, m *Model, id ThreadID) int {
	t.Helper()
	got, err := m.EffectivePriority(id)
	if err != nil {
		t.Fatalf("EffectivePriority(%q): %v", id, err)
	}
	return got
}

func mustEffMap(t *testing.T, m *Model) map[ThreadID]int {
	t.Helper()
	return m.EffectivePriorities()
}

func assertEff(t *testing.T, m *Model, want map[ThreadID]int) {
	t.Helper()
	got := m.EffectivePriorities()
	for id, w := range want {
		if got[id] != w {
			t.Errorf("effective priority of %q = %d, want %d (all: %v)", id, got[id], w, got)
		}
	}
	if len(got) != len(want) {
		t.Errorf("EffectivePriorities returned %d entries, want %d: %v", len(got), len(want), got)
	}
}

func assertOwner(t *testing.T, m *Model, lockID LockID, wantOwner ThreadID, wantHeld bool) {
	t.Helper()
	owner, held, err := m.Owner(lockID)
	if err != nil {
		t.Fatalf("Owner(%q): %v", lockID, err)
	}
	if held != wantHeld || owner != wantOwner {
		t.Errorf("Owner(%q) = (%q, %v), want (%q, %v)", lockID, owner, held, wantOwner, wantHeld)
	}
}

func mustAcquire(t *testing.T, m *Model, id ThreadID, lockID LockID, want AcquireOutcome) {
	t.Helper()
	got, err := m.Acquire(id, lockID)
	if err != nil {
		t.Fatalf("Acquire(%q, %q): %v", id, lockID, err)
	}
	if got != want {
		t.Fatalf("Acquire(%q, %q) = %v, want %v", id, lockID, got, want)
	}
}

func mustRelease(t *testing.T, m *Model, id ThreadID, lockID LockID) {
	t.Helper()
	if err := m.Release(id, lockID); err != nil {
		t.Fatalf("Release(%q, %q): %v", id, lockID, err)
	}
}

func mustCancel(t *testing.T, m *Model, id ThreadID, wantLock LockID) {
	t.Helper()
	got, err := m.CancelWait(id)
	if err != nil {
		t.Fatalf("CancelWait(%q): %v", id, err)
	}
	if got != wantLock {
		t.Fatalf("CancelWait(%q) returned %q, want %q", id, got, wantLock)
	}
}

// scenarios ---------------------------------------------------------------

// TestNormalAcquireRelease covers the uncontended path and a plain handoff:
// a free lock is acquired, then released to a single waiter.
func TestNormalAcquireRelease(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"A": 5, "B": 3},
		[]LockID{"L1"},
	)

	assertOwner(t, m, "L1", "", false)
	mustAcquire(t, m, "A", "L1", Acquired)
	assertOwner(t, m, "L1", "A", true)
	if locks, err := m.HeldLocks("A"); err != nil || len(locks) != 1 || locks[0] != "L1" {
		t.Fatalf("HeldLocks(A) = %v, %v; want [L1]", locks, err)
	}

	mustAcquire(t, m, "B", "L1", Waiting)
	assertOwner(t, m, "L1", "A", true)
	if on, waiting, err := m.WaitingOn("B"); err != nil || !waiting || on != "L1" {
		t.Fatalf("WaitingOn(B) = (%q, %v, %v); want (L1, true, nil)", on, waiting, err)
	}

	mustRelease(t, m, "A", "L1")
	assertOwner(t, m, "L1", "B", true)
	if _, waiting, _ := m.WaitingOn("B"); waiting {
		t.Fatalf("B should no longer be waiting after handoff")
	}
	if locks, _ := m.HeldLocks("A"); len(locks) != 0 {
		t.Fatalf("A should hold no locks after release, got %v", locks)
	}

	// Releasing the last lock leaves it free.
	mustRelease(t, m, "B", "L1")
	assertOwner(t, m, "L1", "", false)
	if owners := m.Owners(); len(owners) != 0 {
		t.Fatalf("Owners() = %v, want empty", owners)
	}
}

// TestInheritanceChain covers the scenario from the prompt: L holds a lock
// and waits on M's lock while high-priority H waits on L's lock, so H's
// priority must propagate L -> M.
func TestInheritanceChain(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"L": 3, "M": 2, "H": 10},
		[]LockID{"A", "B"},
	)

	mustAcquire(t, m, "M", "B", Acquired) // M owns B
	mustAcquire(t, m, "L", "A", Acquired) // L owns A
	mustAcquire(t, m, "L", "B", Waiting)  // L waits on M's B

	assertEff(t, m, map[ThreadID]int{"L": 3, "M": 3, "H": 10})

	mustAcquire(t, m, "H", "A", Waiting) // H waits on L's A
	assertEff(t, m, map[ThreadID]int{"L": 10, "M": 10, "H": 10})

	// Priority propagates back down the chain when the source goes away.
	mustCancel(t, m, "H", "A")
	assertEff(t, m, map[ThreadID]int{"L": 3, "M": 3, "H": 10})

	// L is still queued on M's lock B; cancelling H only removed H's edge.
	if on, waiting, _ := m.WaitingOn("L"); !waiting || on != "B" {
		t.Fatalf("L wait state = (%q, %v), want (B, true)", on, waiting)
	}
	// Releasing B hands it to L, which no longer boosts M.
	mustRelease(t, m, "M", "B")
	assertOwner(t, m, "B", "L", true)
	assertEff(t, m, map[ThreadID]int{"L": 3, "M": 2, "H": 10})
}

// TestCancelFallsBackToSecondHighest is the interleaving called out in the
// prompt: after H cancels, L is still boosted by a second waiter on another
// lock it holds, and M must fall back to exactly that value rather than M's
// base priority or H's old priority.
func TestCancelFallsBackToSecondHighest(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"L": 3, "M": 2, "H": 10, "H2": 8},
		[]LockID{"A", "C", "B"},
	)

	mustAcquire(t, m, "M", "B", Acquired)
	mustAcquire(t, m, "L", "A", Acquired)
	mustAcquire(t, m, "L", "C", Acquired)
	mustAcquire(t, m, "L", "B", Waiting) // L waits on M
	mustAcquire(t, m, "H2", "C", Waiting)
	mustAcquire(t, m, "H", "A", Waiting)

	// H (10) outranks H2 (8), and both wait on locks L holds.
	assertEff(t, m, map[ThreadID]int{"L": 10, "M": 10, "H": 10, "H2": 8})

	// H cancels: L keeps the boost from H2 on the *other* held lock.
	mustCancel(t, m, "H", "A")
	assertEff(t, m, map[ThreadID]int{"L": 8, "M": 8, "H": 10, "H2": 8})

	// H2 leaves too: the chain relaxes all the way to base priorities.
	mustCancel(t, m, "H2", "C")
	assertEff(t, m, map[ThreadID]int{"L": 3, "M": 3, "H": 10, "H2": 8})

	// M still owns B and L is still queued there.
	assertOwner(t, m, "B", "M", true)
	if waiters, err := m.Waiters("B"); err != nil || len(waiters) != 1 || waiters[0] != "L" {
		t.Fatalf("Waiters(B) = %v, %v; want [L]", waiters, err)
	}
}

// TestHandoffPriorityOrderAndTieBreak verifies that release hands the lock to
// the highest effective-priority waiter and, on equal priority, to the
// earliest waiter in queue order.
func TestHandoffPriorityOrderAndTieBreak(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"O": 1, "T1": 5, "T2": 5, "T3": 7},
		[]LockID{"X"},
	)

	mustAcquire(t, m, "O", "X", Acquired)
	mustAcquire(t, m, "T1", "X", Waiting)
	mustAcquire(t, m, "T2", "X", Waiting)
	mustAcquire(t, m, "T3", "X", Waiting)

	mustRelease(t, m, "O", "X")
	assertOwner(t, m, "X", "T3", true) // highest priority wins

	if waiters, _ := m.Waiters("X"); len(waiters) != 2 || waiters[0] != "T1" || waiters[1] != "T2" {
		t.Fatalf("Waiters(X) after handoff = %v, want [T1 T2]", waiters)
	}

	mustRelease(t, m, "T3", "X")
	assertOwner(t, m, "X", "T1", true) // priority tie -> earliest waiter

	mustRelease(t, m, "T1", "X")
	assertOwner(t, m, "X", "T2", true)
}

// TestInheritedPriorityDrivesHandoff shows that handoff compares *effective*
// priorities, not base priorities: a waiter boosted by its own waiter wins.
func TestInheritedPriorityDrivesHandoff(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"O": 1, "Boosted": 2, "Plain": 6, "Inner": 9},
		[]LockID{"X", "Y"},
	)

	mustAcquire(t, m, "O", "X", Acquired)
	mustAcquire(t, m, "Boosted", "Y", Acquired) // Boosted holds Y
	mustAcquire(t, m, "Boosted", "X", Waiting)  // and waits for X
	mustAcquire(t, m, "Inner", "Y", Waiting)    // Inner boosts Boosted
	mustAcquire(t, m, "Plain", "X", Waiting)

	assertEff(t, m, map[ThreadID]int{"O": 9, "Boosted": 9, "Plain": 6, "Inner": 9})

	mustRelease(t, m, "O", "X")
	assertOwner(t, m, "X", "Boosted", true) // effective 9 beats base 6

	// Boosted now owns both X and Y; releasing X hands it back to Plain.
	mustRelease(t, m, "Boosted", "X")
	assertOwner(t, m, "X", "Plain", true)
}

// TestThreeHopChain makes sure inheritance is transitive across a longer
// chain, and that relaxation unwinds one link at a time.
func TestThreeHopChain(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"H": 20, "L": 5, "M": 4, "N": 3},
		[]LockID{"A", "B", "C"},
	)

	mustAcquire(t, m, "N", "C", Acquired)
	mustAcquire(t, m, "M", "B", Acquired)
	mustAcquire(t, m, "L", "A", Acquired)
	mustAcquire(t, m, "M", "C", Waiting) // M waits on N
	mustAcquire(t, m, "L", "B", Waiting) // L waits on M
	mustAcquire(t, m, "H", "A", Waiting) // H waits on L

	assertEff(t, m, map[ThreadID]int{"H": 20, "L": 20, "M": 20, "N": 20})

	mustCancel(t, m, "M", "C") // M stops waiting on N
	assertEff(t, m, map[ThreadID]int{"H": 20, "L": 20, "M": 20, "N": 3})

	mustCancel(t, m, "H", "A")
	assertEff(t, m, map[ThreadID]int{"H": 20, "L": 5, "M": 5, "N": 3})
}

// TestBasePriorityChangePropagates covers SetBasePriority as another source
// of change, including lowering and raising it mid-chain.
func TestBasePriorityChangePropagates(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"L": 3, "M": 2, "H": 10},
		[]LockID{"A", "B"},
	)
	mustAcquire(t, m, "M", "B", Acquired)
	mustAcquire(t, m, "L", "A", Acquired)
	mustAcquire(t, m, "L", "B", Waiting)
	mustAcquire(t, m, "H", "A", Waiting)
	assertEff(t, m, map[ThreadID]int{"L": 10, "M": 10, "H": 10})

	// Lowering the source's base priority removes the inherited boost.
	if err := m.SetBasePriority("H", 1); err != nil {
		t.Fatalf("SetBasePriority(H, 1): %v", err)
	}
	assertEff(t, m, map[ThreadID]int{"L": 3, "M": 3, "H": 1})

	// Raising a holder's own base priority propagates to its own holder.
	if err := m.SetBasePriority("L", 30); err != nil {
		t.Fatalf("SetBasePriority(L, 30): %v", err)
	}
	assertEff(t, m, map[ThreadID]int{"L": 30, "M": 30, "H": 1})
	if got, _ := m.BasePriority("L"); got != 30 {
		t.Fatalf("BasePriority(L) = %d, want 30", got)
	}

	// Raising the source above everything is reflected immediately.
	if err := m.SetBasePriority("H", 50); err != nil {
		t.Fatalf("SetBasePriority(H, 50): %v", err)
	}
	assertEff(t, m, map[ThreadID]int{"L": 50, "M": 50, "H": 50})

	// Negative priorities are allowed; ordering is unchanged.
	if err := m.SetBasePriority("M", -7); err != nil {
		t.Fatalf("SetBasePriority(M, -7): %v", err)
	}
	if got, _ := m.BasePriority("M"); got != -7 {
		t.Fatalf("BasePriority(M) = %d, want -7", got)
	}
	// M still inherits from L regardless of its own negative base.
	assertEff(t, m, map[ThreadID]int{"L": 50, "M": 50, "H": 50})
}

// TestSnapshotReflectsState checks the aggregate read APIs after operations.
func TestSnapshotReflectsState(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"A": 4, "B": 1},
		[]LockID{"X", "Y"},
	)
	assertEff(t, m, map[ThreadID]int{"A": 4, "B": 1})
	if got := m.EffectivePriorities(); len(got) != 2 {
		t.Fatalf("EffectivePriorities on idle model = %v, want two entries", got)
	}
	if threads := m.Threads(); len(threads) != 2 || threads[0] != "A" || threads[1] != "B" {
		t.Fatalf("Threads() = %v, want [A B]", threads)
	}
	if locks := m.Locks(); len(locks) != 2 || locks[0] != "X" || locks[1] != "Y" {
		t.Fatalf("Locks() = %v, want [X Y]", locks)
	}

	mustAcquire(t, m, "A", "X", Acquired)
	mustAcquire(t, m, "A", "Y", Acquired)
	mustAcquire(t, m, "B", "X", Waiting)

	owners := m.Owners()
	if len(owners) != 2 || owners["X"] != "A" || owners["Y"] != "A" {
		t.Fatalf("Owners() = %v, want map[X:A Y:A]", owners)
	}
	if held, _ := m.HeldLocks("A"); len(held) != 2 || held[0] != "X" || held[1] != "Y" {
		t.Fatalf("HeldLocks(A) = %v, want [X Y]", held)
	}
	if waiters, _ := m.Waiters("X"); len(waiters) != 1 || waiters[0] != "B" {
		t.Fatalf("Waiters(X) = %v, want [B]", waiters)
	}

	// Snapshot must be a copy: mutating it cannot corrupt the model.
	snap := m.EffectivePriorities()
	snap["A"] = 999
	if got := mustEff(t, m, "A"); got != 4 {
		t.Fatalf("model mutated through snapshot: EffectivePriority(A) = %d, want 4", got)
	}
}

// TestErrors covers boundary and misuse cases.
func TestErrors(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"A": 5, "B": 3},
		[]LockID{"X", "Y"},
	)

	tests := []struct {
		name string
		call func() error
		want error
	}{
		{"empty thread id", func() error { return m.AddThread("", 1) }, ErrInvalidID},
		{"duplicate thread", func() error { return m.AddThread("A", 1) }, ErrDuplicateThread},
		{"empty lock id", func() error { return m.AddLock("") }, ErrInvalidID},
		{"duplicate lock", func() error { return m.AddLock("X") }, ErrDuplicateLock},
		{"acquire unknown thread", func() error { _, err := m.Acquire("Z", "X"); return err }, ErrUnknownThread},
		{"acquire unknown lock", func() error { _, err := m.Acquire("A", "Z"); return err }, ErrUnknownLock},
		{"release unknown thread", func() error { return m.Release("Z", "X") }, ErrUnknownThread},
		{"release unknown lock", func() error { return m.Release("A", "Z") }, ErrUnknownLock},
		{"release not owner", func() error { return m.Release("B", "X") }, ErrNotOwner},
		{"release free lock", func() error { return m.Release("A", "Y") }, ErrNotOwner},
		{"cancel not waiting", func() error { _, err := m.CancelWait("A"); return err }, ErrNotWaiting},
		{"cancel unknown thread", func() error { _, err := m.CancelWait("Z"); return err }, ErrUnknownThread},
		{"set unknown thread", func() error { return m.SetBasePriority("Z", 1) }, ErrUnknownThread},
		{"priority unknown thread", func() error { _, err := m.EffectivePriority("Z"); return err }, ErrUnknownThread},
		{"base unknown thread", func() error { _, err := m.BasePriority("Z"); return err }, ErrUnknownThread},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if err := tc.call(); !errors.Is(err, tc.want) {
				t.Fatalf("error = %v, want errors.Is(_, %v)", err, tc.want)
			}
		})
	}

	// Re-acquiring a lock you already own is rejected, and does not disturb
	// ownership.
	mustAcquire(t, m, "A", "X", Acquired)
	if _, err := m.Acquire("A", "X"); !errors.Is(err, ErrAlreadyOwned) {
		t.Fatalf("Acquire owned lock: error = %v, want ErrAlreadyOwned", err)
	}
	assertOwner(t, m, "X", "A", true)

	// A thread may hold several locks but may wait on only one.
	mustAcquire(t, m, "A", "Y", Acquired)
	mustAcquire(t, m, "B", "X", Waiting)
	if _, err := m.Acquire("B", "Y"); !errors.Is(err, ErrThreadWaiting) {
		t.Fatalf("Acquire while waiting: error = %v, want ErrThreadWaiting", err)
	}
	if _, err := m.Acquire("B", "X"); !errors.Is(err, ErrThreadWaiting) {
		t.Fatalf("Acquire while waiting (same lock): error = %v, want ErrThreadWaiting", err)
	}

	// Cancel returns the lock id and leaves the lock's owner untouched.
	mustCancel(t, m, "B", "X")
	assertOwner(t, m, "X", "A", true)
	if err := func() error { _, err := m.CancelWait("B"); return err }(); !errors.Is(err, ErrNotWaiting) {
		t.Fatalf("second CancelWait: error = %v, want ErrNotWaiting", err)
	}
}

// TestDeadlockRejected exercises the defensive cycle check. The problem
// statement guarantees acyclic input, so this only guards against misuse.
func TestDeadlockRejected(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"A": 5, "B": 4},
		[]LockID{"X", "Y"},
	)
	mustAcquire(t, m, "A", "X", Acquired)
	mustAcquire(t, m, "B", "Y", Acquired)
	mustAcquire(t, m, "A", "Y", Waiting) // A waits on B

	if _, err := m.Acquire("B", "X"); !errors.Is(err, ErrWouldDeadlock) {
		t.Fatalf("Acquire creating cycle: error = %v, want ErrWouldDeadlock", err)
	}
	// Rejection left the state intact.
	assertOwner(t, m, "X", "A", true)
	assertOwner(t, m, "Y", "B", true)
	if waiters, _ := m.Waiters("X"); len(waiters) != 0 {
		t.Fatalf("Waiters(X) = %v, want empty after rejected acquire", waiters)
	}
	// The legal release still works.
	mustRelease(t, m, "A", "X")
	assertOwner(t, m, "X", "", false)
}

// TestConcurrentReaders runs readers concurrently with a writer that performs
// a fixed, legal sequence; it exists to check the internal synchronization
// under `go test -race`. Every operation stays on the same lock so the final
// state is deterministic.
func TestConcurrentReaders(t *testing.T) {
	m := newModel(t,
		map[ThreadID]int{"W": 5, "R1": 3, "R2": 4},
		[]LockID{"X"},
	)

	var wg sync.WaitGroup
	stop := make(chan struct{})
	for _, id := range []ThreadID{"R1", "R2"} {
		id := id
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
					_, _ = m.EffectivePriority(id)
					_ = m.EffectivePriorities()
					_, _, _ = m.Owner("X")
					_, _ = m.Waiters("X")
				}
			}
		}()
	}

	// Writer performs a deterministic acquire/handoff cycle.
	mustAcquire(t, m, "W", "X", Acquired)
	mustAcquire(t, m, "R1", "X", Waiting)
	mustAcquire(t, m, "R2", "X", Waiting)
	mustRelease(t, m, "W", "X") // handoff to R2 (base 4 > 3)
	mustRelease(t, m, "R2", "X")
	mustRelease(t, m, "R1", "X")

	close(stop)
	wg.Wait()
	assertOwner(t, m, "X", "", false)
}
