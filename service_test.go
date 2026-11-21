package coordination

import (
	"errors"
	"testing"
)

const lockA = "/locks/job-a"
const lockB = "/locks/job-b"

func mustSession(t *testing.T, svc *Service, id string) {
	t.Helper()
	if err := svc.OpenSession(id); err != nil {
		t.Fatalf("OpenSession(%q): %v", id, err)
	}
}

func mustEnqueue(t *testing.T, c *Contender) Node {
	t.Helper()
	if err := c.Enqueue(); err != nil {
		t.Fatalf("enqueue %s: %v", c.SessionID(), err)
	}
	return c.Node()
}

// TestNormalQueueGrantOrder is the happy path: contenders enqueue in order,
// each one watches only its direct predecessor, and the lock passes in sequence.
func TestNormalQueueGrantOrder(t *testing.T) {
	svc := NewService()
	for _, id := range []string{"A", "B", "C"} {
		mustSession(t, svc, id)
	}
	a := NewContender(svc, "A", lockA)
	b := NewContender(svc, "B", lockA)
	c := NewContender(svc, "C", lockA)

	na := mustEnqueue(t, a)
	nb := mustEnqueue(t, b)
	nc := mustEnqueue(t, c)

	if got := svc.Children(lockA); len(got) != 3 ||
		got[0].Path != na.Path || got[1].Path != nb.Path || got[2].Path != nc.Path {
		t.Fatalf("children order wrong: %+v", got)
	}

	if phase, err := a.Run(10); err != nil || phase != PhaseHeld {
		t.Fatalf("A: phase=%v err=%v, want held", phase, err)
	}
	if phase, err := b.Run(10); err != nil || phase != PhaseWatching {
		t.Fatalf("B: phase=%v err=%v, want watching", phase, err)
	}
	if phase, err := c.Run(10); err != nil || phase != PhaseWatching {
		t.Fatalf("C: phase=%v err=%v, want watching", phase, err)
	}
	if b.Predecessor() != na.Path {
		t.Fatalf("B watches %q, want %q", b.Predecessor(), na.Path)
	}
	if c.Predecessor() != nb.Path {
		t.Fatalf("C watches %q, want %q", c.Predecessor(), nb.Path)
	}
	if svc.WatcherCount(na.Path) != 1 || svc.WatcherCount(nb.Path) != 1 {
		t.Fatalf("watcher counts: A=%d B=%d, want 1/1",
			svc.WatcherCount(na.Path), svc.WatcherCount(nb.Path))
	}

	if !a.Held() || b.Held() || c.Held() {
		t.Fatalf("held flags: A=%v B=%v C=%v", a.Held(), b.Held(), c.Held())
	}

	// A leaves: only B may wake, C must stay asleep.
	if err := a.Release(); err != nil {
		t.Fatalf("A release: %v", err)
	}
	if b.WakeCount() != 1 || c.WakeCount() != 0 {
		t.Fatalf("after A release wakes B=%d C=%d, want 1/0", b.WakeCount(), c.WakeCount())
	}
	if phase, err := b.Step(); err != nil || phase != PhaseWoken {
		t.Fatalf("B step after wake: %v %v", phase, err)
	}
	if phase, err := b.Run(10); err != nil || phase != PhaseHeld {
		t.Fatalf("B acquire: %v %v", phase, err)
	}
	if c.WakeCount() != 0 {
		t.Fatalf("C woke early: %d", c.WakeCount())
	}
	if b.Fence() <= na.Sequence {
		t.Fatalf("B fence %d not greater than A fence %d", b.Fence(), na.Sequence)
	}

	// B leaves: C wakes and acquires.
	if err := b.Release(); err != nil {
		t.Fatalf("B release: %v", err)
	}
	if c.WakeCount() != 1 {
		t.Fatalf("C wakes=%d, want 1", c.WakeCount())
	}
	if phase, err := c.Run(10); err != nil || phase != PhaseHeld {
		t.Fatalf("C acquire: %v %v", phase, err)
	}

	// Queue is empty once C releases.
	if err := c.Release(); err != nil {
		t.Fatalf("C release: %v", err)
	}
	if got := svc.Children(lockA); len(got) != 0 {
		t.Fatalf("queue not empty: %+v", got)
	}
}

// TestHeadReleaseWakesOnlyRegisteredWatcher checks the fan-out rule directly:
// releasing a node notifies exactly the watchers armed on that node.
func TestHeadReleaseWakesOnlyRegisteredWatcher(t *testing.T) {
	svc := NewService()
	for _, id := range []string{"A", "B", "C", "D"} {
		mustSession(t, svc, id)
	}
	a := NewContender(svc, "A", lockA)
	b := NewContender(svc, "B", lockA)
	c := NewContender(svc, "C", lockA)
	d := NewContender(svc, "D", lockA)

	for _, x := range []*Contender{a, b, c, d} {
		mustEnqueue(t, x)
	}
	for _, x := range []*Contender{a, b, c, d} {
		if _, err := x.Run(10); err != nil {
			t.Fatalf("%s run: %v", x.SessionID(), err)
		}
	}
	if !a.Held() || b.Held() || c.Held() || d.Held() {
		t.Fatalf("unexpected held state")
	}
	if c.Predecessor() != b.NodePath() || d.Predecessor() != c.NodePath() {
		t.Fatalf("predecessor chain broken: C=%q D=%q", c.Predecessor(), d.Predecessor())
	}

	// A owns the head node. Its expiry removes that node and must wake only B,
	// the single contender that armed a watch on it; C and D stay asleep.
	if _, err := svc.ExpireSession("A"); err != nil {
		t.Fatalf("expire A: %v", err)
	}
	if b.WakeCount() != 1 || c.WakeCount() != 0 || d.WakeCount() != 0 {
		t.Fatalf("after A expiry: B=%d C=%d D=%d, want 1/0/0",
			b.WakeCount(), c.WakeCount(), d.WakeCount())
	}
	if svc.WatcherCount(c.NodePath()) != 1 {
		t.Fatalf("D's watch on C was disturbed")
	}
}

// TestEnumerationWatchRacePredecessorGone covers the stated race: after the
// queue is listed but before the watch is installed, the predecessor expires.
// Re-enumeration must reveal that the contender is now first and allow it to
// take the lock directly, without watching a dead node.
func TestEnumerationWatchRacePredecessorGone(t *testing.T) {
	svc := NewService()
	mustSession(t, svc, "A")
	mustSession(t, svc, "B")
	a := NewContender(svc, "A", lockA)
	b := NewContender(svc, "B", lockA)

	mustEnqueue(t, a)
	nb := mustEnqueue(t, b)
	if ok, err := a.TryAcquireLock(); err != nil || !ok {
		t.Fatalf("A acquire: %v %v", ok, err)
	}

	// B enumerates and records A as predecessor...
	if err := b.Enumerate(); err != nil {
		t.Fatalf("B enumerate: %v", err)
	}
	if b.Predecessor() != a.NodePath() {
		t.Fatalf("B predecessor %q", b.Predecessor())
	}
	// ...then A's session expires before B installs its watch.
	if _, err := svc.ExpireSession("A"); err != nil {
		t.Fatalf("expire A: %v", err)
	}
	status, err := b.InstallWatch()
	if err != nil {
		t.Fatalf("B install watch: %v", err)
	}
	if status != WatchNodeMissing {
		t.Fatalf("install status = %v, want WatchNodeMissing", status)
	}
	if b.Phase() != PhaseWoken || b.RaceRecoveries() != 1 {
		t.Fatalf("phase=%v recoveries=%d, want woken/1", b.Phase(), b.RaceRecoveries())
	}
	if svc.WatcherCount(a.NodePath()) != 0 {
		t.Fatalf("watch armed on removed node")
	}

	// Re-enumerate: B is now the smallest node.
	if err := b.Enumerate(); err != nil {
		t.Fatalf("B re-enumerate: %v", err)
	}
	if b.Predecessor() != "" {
		t.Fatalf("B predecessor after re-enumerate = %q, want empty", b.Predecessor())
	}
	ok, err := b.TryAcquireLock()
	if err != nil || !ok {
		t.Fatalf("B acquire after race: %v %v", ok, err)
	}
	if !b.Held() || b.Fence() != nb.Sequence {
		t.Fatalf("B held=%v fence=%d", b.Held(), b.Fence())
	}
}

// TestEnumerationWatchRaceReenumerateToNewPredecessor covers the other branch of
// the race: when the vanished predecessor is not the head, re-enumeration must
// pick the new direct predecessor and arm a fresh watch on it.
func TestEnumerationWatchRaceReenumerateToNewPredecessor(t *testing.T) {
	svc := NewService()
	for _, id := range []string{"A", "B", "C"} {
		mustSession(t, svc, id)
	}
	a := NewContender(svc, "A", lockA)
	b := NewContender(svc, "B", lockA)
	c := NewContender(svc, "C", lockA)

	mustEnqueue(t, a)
	_ = mustEnqueue(t, b)
	mustEnqueue(t, c)
	if ok, _ := a.TryAcquireLock(); !ok {
		t.Fatalf("A should hold")
	}

	if err := c.Enumerate(); err != nil {
		t.Fatalf("C enumerate: %v", err)
	}
	if c.Predecessor() != b.NodePath() {
		t.Fatalf("C predecessor = %q, want B", c.Predecessor())
	}
	// B (a queued non-head) disappears between enumerate and install.
	if _, err := svc.ExpireSession("B"); err != nil {
		t.Fatalf("expire B: %v", err)
	}
	status, err := c.InstallWatch()
	if err != nil || status != WatchNodeMissing {
		t.Fatalf("C install: status=%v err=%v", status, err)
	}
	if c.RaceRecoveries() != 1 {
		t.Fatalf("recoveries=%d", c.RaceRecoveries())
	}

	if err := c.Enumerate(); err != nil {
		t.Fatalf("C re-enumerate: %v", err)
	}
	if c.Predecessor() != a.NodePath() {
		t.Fatalf("C predecessor = %q, want A", c.Predecessor())
	}
	status, err = c.InstallWatch()
	if err != nil || status != WatchInstalled {
		t.Fatalf("C install fresh: status=%v err=%v", status, err)
	}
	if svc.WatcherCount(a.NodePath()) != 1 {
		t.Fatalf("fresh watch not armed on A")
	}

	// When A leaves, C is roused.
	if err := a.Release(); err != nil {
		t.Fatalf("A release: %v", err)
	}
	if c.WakeCount() != 1 {
		t.Fatalf("C wakes=%d, want 1", c.WakeCount())
	}
	if phase, err := c.Run(10); err != nil || phase != PhaseHeld {
		t.Fatalf("C acquire: %v %v", phase, err)
	}
}

// TestSessionExpiryRemovesAllNodesAndWatches checks that expiry is all-or-nothing
// across multiple locks and that watches owned by the expiring session vanish.
func TestSessionExpiryRemovesAllNodesAndWatches(t *testing.T) {
	svc := NewService()
	for _, id := range []string{"A", "B"} {
		mustSession(t, svc, id)
	}
	a := NewContender(svc, "A", lockA)
	a2 := NewContender(svc, "A", lockB)
	b := NewContender(svc, "B", lockA)

	mustEnqueue(t, a)
	mustEnqueue(t, a2)
	mustEnqueue(t, b)
	if ok, _ := a.TryAcquireLock(); !ok {
		t.Fatalf("A should hold lockA")
	}
	if ok, _ := a2.TryAcquireLock(); !ok {
		t.Fatalf("A should hold lockB")
	}
	if _, err := b.Run(10); err != nil {
		t.Fatalf("B run: %v", err)
	}
	// B arms a watch on A. A also arms a stray watch on itself, which must be
	// removed with the session rather than firing.
	if _, status, err := svc.WatchNode(b.NodePath(), "A", nil); err != nil || status != WatchInstalled {
		t.Fatalf("A stray watch: %v %v", status, err)
	}
	if svc.SessionWatcherCount("A") != 1 {
		t.Fatalf("A watch count = %d", svc.SessionWatcherCount("A"))
	}

	removed, err := svc.ExpireSession("A")
	if err != nil {
		t.Fatalf("expire A: %v", err)
	}
	if len(removed) != 2 || removed[0] != a.NodePath() || removed[1] != a2.NodePath() {
		t.Fatalf("removed paths = %v", removed)
	}
	if svc.SessionExists("A") {
		t.Fatalf("A still exists")
	}
	if paths := svc.SessionNodePaths("A"); len(paths) != 0 {
		t.Fatalf("A nodes remain: %v", paths)
	}
	if svc.SessionWatcherCount("A") != 0 {
		t.Fatalf("A watches remain: %d", svc.SessionWatcherCount("A"))
	}
	// B watched A's node in lockA, so B wakes exactly once.
	if b.WakeCount() != 1 {
		t.Fatalf("B wakes=%d, want 1", b.WakeCount())
	}
	if svc.WatcherCount(a.NodePath()) != 0 {
		t.Fatalf("watchers remain on removed node")
	}
	// B did not watch A's lockB node, so the stray watch never fired (it belonged
	// to A and died with A).
	if svc.WatcherCount(b.NodePath()) != 0 {
		t.Fatalf("stray watch outlived its session")
	}
	if phase, err := b.Run(10); err != nil || phase != PhaseHeld {
		t.Fatalf("B acquire after expiry: %v %v", phase, err)
	}

	// Expiring again is an error, not a silent no-op.
	if _, err := svc.ExpireSession("A"); !errors.Is(err, ErrSessionExpired) {
		t.Fatalf("second expire err = %v", err)
	}
	// A stale holder can no longer act on the service.
	if err := a.Release(); !errors.Is(err, ErrSessionExpired) && !errors.Is(err, ErrNodeNotFound) {
		t.Fatalf("stale release err = %v", err)
	}
}

// TestFencingRejectsStaleHolderAfterNewFirstWrite is the core fencing scenario:
// the old holder's session expired but the old process has not noticed. Once the
// new holder writes, the old holder's late write is rejected and data is intact.
func TestFencingRejectsStaleHolderAfterNewFirstWrite(t *testing.T) {
	svc := NewService()
	res := NewResource()
	mustSession(t, svc, "A")
	mustSession(t, svc, "B")

	a := NewContender(svc, "A", lockA)
	b := NewContender(svc, "B", lockA)
	mustEnqueue(t, a)
	mustEnqueue(t, b)
	if phase, err := a.Run(10); err != nil || phase != PhaseHeld {
		t.Fatalf("A acquire: %v %v", phase, err)
	}
	if err := a.TakeOver(res, "a-1"); err != nil {
		t.Fatalf("A takeover: %v", err)
	}
	if res.Fence() != a.Fence() || res.Data() != "a-1" {
		t.Fatalf("resource after A: fence=%d data=%q", res.Fence(), res.Data())
	}

	// A's session expires while the A process still believes it holds the lock.
	if _, err := svc.ExpireSession("A"); err != nil {
		t.Fatalf("expire A: %v", err)
	}
	if !a.Held() {
		// Held() reflects the cached belief; it is allowed to stay true until A
		// verifies. Model the lagging process by requiring the stale belief here.
		t.Fatalf("expected A to still believe it holds the lock before verification")
	}

	// B wakes and takes over with a strictly larger fencing token.
	if phase, err := b.Run(10); err != nil || phase != PhaseHeld {
		t.Fatalf("B acquire: %v %v", phase, err)
	}
	if b.Fence() <= a.Fence() {
		t.Fatalf("B fence %d <= A fence %d", b.Fence(), a.Fence())
	}
	if err := b.TakeOver(res, "b-1"); err != nil {
		t.Fatalf("B takeover: %v", err)
	}
	if res.Fence() != b.Fence() || res.Data() != "b-1" {
		t.Fatalf("resource after B: fence=%d data=%q", res.Fence(), res.Data())
	}
	before := res.Version()

	// A finally wakes up and attempts a write with its old token. It must be
	// rejected and must not change the payload or version.
	if err := a.WriteToResource(res, "a-late"); !errors.Is(err, ErrStaleHolder) {
		t.Fatalf("stale A write err = %v, want ErrStaleHolder", err)
	}
	if res.Data() != "b-1" || res.Version() != before {
		t.Fatalf("resource mutated by stale write: data=%q version=%d", res.Data(), res.Version())
	}
	if res.Fence() != b.Fence() {
		t.Fatalf("fence moved backwards to %d", res.Fence())
	}
	// The rejection is recorded, and so is the accepted write.
	hist := res.History()
	if len(hist) != 3 {
		t.Fatalf("history len = %d: %+v", len(hist), hist)
	}
	if hist[2].Accepted || hist[2].SessionID != "A" || hist[2].Token != a.Fence() {
		t.Fatalf("stale attempt record wrong: %+v", hist[2])
	}

	// A's cached Held() is stale until A checks with the service; a verification
	// step flips it to lost.
	if phase, err := a.Step(); !errors.Is(err, ErrSessionExpired) || phase != PhaseLost {
		t.Fatalf("A verify: phase=%v err=%v", phase, err)
	}
	if a.Held() {
		t.Fatalf("A still held after verification")
	}
}

// TestFenceAcceptanceBoundaries covers token edge cases: zero is invalid, equal
// tokens are accepted (idempotent re-write), and Acquire rejects a token below
// the barrier.
func TestFenceAcceptanceBoundaries(t *testing.T) {
	res := NewResource()
	if err := res.Write("X", 0, "z"); !errors.Is(err, ErrInvalidFence) {
		t.Fatalf("zero token err = %v", err)
	}
	if err := res.Acquire("X", 0); !errors.Is(err, ErrInvalidFence) {
		t.Fatalf("zero acquire err = %v", err)
	}
	if err := res.Acquire("A", 5); err != nil {
		t.Fatalf("acquire 5: %v", err)
	}
	if err := res.Write("A", 5, "first"); err != nil {
		t.Fatalf("write 5: %v", err)
	}
	if res.Fence() != 5 {
		t.Fatalf("fence = %d, want 5", res.Fence())
	}
	// Same holder, same token: accepted.
	if err := res.Write("A", 5, "second"); err != nil {
		t.Fatalf("same-token write: %v", err)
	}
	if res.Data() != "second" || res.Version() != 2 {
		t.Fatalf("data=%q version=%d", res.Data(), res.Version())
	}
	// Lower token rejected.
	if err := res.Write("B", 4, "low"); !errors.Is(err, ErrStaleHolder) {
		t.Fatalf("low write err = %v", err)
	}
	// Acquiring with a superseded token is rejected.
	if err := res.Acquire("B", 4); !errors.Is(err, ErrStaleHolder) {
		t.Fatalf("low acquire err = %v", err)
	}
	if res.Data() != "second" || res.Version() != 2 || res.Fence() != 5 {
		t.Fatalf("state changed: %+v", res)
	}
}

// TestFenceBarrierMovesOnFirstWrite pins the exact rule "reject a smaller
// sequence after the resource has received the new holder's first write": a
// registered but not-yet-writing successor does not raise the barrier.
func TestFenceBarrierMovesOnFirstWrite(t *testing.T) {
	res := NewResource()
	if err := res.Write("old", 1, "old-1"); err != nil {
		t.Fatalf("old first write: %v", err)
	}
	// New holder is elected and registers, but has not written yet.
	if err := res.Acquire("new", 2); err != nil {
		t.Fatalf("new acquire: %v", err)
	}
	if res.Fence() != 1 {
		t.Fatalf("acquire alone raised the barrier to %d", res.Fence())
	}
	// The old holder is still within the barrier, so this late write lands. This
	// documents the window before the successor's first write.
	if err := res.Write("old", 1, "old-late"); err != nil {
		t.Fatalf("pre-barrier write should be accepted: %v", err)
	}
	// New holder's first write moves the barrier.
	if err := res.Write("new", 2, "new-1"); err != nil {
		t.Fatalf("new first write: %v", err)
	}
	if res.Fence() != 2 {
		t.Fatalf("fence = %d, want 2", res.Fence())
	}
	// Now the old holder is fenced out.
	if err := res.Write("old", 1, "old-too-late"); !errors.Is(err, ErrStaleHolder) {
		t.Fatalf("post-barrier old write err = %v", err)
	}
	if res.Data() != "new-1" || res.Fence() != 2 {
		t.Fatalf("state after rejection: data=%q fence=%d", res.Data(), res.Fence())
	}
}

// TestSequenceNumbersPerLock checks that each lock's queue has its own strictly
// increasing sequence and that names sort in sequence order.
func TestSequenceNumbersPerLock(t *testing.T) {
	svc := NewService()
	mustSession(t, svc, "A")
	mustSession(t, svc, "B")

	a1 := mustEnqueue(t, NewContender(svc, "A", lockA))
	a2 := mustEnqueue(t, NewContender(svc, "B", lockA))
	b1 := mustEnqueue(t, NewContender(svc, "A", lockB))

	if a1.Sequence != 1 || a2.Sequence != 2 {
		t.Fatalf("lockA sequences = %d,%d", a1.Sequence, a2.Sequence)
	}
	if b1.Sequence != 1 {
		t.Fatalf("lockB sequence = %d, want 1", b1.Sequence)
	}
	if a1.Path >= a2.Path {
		t.Fatalf("paths not ordered: %q %q", a1.Path, a2.Path)
	}
	// A new node after one is deleted continues to increase.
	if err := svc.DeleteNode(a2.Path, "B"); err != nil {
		t.Fatalf("delete a2: %v", err)
	}
	a3 := mustEnqueue(t, NewContender(svc, "A", lockA))
	if a3.Sequence != 3 {
		t.Fatalf("sequence after delete = %d, want 3", a3.Sequence)
	}
}

// TestFirstContenderAcquiresWithoutPredecessor covers the boundary of an empty
// queue: enumeration yields no predecessor and the watch phase is skipped.
func TestFirstContenderAcquiresWithoutPredecessor(t *testing.T) {
	svc := NewService()
	mustSession(t, svc, "A")
	a := NewContender(svc, "A", lockA)
	mustEnqueue(t, a)

	if err := a.Enumerate(); err != nil {
		t.Fatalf("enumerate: %v", err)
	}
	if a.Predecessor() != "" {
		t.Fatalf("predecessor = %q, want empty", a.Predecessor())
	}
	if _, err := a.InstallWatch(); !errors.Is(err, ErrNoPredecessor) {
		t.Fatalf("install watch err = %v, want ErrNoPredecessor", err)
	}
	if phase, err := a.Step(); err != nil || phase != PhaseHeld {
		t.Fatalf("step: %v %v", phase, err)
	}
	if !a.Held() {
		t.Fatalf("first contender not held")
	}
}

// TestReleaseWakesOnlyWatchersOnReleasedNode is a boundary check for the
// fan-out rule with a non-head queued node: deleting it notifies its own
// watcher and nobody else.
func TestReleaseWakesOnlyWatchersOnReleasedNode(t *testing.T) {
	svc := NewService()
	for _, id := range []string{"A", "B", "C"} {
		mustSession(t, svc, id)
	}
	a := NewContender(svc, "A", lockA)
	b := NewContender(svc, "B", lockA)
	c := NewContender(svc, "C", lockA)
	mustEnqueue(t, a)
	mustEnqueue(t, b)
	mustEnqueue(t, c)
	for _, x := range []*Contender{a, b, c} {
		if _, err := x.Run(10); err != nil {
			t.Fatalf("run %s: %v", x.SessionID(), err)
		}
	}
	// B gives up its queued slot: only C, its direct watcher, wakes.
	if err := b.Release(); err != nil {
		t.Fatalf("B release: %v", err)
	}
	if a.WakeCount() != 0 || c.WakeCount() != 1 {
		t.Fatalf("wakes A=%d C=%d, want 0/1", a.WakeCount(), c.WakeCount())
	}
	if phase, err := c.Run(10); err != nil || phase != PhaseWatching {
		t.Fatalf("C after B release: %v %v", phase, err)
	}
	if c.Predecessor() != a.NodePath() {
		t.Fatalf("C predecessor = %q, want A", c.Predecessor())
	}
	// A still holds throughout.
	if !a.Held() || c.Held() {
		t.Fatalf("held flags A=%v C=%v", a.Held(), c.Held())
	}
}

// TestWatchAndSessionErrors covers error surfaces used by callers.
func TestWatchAndSessionErrors(t *testing.T) {
	svc := NewService()
	if err := svc.OpenSession(""); !errors.Is(err, ErrEmptySession) {
		t.Fatalf("empty session err = %v", err)
	}
	mustSession(t, svc, "A")
	if err := svc.OpenSession("A"); !errors.Is(err, ErrSessionExists) {
		t.Fatalf("dup session err = %v", err)
	}
	if _, err := svc.CreateEphemeralSequential(lockA, "ghost", ""); !errors.Is(err, ErrSessionUnknown) {
		t.Fatalf("unknown session create err = %v", err)
	}
	a := NewContender(svc, "A", lockA)
	mustEnqueue(t, a)
	// A missing predecessor is not an error; it reports WatchNodeMissing so the
	// caller knows to re-enumerate.
	if id, status, err := svc.WatchPredecessor(a.NodePath(), "/locks/missing", "A", nil); err != nil || status != WatchNodeMissing || id != 0 {
		t.Fatalf("watch missing predecessor: id=%d status=%v err=%v", id, status, err)
	}
	// A missing self node is an error.
	if _, _, err := svc.WatchPredecessor("/locks/missing", a.NodePath(), "A", nil); !errors.Is(err, ErrNodeNotFound) {
		t.Fatalf("watch with bad self err = %v", err)
	}
	if err := svc.DeleteNode(a.NodePath(), "ghost"); !errors.Is(err, ErrSessionUnknown) {
		t.Fatalf("delete with unknown session err = %v", err)
	}
	if err := svc.DeleteNode("/locks/nope", "A"); !errors.Is(err, ErrNodeNotFound) {
		t.Fatalf("delete missing err = %v", err)
	}
}
