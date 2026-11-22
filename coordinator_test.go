package rcu

import (
	"errors"
	"reflect"
	"strings"
	"testing"
)

// applyAll applies events and fails on the first error.
func applyAll(t *testing.T, c *Coordinator, events ...Event) []Outcome {
	t.Helper()
	out := make([]Outcome, 0, len(events))
	for i, e := range events {
		oc, err := c.Apply(e)
		if err != nil {
			t.Fatalf("Apply(%s) #%d: unexpected error: %v", e, i, err)
		}
		out = append(out, oc)
	}
	return out
}

func mustReport(t *testing.T, c *Coordinator, id string) CallbackReport {
	t.Helper()
	r, ok := c.CallbackReport(id)
	if !ok {
		t.Fatalf("callback %q not registered", id)
	}
	return r
}

// ---------------------------------------------------------------------------
// Normal scenarios
// ---------------------------------------------------------------------------

// TestEmptyWaitSetEffectiveImmediately: a callback registered with no active
// readers becomes releasable at its own registration event.
func TestEmptyWaitSetEffectiveImmediately(t *testing.T) {
	c := New()
	oc := applyAll(t, c, RegisterCallback("cb1", "srcA"))[0]
	if !reflect.DeepEqual(oc.BecameEffective, []string{"cb1"}) {
		t.Fatalf("registration outcome = %v, want [cb1]", oc.BecameEffective)
	}
	r := mustReport(t, c, "cb1")
	if r.EffectiveAt != oc.Seq {
		t.Fatalf("effective at %d, want %d", r.EffectiveAt, oc.Seq)
	}
	if len(r.Waiters) != 0 {
		t.Fatalf("waiters = %v, want none", r.Waiters)
	}
}

// TestBlocksOnCurrentReaderThenReleases: one reader in a section holds the
// callback until it exits; the release pinpoints the exit event's sequence.
func TestBlocksOnCurrentReaderThenReleases(t *testing.T) {
	c := New()
	applyAll(t, c,
		EnterRead("r1"),
		RegisterCallback("cb1", "srcA"),
	)
	r := mustReport(t, c, "cb1")
	if r.Effective() {
		t.Fatal("callback effective while reader inside section")
	}
	if !reflect.DeepEqual(r.Waiters, []string{"r1"}) {
		t.Fatalf("waiters = %v, want [r1]", r.Waiters)
	}

	oc := applyAll(t, c, ExitRead("r1"))[0]
	if !reflect.DeepEqual(oc.BecameEffective, []string{"cb1"}) {
		t.Fatalf("exit released %v, want [cb1]", oc.BecameEffective)
	}
	r = mustReport(t, c, "cb1")
	if r.EffectiveAt != oc.Seq || r.GraceSatisfiedAt != oc.Seq {
		t.Fatalf("effective/grace at %d/%d, want %d/%d",
			r.EffectiveAt, r.GraceSatisfiedAt, oc.Seq, oc.Seq)
	}
}

// TestReadersStartingLaterDoNotExtend is the explicit requirement: readers
// that start after registration must not delay an already-registered callback.
func TestReadersStartingLaterDoNotExtend(t *testing.T) {
	c := New()
	applyAll(t, c,
		EnterRead("before"),
		RegisterCallback("cb1", "srcA"),
		EnterRead("after"), // starts after registration
	)
	r := mustReport(t, c, "cb1")
	if !reflect.DeepEqual(r.Waiters, []string{"before"}) {
		t.Fatalf("waiters = %v, want [before] (late reader must not join)", r.Waiters)
	}

	// "after" is still inside its section, but cb1 only waits on "before".
	oc := applyAll(t, c, ExitRead("before"))[0]
	if !reflect.DeepEqual(oc.BecameEffective, []string{"cb1"}) {
		t.Fatalf("released %v, want [cb1] despite 'after' still reading", oc.BecameEffective)
	}
	if _, still := c.CallbackReport("cb1"); !still {
		t.Fatal("cb1 vanished")
	}
	if n := len(c.readers); n != 1 {
		t.Fatalf("open readers = %d, want 1 (after)", n)
	}
}

// TestNestingCountsAsOutermostGeneration: nested enter/exit pairs do not
// release the callback early; only the outermost exit does.
func TestNestingCountsAsOutermostGeneration(t *testing.T) {
	c := New()
	applyAll(t, c, EnterRead("r1"), EnterRead("r1"), EnterRead("r1"))
	applyAll(t, c, RegisterCallback("cb1", "srcA"))

	rr := c.Readers()
	if len(rr) != 1 || rr[0].Nesting != 3 {
		t.Fatalf("readers = %+v, want single reader nesting 3", rr)
	}
	gen := rr[0].Generation
	if gen != 1 {
		t.Fatalf("generation = %d, want 1 (only the outermost entry starts a generation)", gen)
	}

	for i := 0; i < 2; i++ {
		oc := applyAll(t, c, ExitRead("r1"))[0]
		if len(oc.BecameEffective) != 0 {
			t.Fatalf("inner exit #%d released %v, want nothing", i+1, oc.BecameEffective)
		}
	}
	oc := applyAll(t, c, ExitRead("r1"))[0]
	if !reflect.DeepEqual(oc.BecameEffective, []string{"cb1"}) {
		t.Fatalf("outermost exit released %v, want [cb1]", oc.BecameEffective)
	}
}

// ---------------------------------------------------------------------------
// Interleaved / migration scenarios — the sequence the task asks to exercise.
// ---------------------------------------------------------------------------

// TestMigrationDoesNotEndReader: a migrating reader is still the same reader
// and keeps the callback blocked.
func TestMigrationDoesNotEndReader(t *testing.T) {
	c := New()
	applyAll(t, c,
		Migrate("r1", 0),
		EnterRead("r1"),
		RegisterCallback("cb1", "srcA"),
		Migrate("r1", 3),
	)
	r := mustReport(t, c, "cb1")
	if !reflect.DeepEqual(r.Waiters, []string{"r1"}) {
		t.Fatalf("waiters after migration = %v, want [r1]", r.Waiters)
	}
	rr := c.Readers()
	if len(rr) != 1 || rr[0].Thread != "r1" || rr[0].CPU != 3 {
		t.Fatalf("reader after migration = %+v, want r1 on cpu 3", rr)
	}
	// The reader instance id and generation survive migration.
	if rr[0].ReaderID != 1 || rr[0].Generation != 1 {
		t.Fatalf("reader identity changed across migration: %+v", rr[0])
	}
}

// TestCPUOfflineAndQuiescentDoNotReleaseReader: neither an offline announcement
// nor a quiescent state on any CPU may stand in for the thread exiting.
func TestCPUOfflineAndQuiescentDoNotReleaseReader(t *testing.T) {
	c := New()
	applyAll(t, c,
		CPUOnline(0),
		EnterRead("r1"),
		RegisterCallback("cb1", "srcA"),
		CPUQuiescent(0),
		CPUOffline(0),
		CPUQuiescent(1),
	)
	r := mustReport(t, c, "cb1")
	if r.Effective() {
		t.Fatal("CPU announcements released a callback whose reader is still inside")
	}
	if !reflect.DeepEqual(r.Waiters, []string{"r1"}) {
		t.Fatalf("waiters = %v, want [r1]", r.Waiters)
	}
	oc := applyAll(t, c, ExitRead("r1"))[0]
	if !reflect.DeepEqual(oc.BecameEffective, []string{"cb1"}) {
		t.Fatalf("thread exit released %v, want [cb1]", oc.BecameEffective)
	}
}

// TestMigratedReaderThreadPastGraceOtherStillNested is the full requested
// scenario: a reader migrates, another thread has already crossed the grace
// period (registered and released earlier), and an old thread is still nested
// inside its read section. Each callback reports precisely when it may take
// effect and who it still waits on.
func TestMigratedReaderThreadPastGraceOtherStillNested(t *testing.T) {
	c := New()
	applyAll(t, c,
		// Thread "quick" starts and fully crosses its grace period first, so
		// cb-quick is committed before the other threads even start reading.
		EnterRead("quick"),
		RegisterCallback("cb-quick", "srcA"),
		ExitRead("quick"),
		// Thread "mover" starts reading on cpu 0 and will migrate.
		Migrate("mover", 0),
		EnterRead("mover"),
		// An old thread is nested deep inside a section.
		EnterRead("old"),
		EnterRead("old"),
		EnterRead("old"),
	)

	// Register the callback that must wait on the migrated mover and on the
	// still-nested old thread, but not on the already-finished quick.
	regOut := applyAll(t, c, RegisterCallback("cb-wait", "srcA"))[0]
	if len(regOut.BecameEffective) != 0 {
		t.Fatalf("cb-wait became effective on registration: %v", regOut.BecameEffective)
	}

	// The mover migrates while still inside its section.
	applyAll(t, c, Migrate("mover", 2), CPUOffline(0), CPUQuiescent(0))

	quick := mustReport(t, c, "cb-quick")
	if !quick.Effective() {
		t.Fatal("cb-quick should already be effective; it was released when 'quick' exited")
	}
	if len(quick.Waiters) != 0 {
		t.Fatalf("cb-quick waiters = %v, want none", quick.Waiters)
	}

	wait := mustReport(t, c, "cb-wait")
	if wait.Effective() {
		t.Fatal("cb-wait must not be effective while mover and old still read")
	}
	if !reflect.DeepEqual(wait.Waiters, []string{"mover", "old"}) {
		t.Fatalf("cb-wait waiters = %v, want [mover old]", wait.Waiters)
	}

	// Mover leaves its section; old is still nested so cb-wait keeps waiting.
	mid := applyAll(t, c, ExitRead("mover"))[0]
	if len(mid.BecameEffective) != 0 {
		t.Fatalf("cb-wait released early on mover exit: %v", mid.BecameEffective)
	}
	wait = mustReport(t, c, "cb-wait")
	if !reflect.DeepEqual(wait.Waiters, []string{"old"}) {
		t.Fatalf("cb-wait waiters after mover exit = %v, want [old]", wait.Waiters)
	}

	// Old unwinds its nesting one level at a time; only the last releases.
	for i := 0; i < 2; i++ {
		if oc := applyAll(t, c, ExitRead("old"))[0]; len(oc.BecameEffective) != 0 {
			t.Fatalf("nested exit #%d released cb-wait: %v", i+1, oc.BecameEffective)
		}
	}
	final := applyAll(t, c, ExitRead("old"))[0]
	if !reflect.DeepEqual(final.BecameEffective, []string{"cb-wait"}) {
		t.Fatalf("outermost old exit released %v, want [cb-wait]", final.BecameEffective)
	}
	wait = mustReport(t, c, "cb-wait")
	if wait.EffectiveAt != final.Seq {
		t.Fatalf("cb-wait effective at %d, want %d", wait.EffectiveAt, final.Seq)
	}
	if len(wait.Waiters) != 0 {
		t.Fatalf("cb-wait waiters = %v, want none", wait.Waiters)
	}
}

// ---------------------------------------------------------------------------
// Multiple callbacks with different wait sets; per-source ordering; acks.
// ---------------------------------------------------------------------------

// TestDifferentWaitSetsAndSourceOrder: callbacks hold distinct wait sets, and
// within a source delivery is strictly registration order even when a later
// callback's wait set finishes first.
func TestDifferentWaitSetsAndSourceOrder(t *testing.T) {
	c := New()
	applyAll(t, c,
		EnterRead("a"),
		EnterRead("b"),
		RegisterCallback("cb1", "src"), // waits on a,b
	)
	applyAll(t, c, ExitRead("b"))
	// cb2's wait set is only the remaining reader "a"; it must not be handed
	// over before cb1 because cb1 from the same source is still waiting.
	applyAll(t, c, RegisterCallback("cb2", "src"))

	r1 := mustReport(t, c, "cb1")
	r2 := mustReport(t, c, "cb2")
	if r1.Effective() || r2.Effective() {
		t.Fatalf("both same-source callbacks should wait; cb1=%v cb2=%v", r1.Effective(), r2.Effective())
	}
	if !reflect.DeepEqual(r1.Waiters, []string{"a"}) {
		t.Fatalf("cb1 waiters = %v, want [a]", r1.Waiters)
	}
	if !reflect.DeepEqual(r2.Waiters, []string{"a"}) {
		t.Fatalf("cb2 waiters = %v, want [a]", r2.Waiters)
	}

	oc := applyAll(t, c, ExitRead("a"))[0]
	if !reflect.DeepEqual(oc.BecameEffective, []string{"cb1", "cb2"}) {
		t.Fatalf("release order = %v, want [cb1 cb2] (registration order within source)", oc.BecameEffective)
	}
}

// TestSourceIndependenceAndOrdering: a blocked source does not hold back a
// different source, and callbacks from one source are handed over in
// registration order.
//
// Note on reachability: because a callback's wait set is snapshot from every
// reader inside a section, a later same-source callback always retains any
// still-open reader that blocks an earlier one. So "tail's waiters empty while
// head's are not" cannot occur; ordering still holds and is asserted here for
// the reachable case.
func TestSourceIndependenceAndOrdering(t *testing.T) {
	// An independent source registered with no open readers releases at once.
	c := New()
	oc := applyAll(t, c, RegisterCallback("other", "srcY"))[0]
	if !reflect.DeepEqual(oc.BecameEffective, []string{"other"}) {
		t.Fatalf("srcY released %v, want [other]", oc.BecameEffective)
	}

	applyAll(t, c,
		EnterRead("slow"),
		RegisterCallback("head", "srcX"), // waits on slow
		RegisterCallback("tail", "srcX"), // also waits on slow
	)
	if mustReport(t, c, "head").Effective() || mustReport(t, c, "tail").Effective() {
		t.Fatal("srcX callbacks effective while 'slow' still reads")
	}
	if !mustReport(t, c, "other").Effective() {
		t.Fatal("srcY callback lost its effectiveness when srcX blocked")
	}

	rel := applyAll(t, c, ExitRead("slow"))[0]
	if !reflect.DeepEqual(rel.BecameEffective, []string{"head", "tail"}) {
		t.Fatalf("release on unblock = %v, want [head tail] (registration order)", rel.BecameEffective)
	}
	head := mustReport(t, c, "head")
	tail := mustReport(t, c, "tail")
	if head.EffectiveAt != tail.EffectiveAt {
		t.Fatalf("head/tail effective at %d/%d, want equal (batch release)", head.EffectiveAt, tail.EffectiveAt)
	}
}

// TestDuplicateAckOnlyConfirmsOnce: repeated/replayed acks confirm once and do
// not move the first-confirmation sequence.
func TestDuplicateAckOnlyConfirmsOnce(t *testing.T) {
	c := New()
	applyAll(t, c, RegisterCallback("cb1", "src"))
	first := applyAll(t, c, AckCallback("cb1"))[0]
	second := applyAll(t, c, AckCallback("cb1"))[0]
	if second.Seq == first.Seq {
		t.Fatal("test setup: acks should have distinct sequence numbers")
	}
	r := mustReport(t, c, "cb1")
	if !r.Acked || r.AckedAt != first.Seq {
		t.Fatalf("acked=%v at %d, want true at first ack %d", r.Acked, r.AckedAt, first.Seq)
	}
	if second.Seq <= first.Seq {
		t.Fatalf("second ack seq %d should follow first %d", second.Seq, first.Seq)
	}
}

// TestAckBeforeEffectiveIsRecorded: an ack may arrive before the callback is
// effective; it is still recorded once.
func TestAckBeforeEffectiveIsRecorded(t *testing.T) {
	c := New()
	applyAll(t, c, EnterRead("r1"), RegisterCallback("cb1", "src"))
	ack := applyAll(t, c, AckCallback("cb1"))[0]
	r := mustReport(t, c, "cb1")
	if r.Effective() {
		t.Fatal("premature effective")
	}
	if !r.Acked || r.AckedAt != ack.Seq {
		t.Fatalf("ack not recorded: %+v", r)
	}
}

// ---------------------------------------------------------------------------
// Boundary / error handling
// ---------------------------------------------------------------------------

// TestExitWithoutEnterRejected: an unmatched exit is rejected and not logged,
// so replay stays faithful.
func TestExitWithoutEnterRejected(t *testing.T) {
	c := New()
	before := len(c.Log())
	_, err := c.Apply(ExitRead("ghost"))
	if !errors.Is(err, ErrNotReading) {
		t.Fatalf("err = %v, want ErrNotReading", err)
	}
	if len(c.Log()) != before {
		t.Fatal("rejected event was logged")
	}
	if c.LastSeq() != 0 {
		t.Fatalf("LastSeq = %d, want 0 after rejected event", c.LastSeq())
	}
}

// TestRejectedEventsLeaveStateUnchanged covers duplicate callback ids, unknown
// ack targets, empty ids and bad kinds.
func TestRejectedEventsLeaveStateUnchanged(t *testing.T) {
	c := New()
	applyAll(t, c, RegisterCallback("cb1", "src"))
	base := c.projectionString(t)

	cases := []struct {
		e    Event
		want error
	}{
		{RegisterCallback("cb1", "src"), ErrDuplicateCallback},
		{AckCallback("nope"), ErrUnknownCallback},
		{EnterRead(""), ErrEmptyThread},
		{Event{Kind: KindMigrate, Thread: "x", CPU: -1}, ErrNegativeCPU},
		{Event{Kind: EventKind(99)}, ErrUnknownKind},
	}
	for _, tc := range cases {
		if _, err := c.Apply(tc.e); !errors.Is(err, tc.want) {
			t.Fatalf("Apply(%s) err = %v, want %v", tc.e, err, tc.want)
		}
		if got := c.projectionString(t); got != base {
			t.Fatalf("state changed after rejected %s", tc.e)
		}
	}
}

// TestReenterAfterExitIsNewReader: a thread that exits and re-enters becomes a
// new reader instance and must not re-block an already released callback.
func TestReenterAfterExitIsNewReader(t *testing.T) {
	c := New()
	applyAll(t, c, EnterRead("r1"), RegisterCallback("cb1", "src"))
	applyAll(t, c, ExitRead("r1"))
	if !mustReport(t, c, "cb1").Effective() {
		t.Fatal("cb1 not released on exit")
	}
	applyAll(t, c, EnterRead("r1"))
	r := mustReport(t, c, "cb1")
	if !r.Effective() || len(r.Waiters) != 0 {
		t.Fatalf("new reader instance re-blocked a released callback: %+v", r)
	}
	rr := c.Readers()
	if len(rr) != 1 || rr[0].ReaderID == 1 || rr[0].Generation != 2 {
		t.Fatalf("re-entered reader = %+v, want fresh id and generation 2", rr)
	}
}

// ---------------------------------------------------------------------------
// Determinism + reopen
// ---------------------------------------------------------------------------

// buildScenario returns a reusable, non-trivial event sequence that ends with
// two committed callbacks (cb-quick, cb-independent) and one pending callback
// (cb-wait) still blocked by two open readers, one of them nested.
func buildScenario() []Event {
	return []Event{
		// Commit quick first: no other readers exist yet.
		EnterRead("quick"),
		RegisterCallback("cb-quick", "srcA"),
		ExitRead("quick"),
		// An independent source with nobody reading commits immediately.
		RegisterCallback("cb-independent", "srcB"),
		// Now open the readers that cb-wait must wait on.
		Migrate("mover", 0),
		EnterRead("mover"),
		EnterRead("old"),
		EnterRead("old"),
		RegisterCallback("cb-wait", "srcA"), // waits on mover + old
		Migrate("mover", 3),
		CPUOnline(3),
		CPUOffline(0),
		CPUQuiescent(3),
		AckCallback("cb-quick"),
		AckCallback("cb-quick"), // duplicate; confirms once
	}
}

// TestReplayIsDeterministic: applying the same events twice yields identical
// observable state.
func TestReplayIsDeterministic(t *testing.T) {
	a, err := Replay(buildScenario())
	if err != nil {
		t.Fatal(err)
	}
	b, err := Replay(buildScenario())
	if err != nil {
		t.Fatal(err)
	}
	pa, err := a.projection()
	if err != nil {
		t.Fatal(err)
	}
	pb, err := b.projection()
	if err != nil {
		t.Fatal(err)
	}
	if pa != pb {
		t.Fatal("replay not deterministic")
	}
	if a.LastSeq() != int64(len(buildScenario())) {
		t.Fatalf("LastSeq = %d, want %d", a.LastSeq(), len(buildScenario()))
	}
}

// TestSnapshotReopenPreservesCommittedAndOpen: reopening keeps committed
// callback positions, pending waiters, and still-open nested read sections.
func TestSnapshotReopenPreservesCommittedAndOpen(t *testing.T) {
	c, err := Replay(buildScenario())
	if err != nil {
		t.Fatal(err)
	}
	before := c.projectionString(t)

	data, err := c.Snapshot()
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	reopened, err := Reopen(data)
	if err != nil {
		t.Fatalf("Reopen: %v", err)
	}
	if got := reopened.projectionString(t); got != before {
		t.Fatalf("state changed across reopen:\nbefore=%s\nafter =%s", before, got)
	}

	// Committed callback position survives: cb-quick is effective and acked.
	quick := mustReport(t, reopened, "cb-quick")
	if !quick.Effective() || !quick.Acked {
		t.Fatalf("cb-quick not preserved: %+v", quick)
	}
	// Pending callback keeps its exact waiters.
	wait := mustReport(t, reopened, "cb-wait")
	if wait.Effective() != false || !reflect.DeepEqual(wait.Waiters, []string{"mover", "old"}) {
		t.Fatalf("cb-wait not preserved: %+v", wait)
	}
	// Open read sections (with nesting) survive.
	var old ReaderReport
	for _, rr := range reopened.Readers() {
		if rr.Thread == "old" {
			old = rr
		}
	}
	if old.Thread != "old" || old.Nesting != 2 {
		t.Fatalf("open nested reader lost across reopen: %+v", reopened.Readers())
	}
	if reopened.Generation() != c.Generation() {
		t.Fatalf("generation changed: %d vs %d", reopened.Generation(), c.Generation())
	}
	// Continuing after reopen behaves like the original would have.
	applyAll(t, reopened, ExitRead("mover"))
	if !reflect.DeepEqual(mustReport(t, reopened, "cb-wait").Waiters, []string{"old"}) {
		t.Fatalf("post-reopen exit did not update waiters: %+v", mustReport(t, reopened, "cb-wait"))
	}
}

// TestReopenRejectsCorruptSnapshot: verification catches a snapshot whose
// stored state disagrees with its log, and structurally bad JSON.
func TestReopenRejectsCorruptSnapshot(t *testing.T) {
	c, err := Replay(buildScenario())
	if err != nil {
		t.Fatal(err)
	}
	data, err := c.Snapshot()
	if err != nil {
		t.Fatal(err)
	}

	// Drop the log so the state can no longer be reproduced from it.
	corrupt := strings.Replace(string(data), `"log": [`, `"log": [], "_old_log": [`, 1)
	if _, err := Reopen([]byte(corrupt)); !errors.Is(err, ErrBadSnapshot) {
		t.Fatalf("corrupt snapshot err = %v, want ErrBadSnapshot", err)
	}
	if _, err := Reopen([]byte("{not json")); !errors.Is(err, ErrBadSnapshot) {
		t.Fatalf("bad json err = %v, want ErrBadSnapshot", err)
	}
}

// TestLogDrivesReplayFaithfully: the accepted log, replayed into a fresh
// coordinator, reproduces current state (rejected events are absent).
func TestLogDrivesReplayFaithfully(t *testing.T) {
	c := New()
	applyAll(t, c, buildScenario()...)
	// Inject two rejected events; they must not enter the log.
	if _, err := c.Apply(ExitRead("ghost")); err == nil {
		t.Fatal("expected rejection")
	}
	if _, err := c.Apply(AckCallback("ghost")); err == nil {
		t.Fatal("expected rejection")
	}

	logged := c.Log()
	events := make([]Event, len(logged))
	for i, le := range logged {
		if le.Seq != int64(i+1) {
			t.Fatalf("log seq not contiguous: idx %d seq %d", i, le.Seq)
		}
		events[i] = le.Event
	}
	replayed, err := Replay(events)
	if err != nil {
		t.Fatal(err)
	}
	if replayed.projectionString(t) != c.projectionString(t) {
		t.Fatal("replaying the accepted log did not reproduce state")
	}
}

// projectionString is a test helper wrapping the unexported projection.
func (c *Coordinator) projectionString(t *testing.T) string {
	t.Helper()
	s, err := c.projection()
	if err != nil {
		t.Fatalf("projection: %v", err)
	}
	return s
}
