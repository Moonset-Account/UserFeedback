package probe

import (
	"reflect"
	"sync"
	"testing"
)

// mustDispatch fails the test when a probe family is unexpectedly unavailable.
func mustDispatch(t *testing.T, c *Controller, pt ProbeType) Probe {
	t.Helper()
	p, ok := c.Dispatch(pt)
	if !ok {
		t.Fatalf("Dispatch(%s) unexpectedly unavailable", pt)
	}
	return p
}

func mustSuccess(p Probe) Receipt {
	return Receipt{Instance: p.Instance, Type: p.Type, Seq: p.Seq, Success: true}
}

func mustFailure(p Probe) Receipt {
	return Receipt{Instance: p.Instance, Type: p.Type, Seq: p.Seq, Success: false}
}

func assertEvents(t *testing.T, got, want []Event) {
	t.Helper()
	if len(got) == 0 && len(want) == 0 {
		return
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("events mismatch:\n got: %#v\nwant: %#v", got, want)
	}
}

// startupComplete drives the current instance through a successful startup
// probe so readiness and liveness probes become available.
func startupComplete(t *testing.T, c *Controller) {
	t.Helper()
	p := mustDispatch(t, c, Startup)
	if ev := c.Deliver(mustSuccess(p)); ev != nil {
		t.Fatalf("startup success produced events: %#v", ev)
	}
	if !c.StartupDone() {
		t.Fatalf("startup not marked done after success")
	}
}

// makeReady drives readiness to enabled via two consecutive successes.
func makeReady(t *testing.T, c *Controller) {
	t.Helper()
	startupComplete(t, c)
	r1 := mustDispatch(t, c, Readiness)
	r2 := mustDispatch(t, c, Readiness)
	if ev := c.Deliver(mustSuccess(r1)); ev != nil {
		t.Fatalf("first readiness success produced events: %#v", ev)
	}
	assertEvents(t, c.Deliver(mustSuccess(r2)), []Event{{Kind: TrafficEnabled, Instance: c.Instance()}})
	if !c.Ready() {
		t.Fatalf("expected ready after two consecutive successes")
	}
}

func TestStartupGatesOtherProbeFamilies(t *testing.T) {
	c := NewController()

	if _, ok := c.Dispatch(Readiness); ok {
		t.Fatalf("readiness probe must not be dispatchable before startup success")
	}
	if _, ok := c.Dispatch(Liveness); ok {
		t.Fatalf("liveness probe must not be dispatchable before startup success")
	}
	if p, ok := c.Dispatch(Startup); !ok || p.Seq != 1 || p.Type != Startup {
		t.Fatalf("first startup probe = %#v, ok=%v; want seq 1", p, ok)
	}
	if c.StartupDone() {
		t.Fatalf("startup must not be done before any result")
	}
}

func TestStartupSuccessOpensReadinessAndLiveness(t *testing.T) {
	c := NewController()
	startupComplete(t, c)

	if _, ok := c.Dispatch(Startup); ok {
		t.Fatalf("startup probes must stop being dispatchable after success")
	}
	if p, ok := c.Dispatch(Readiness); !ok || p.Seq != 1 {
		t.Fatalf("readiness probe after startup success = %#v, ok=%v", p, ok)
	}
	if p, ok := c.Dispatch(Liveness); !ok || p.Seq != 1 {
		t.Fatalf("liveness probe after startup success = %#v, ok=%v", p, ok)
	}
}

func TestReadinessRequiresTwoConsecutiveSuccesses(t *testing.T) {
	c := NewController()
	startupComplete(t, c)

	r1 := mustDispatch(t, c, Readiness)
	if ev := c.Deliver(mustSuccess(r1)); ev != nil {
		t.Fatalf("one readiness success must not enable traffic, got %#v", ev)
	}
	if c.Ready() {
		t.Fatalf("must not be ready after a single success")
	}

	r2 := mustDispatch(t, c, Readiness)
	assertEvents(t, c.Deliver(mustSuccess(r2)), []Event{{Kind: TrafficEnabled, Instance: 1}})
	if !c.Ready() {
		t.Fatalf("must be ready after two consecutive successes")
	}
}

func TestReadinessFailureRemovesTrafficImmediately(t *testing.T) {
	c := NewController()
	makeReady(t, c)

	r := mustDispatch(t, c, Readiness)
	assertEvents(t, c.Deliver(mustFailure(r)), []Event{{Kind: TrafficDisabled, Instance: 1}})
	if c.Ready() {
		t.Fatalf("one readiness failure must remove traffic")
	}
}

func TestReadinessFailureResetsStreak(t *testing.T) {
	c := NewController()
	startupComplete(t, c)

	// success, failure, success -> still only a streak of one.
	r1 := mustDispatch(t, c, Readiness)
	c.Deliver(mustSuccess(r1))
	r2 := mustDispatch(t, c, Readiness)
	c.Deliver(mustFailure(r2))
	r3 := mustDispatch(t, c, Readiness)
	r4 := mustDispatch(t, c, Readiness)
	if ev := c.Deliver(mustSuccess(r3)); ev != nil {
		t.Fatalf("success after a failure (streak 1) must not enable traffic: %#v", ev)
	}
	assertEvents(t, c.Deliver(mustSuccess(r4)), []Event{{Kind: TrafficEnabled, Instance: 1}})
}

// TestReadinessOutOfOrderAppliesInDispatchOrder verifies that results take
// effect in dispatch order even when they complete out of order. A naive
// arrival-order application would incorrectly see two consecutive successes
// and enable traffic.
func TestReadinessOutOfOrderAppliesInDispatchOrder(t *testing.T) {
	c := NewController()
	startupComplete(t, c)

	r1 := mustDispatch(t, c, Readiness) // success
	r2 := mustDispatch(t, c, Readiness) // failure
	r3 := mustDispatch(t, c, Readiness) // success

	if ev := c.Deliver(mustSuccess(r3)); ev != nil {
		t.Fatalf("buffered r3 must not produce events yet: %#v", ev)
	}
	if c.Ready() {
		t.Fatalf("ready before earlier results were applied")
	}
	if ev := c.Deliver(mustSuccess(r1)); ev != nil {
		t.Fatalf("r1 alone (r2 not yet applied) must not enable traffic: %#v", ev)
	}
	if c.Ready() {
		t.Fatalf("premature traffic with a missing r2")
	}
	if ev := c.Deliver(mustFailure(r2)); ev != nil {
		// r1 success (streak 1), r2 failure (streak 0), r3 success (streak 1):
		// net readiness is unchanged (still disabled).
		t.Fatalf("dispatch-order evaluation must not enable traffic: %#v", ev)
	}
	if c.Ready() {
		t.Fatalf("ready must be false: dispatch order is success,failure,success")
	}
	if got := c.Snapshot().ReadinessStreak; got != 1 {
		t.Fatalf("readiness streak = %d, want 1", got)
	}
}

func TestReadinessOutOfOrderStillEnablesAfterBoth(t *testing.T) {
	c := NewController()
	startupComplete(t, c)

	r1 := mustDispatch(t, c, Readiness)
	r2 := mustDispatch(t, c, Readiness)
	if ev := c.Deliver(mustSuccess(r2)); ev != nil {
		t.Fatalf("out-of-order r2 alone must not enable traffic: %#v", ev)
	}
	assertEvents(t, c.Deliver(mustSuccess(r1)), []Event{{Kind: TrafficEnabled, Instance: 1}})
	if !c.Ready() {
		t.Fatalf("two consecutive successes must enable traffic regardless of completion order")
	}
}

func TestStartupOutOfOrderSuccess(t *testing.T) {
	c := NewController()
	s1 := mustDispatch(t, c, Startup)
	s2 := mustDispatch(t, c, Startup)

	if ev := c.Deliver(mustSuccess(s2)); ev != nil || c.StartupDone() {
		t.Fatalf("out-of-order startup success applied before seq 1: events=%#v done=%v", ev, c.StartupDone())
	}
	if ev := c.Deliver(mustSuccess(s1)); ev != nil {
		t.Fatalf("startup success produced unexpected events: %#v", ev)
	}
	if !c.StartupDone() {
		t.Fatalf("startup must complete once seq 1 and seq 2 are applied in order")
	}
}

func TestStartupThreeConsecutiveFailuresRestart(t *testing.T) {
	c := NewController()

	s1 := mustDispatch(t, c, Startup)
	s2 := mustDispatch(t, c, Startup)
	s3 := mustDispatch(t, c, Startup)

	if ev := c.Deliver(mustFailure(s1), mustFailure(s2)); ev != nil {
		t.Fatalf("two startup failures must not restart: %#v", ev)
	}
	assertEvents(t, c.Deliver(mustFailure(s3)), []Event{{Kind: Restart, Instance: 2}})

	snap := c.Snapshot()
	if snap.Instance != 2 || snap.StartupDone || snap.Ready || snap.StartupFailures != 0 {
		t.Fatalf("state after startup restart = %#v", snap)
	}
	// New instance must renumber every family from 1.
	if p, ok := c.Dispatch(Startup); !ok || p.Instance != 2 || p.Seq != 1 {
		t.Fatalf("new instance startup probe = %#v, ok=%v", p, ok)
	}
}

func TestStartupSuccessResetsFailureCount(t *testing.T) {
	c := NewController()

	s1 := mustDispatch(t, c, Startup)
	s2 := mustDispatch(t, c, Startup)
	s3 := mustDispatch(t, c, Startup)

	c.Deliver(mustFailure(s1))
	c.Deliver(mustFailure(s2))
	if ev := c.Deliver(mustSuccess(s3)); ev != nil {
		t.Fatalf("startup success produced events: %#v", ev)
	}
	if !c.StartupDone() {
		t.Fatalf("startup must complete on success")
	}
	if got := c.Snapshot().StartupFailures; got != 0 {
		t.Fatalf("startup failures = %d, want 0 after success", got)
	}
}

func TestLivenessThreeConsecutiveFailuresRestartWhileServing(t *testing.T) {
	c := NewController()
	makeReady(t, c)

	l1 := mustDispatch(t, c, Liveness)
	l2 := mustDispatch(t, c, Liveness)
	l3 := mustDispatch(t, c, Liveness)

	c.Deliver(mustFailure(l1), mustFailure(l2))
	if c.Instance() != 1 {
		t.Fatalf("two liveness failures must not restart")
	}
	assertEvents(t, c.Deliver(mustFailure(l3)), []Event{
		{Kind: TrafficDisabled, Instance: 1},
		{Kind: Restart, Instance: 2},
	})

	snap := c.Snapshot()
	if snap.Ready || snap.Instance != 2 || snap.LivenessFailures != 0 {
		t.Fatalf("state after liveness restart = %#v", snap)
	}
}

func TestLivenessThreeConsecutiveFailuresRestartNotServing(t *testing.T) {
	c := NewController()
	startupComplete(t, c) // startup done but not ready

	l1 := mustDispatch(t, c, Liveness)
	l2 := mustDispatch(t, c, Liveness)
	l3 := mustDispatch(t, c, Liveness)

	c.Deliver(mustFailure(l1), mustFailure(l2))
	assertEvents(t, c.Deliver(mustFailure(l3)), []Event{{Kind: Restart, Instance: 2}})
}

func TestLivenessSuccessResetsFailureCount(t *testing.T) {
	c := NewController()
	startupComplete(t, c)

	l1 := mustDispatch(t, c, Liveness)
	l2 := mustDispatch(t, c, Liveness)
	l3 := mustDispatch(t, c, Liveness)
	l4 := mustDispatch(t, c, Liveness)
	l5 := mustDispatch(t, c, Liveness)

	c.Deliver(mustFailure(l1), mustFailure(l2), mustSuccess(l3))
	if c.Instance() != 1 {
		t.Fatalf("a success in between must reset the liveness failure count")
	}
	c.Deliver(mustFailure(l4), mustFailure(l5))
	if c.Instance() != 1 {
		t.Fatalf("only two consecutive liveness failures after a success; no restart expected")
	}
	l6 := mustDispatch(t, c, Liveness)
	assertEvents(t, c.Deliver(mustFailure(l6)), []Event{{Kind: Restart, Instance: 2}})
}

// TestBatchRestartSuppressesReadinessEnable verifies that a readiness change
// to "enabled" in the same batch as a restart is not published.
func TestBatchRestartSuppressesReadinessEnable(t *testing.T) {
	c := NewController()
	startupComplete(t, c)

	r1 := mustDispatch(t, c, Readiness)
	r2 := mustDispatch(t, c, Readiness)
	l1 := mustDispatch(t, c, Liveness)
	l2 := mustDispatch(t, c, Liveness)
	l3 := mustDispatch(t, c, Liveness)

	events := c.Deliver(
		mustSuccess(r1), mustSuccess(r2), // would enable traffic
		mustFailure(l1), mustFailure(l2), mustFailure(l3), // triggers restart
	)
	assertEvents(t, events, []Event{{Kind: Restart, Instance: 2}})
	if c.Ready() {
		t.Fatalf("new instance must not be ready")
	}
}

// TestBatchRestartPublishesOnlyRestartAndTrafficRemoval verifies that when the
// instance was serving traffic, a batch that also contains a readiness failure
// publishes exactly one traffic removal alongside the restart.
func TestBatchRestartPublishesOnlyRestartAndTrafficRemoval(t *testing.T) {
	c := NewController()
	makeReady(t, c)

	r := mustDispatch(t, c, Readiness) // a failure that would remove traffic anyway
	l1 := mustDispatch(t, c, Liveness)
	l2 := mustDispatch(t, c, Liveness)
	l3 := mustDispatch(t, c, Liveness)

	events := c.Deliver(
		mustFailure(r),
		mustFailure(l1), mustFailure(l2), mustFailure(l3),
	)
	assertEvents(t, events, []Event{
		{Kind: TrafficDisabled, Instance: 1},
		{Kind: Restart, Instance: 2},
	})
}

// TestBatchRestartSuppressesReadinessDisableWhenNotServing verifies that a
// readiness change on a non-serving instance is not published when the batch
// also restarts it.
func TestBatchRestartSuppressesReadinessDisableWhenNotServing(t *testing.T) {
	c := NewController()
	startupComplete(t, c)

	// Prime a single readiness success so a failure would otherwise be a no-op
	// anyway; the point is that no traffic event is emitted.
	r := mustDispatch(t, c, Readiness)
	c.Deliver(mustSuccess(r))

	l1 := mustDispatch(t, c, Liveness)
	l2 := mustDispatch(t, c, Liveness)
	l3 := mustDispatch(t, c, Liveness)

	events := c.Deliver(mustFailure(l1), mustFailure(l2), mustFailure(l3))
	assertEvents(t, events, []Event{{Kind: Restart, Instance: 2}})
}

func TestStaleReceiptsFromPreviousInstanceIgnored(t *testing.T) {
	c := NewController()
	makeReady(t, c) // instance 1 serving

	// Old-instance readiness probes dispatched before the restart.
	oldR1 := mustDispatch(t, c, Readiness)
	oldR2 := mustDispatch(t, c, Readiness)

	l1 := mustDispatch(t, c, Liveness)
	l2 := mustDispatch(t, c, Liveness)
	l3 := mustDispatch(t, c, Liveness)
	c.Deliver(mustFailure(l1), mustFailure(l2))
	assertEvents(t, c.Deliver(mustFailure(l3)), []Event{
		{Kind: TrafficDisabled, Instance: 1},
		{Kind: Restart, Instance: 2},
	})
	if c.Ready() {
		t.Fatalf("new instance must start not-ready")
	}

	// Late results from the previous instance must be ignored entirely: they
	// must not enable traffic nor mark startup done on the new instance.
	c.Deliver(mustSuccess(oldR1), mustSuccess(oldR2),
		Receipt{Instance: 1, Type: Startup, Seq: 1, Success: true})
	if c.Ready() {
		t.Fatalf("late previous-instance receipts enabled traffic on the new instance")
	}
	if c.StartupDone() {
		t.Fatalf("late previous-instance startup receipt marked the new instance started")
	}
	// New instance probes must still be gated behind its own startup probe.
	if _, ok := c.Dispatch(Readiness); ok {
		t.Fatalf("new instance readiness must not be dispatchable before its own startup success")
	}
	newS := mustDispatch(t, c, Startup)
	if newS.Instance != 2 || newS.Seq != 1 {
		t.Fatalf("new startup probe = %#v, want instance 2 seq 1", newS)
	}

	// Even after the new instance starts up, old-instance readiness successes
	// must not contribute to its readiness streak.
	c.Deliver(mustSuccess(newS))
	c.Deliver(mustSuccess(oldR1), mustSuccess(oldR2))
	if c.Ready() {
		t.Fatalf("old readiness successes leaked into the new instance")
	}
	if got := c.Snapshot().ReadinessStreak; got != 0 {
		t.Fatalf("readiness streak = %d, want 0", got)
	}
}

func TestDuplicateAndUnknownReceiptsIgnored(t *testing.T) {
	c := NewController()
	startupComplete(t, c)

	r1 := mustDispatch(t, c, Readiness)
	// Unknown (never dispatched) sequence numbers.
	c.Deliver(Receipt{Instance: 1, Type: Readiness, Seq: 99, Success: true})
	c.Deliver(Receipt{Instance: 1, Type: Readiness, Seq: 0, Success: true})
	if c.Ready() {
		t.Fatalf("unknown readiness receipts must be ignored")
	}
	// First success only, streak 1.
	c.Deliver(mustSuccess(r1))
	// Duplicate of an already applied result must not count twice.
	c.Deliver(mustSuccess(r1))
	if c.Ready() {
		t.Fatalf("a duplicate success must not satisfy the consecutive-success rule")
	}
	if got := c.Snapshot().ReadinessStreak; got != 1 {
		t.Fatalf("readiness streak = %d, want 1", got)
	}
}

func TestSnapshotTracksSequenceNumbers(t *testing.T) {
	c := NewController()
	snap := c.Snapshot()
	if snap.NextStartupSeq != 1 || snap.NextReadinessSeq != 1 || snap.NextLivenessSeq != 1 {
		t.Fatalf("initial next seqs = %#v", snap)
	}
	mustDispatch(t, c, Startup)
	mustDispatch(t, c, Startup)
	if got := c.Snapshot().NextStartupSeq; got != 3 {
		t.Fatalf("next startup seq = %d, want 3", got)
	}
}

// TestConcurrentDelivery is a race-oriented smoke test: dispatching and
// delivering concurrently must not corrupt state, and two consecutive
// successes must still enable traffic regardless of completion order.
func TestConcurrentDelivery(t *testing.T) {
	c := NewController()
	mustDispatch(t, c, Startup) // dispatch startup; deliver below
	startupProbe := Probe{Instance: 1, Type: Startup, Seq: 1}
	c.Deliver(mustSuccess(startupProbe))

	r1 := mustDispatch(t, c, Readiness)
	r2 := mustDispatch(t, c, Readiness)

	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); c.Deliver(mustSuccess(r1)) }()
	go func() { defer wg.Done(); c.Deliver(mustSuccess(r2)) }()
	wg.Wait()

	if !c.Ready() {
		t.Fatalf("two delivered successes must enable traffic under concurrency")
	}
}

// TestBatchNetReadinessChangeOnly verifies that within a single non-restart
// batch, intermediate readiness transitions are collapsed into at most one
// published event reflecting the net change.
func TestBatchNetReadinessChangeOnly(t *testing.T) {
	c := NewController()
	startupComplete(t, c)

	r1 := mustDispatch(t, c, Readiness)
	r2 := mustDispatch(t, c, Readiness)
	r3 := mustDispatch(t, c, Readiness)

	// success, success (enable), failure (disable) -> net unchanged (disabled).
	events := c.Deliver(mustSuccess(r1), mustSuccess(r2), mustFailure(r3))
	if events != nil {
		t.Fatalf("net-unchanged readiness batch must publish no traffic event: %#v", events)
	}
	if c.Ready() {
		t.Fatalf("final readiness must be disabled")
	}

	// Now a batch that ends enabled: failure, success, success -> one enabling.
	r4 := mustDispatch(t, c, Readiness)
	r5 := mustDispatch(t, c, Readiness)
	r6 := mustDispatch(t, c, Readiness)
	events = c.Deliver(mustFailure(r4), mustSuccess(r5), mustSuccess(r6))
	assertEvents(t, events, []Event{{Kind: TrafficEnabled, Instance: 1}})
	if !c.Ready() {
		t.Fatalf("final readiness must be enabled")
	}
}
