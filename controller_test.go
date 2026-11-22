package capacitycontroller

import (
	"sync"
	"testing"
)

func mustNew(t *testing.T, cfg Config) *Controller {
	t.Helper()
	c, err := New(cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return c
}

func mustIngest(t *testing.T, c *Controller, o Observation) {
	t.Helper()
	if err := c.IngestObservation(o); err != nil {
		t.Fatalf("IngestObservation %+v: %v", o, err)
	}
}

func findDecision(t *testing.T, ds []Decision, window int) Decision {
	t.Helper()
	for _, d := range ds {
		if d.Verdict.Window == window {
			return d
		}
	}
	t.Fatalf("decision for window %d not found in %d decisions", window, len(ds))
	return Decision{}
}

func assertStatus(t *testing.T, v Verdict, want WindowStatus) {
	t.Helper()
	if v.Status != want {
		t.Fatalf("window %d: status = %s, want %s (util=%d/1000, problems=%d)",
			v.Window, v.Status, want, v.UtilizationPermille, len(v.Problems))
	}
}

func assertProblemKind(t *testing.T, v Verdict, want ProblemKind) {
	t.Helper()
	for _, p := range v.Problems {
		if p.Kind == want {
			return
		}
	}
	t.Fatalf("window %d: expected a %s problem, got %+v", v.Window, want, v.Problems)
}

// TestSampledRestartCorrectionAndLateAck drives the full sampled sequence:
// instance A restarts across generations, an old-generation observation
// arrives late but must not fill the new-generation gap, a later correction
// completes the still-unpublished window, and the scale-out receipt is late
// so cooldown starts only when it is acknowledged.
func TestSampledRestartCorrectionAndLateAck(t *testing.T) {
	c := mustNew(t, DefaultConfig())

	// Lifecycle: A gen0 [0,24), A gen1 [24,50); B gen0 [0,80).
	if err := c.OnlineSpan("A", 0, 0, 24); err != nil {
		t.Fatal(err)
	}
	if err := c.OnlineSpan("A", 1, 24, 50); err != nil {
		t.Fatal(err)
	}
	if err := c.OnlineSpan("B", 0, 0, 80); err != nil {
		t.Fatal(err)
	}

	// ---- Window 0 [0,10): both instances at 80% busy -> high (boundary).
	mustIngest(t, c, Observation{"A", 0, 0, 10, 1, 100, 8})
	mustIngest(t, c, Observation{"B", 0, 0, 10, 1, 80, 8})

	// ---- Window 1 [10,20): A 100%, B 60% -> 16/20 = 80% -> high.
	mustIngest(t, c, Observation{"A", 0, 10, 20, 1, 120, 10})
	mustIngest(t, c, Observation{"B", 0, 10, 20, 1, 60, 6})

	// First pump at t=22: two consecutive high windows -> action #1.
	ds := c.Pump(22)
	if len(ds) != 2 {
		t.Fatalf("pump@22: got %d decisions, want 2", len(ds))
	}
	assertStatus(t, ds[0].Verdict, StatusHigh)
	assertStatus(t, ds[1].Verdict, StatusHigh)
	if ds[0].Action != nil {
		t.Fatalf("window 0 must not emit an action alone")
	}
	if ds[1].Action == nil || ds[1].Action.ID != 1 || ds[1].Action.Direction != ScaleOut {
		t.Fatalf("window 1 should emit scale_out action #1, got %+v", ds[1].Action)
	}
	if c.PendingActionID() != 1 {
		t.Fatalf("pending action = %d, want 1", c.PendingActionID())
	}

	// ---- Window 2 [20,30): A restarts at t=24.
	// A gen0 covers [20,24); A gen1 [24,30). The gen1 [26,30) piece is
	// initially missing, so the window cannot be verified yet.
	mustIngest(t, c, Observation{"A", 0, 20, 24, 1, 40, 4})
	mustIngest(t, c, Observation{"A", 1, 24, 26, 1, 30, 2})
	mustIngest(t, c, Observation{"B", 0, 20, 30, 1, 70, 8})

	// Late observation from the OLD generation (A gen0) covering time after
	// the restart. It must never fill the gen1 [26,30) gap.
	mustIngest(t, c, Observation{"A", 0, 26, 30, 1, 25, 3})
	preview, err := c.Preview(34)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	assertStatus(t, preview, StatusUnverifiable)
	assertProblemKind(t, preview, ProblemGap)
	assertProblemKind(t, preview, ProblemContradiction)
	if got := c.PendingActionID(); got != 1 {
		t.Fatalf("pending action = %d, still want 1 before ack", got)
	}

	// Second pump at t=34: window 2 freezes as unverifiable, which breaks
	// the streak. No second scale-out is allowed while #1 is unacknowledged.
	ds = c.Pump(34)
	if len(ds) != 1 {
		t.Fatalf("pump@34: got %d decisions, want 1", len(ds))
	}
	assertStatus(t, ds[0].Verdict, StatusUnverifiable)
	if ds[0].Action != nil || ds[0].Blocked {
		t.Fatalf("unverifiable window must not produce an action, got %+v", ds[0])
	}
	actions := c.Actions()
	if len(actions) != 1 || actions[0].ID != 1 {
		t.Fatalf("actions = %+v, want only action #1", actions)
	}

	// A later correction arrives. Two pieces:
	//  1. rev 2 supersedes the early A1 [24,26) record (a true late update),
	//  2. the missing A1 [26,30) record finally arrives and fills the gap.
	// Window 2 is already frozen, so its verdict is not re-published, but
	// the corrected data is retained for every still-open use.
	if err := c.IngestObservation(Observation{"A", 1, 24, 26, 2, 31, 2}); err != nil {
		t.Fatalf("revision 2 ingest: %v", err)
	}
	mustIngest(t, c, Observation{"A", 1, 26, 30, 1, 28, 3})

	// ---- Window 3 [30,40): A 9/10, B 9/10 -> 90% high.
	mustIngest(t, c, Observation{"A", 1, 30, 40, 1, 90, 9})
	mustIngest(t, c, Observation{"B", 0, 30, 40, 1, 90, 9})
	// ---- Window 4 [40,50): A gen1 ends at 50; both 9/10 -> 90% high.
	mustIngest(t, c, Observation{"A", 1, 40, 50, 1, 91, 9})
	mustIngest(t, c, Observation{"B", 0, 40, 50, 1, 91, 9})

	// Third pump at t=44: window 3 is high (streak restarts at 1). No
	// action can be published anyway while action #1 lacks a receipt.
	ds = c.Pump(44)
	if len(ds) != 1 {
		t.Fatalf("pump@44: got %d decisions, want 1", len(ds))
	}
	assertStatus(t, ds[0].Verdict, StatusHigh)
	if ds[0].Action != nil {
		t.Fatalf("action must be withheld while #1 is unacknowledged, got %+v", ds[0].Action)
	}

	// Late receipt for action #1 at t=46. Cooldown [46,96).
	if err := c.Acknowledge(1, 46); err != nil {
		t.Fatalf("ack: %v", err)
	}
	if c.PendingActionID() != 0 {
		t.Fatalf("pending action should clear after ack")
	}
	if got := c.CooldownUntil(); got != 96 {
		t.Fatalf("cooldown until = %d, want 96", got)
	}
	// Re-acknowledging is rejected: the same action cannot be re-issued.
	if err := c.Acknowledge(1, 47); err == nil {
		t.Fatalf("duplicate ack must be rejected")
	}

	// Fourth pump at t=54 freezes window 4: the second consecutive high
	// makes scale-out due, but cooldown withholds it as the blocked decision.
	ds = c.Pump(54)
	if len(ds) != 1 {
		t.Fatalf("pump@54: got %d decisions, want 1", len(ds))
	}
	assertStatus(t, ds[0].Verdict, StatusHigh)
	if ds[0].Action != nil {
		t.Fatalf("action must be withheld during cooldown, got %+v", ds[0].Action)
	}
	if !ds[0].Blocked {
		t.Fatalf("window 4 decision should be marked blocked")
	}
	if b := c.BlockedDecision(); b == nil || b.Verdict.Window != 4 {
		t.Fatalf("blocked decision = %+v, want window 4", b)
	}
	if len(c.Actions()) != 1 {
		t.Fatalf("no new action may be issued during cooldown")
	}

	// ---- Window 5 [50,60): only B online, 3/10 -> 30% low.
	mustIngest(t, c, Observation{"B", 0, 50, 60, 1, 50, 3})

	// Pump t=64: the low window interrupts the high streak and clears the
	// parked scale-out; cooldown still blocks any action anyway.
	ds = c.Pump(64)
	assertStatus(t, ds[0].Verdict, StatusLow)
	if ds[0].Action != nil || ds[0].Blocked {
		t.Fatalf("first low window cannot trigger scale-in, got %+v", ds[0])
	}
	if c.BlockedDecision() != nil {
		t.Fatalf("opposite-pressure evidence must replace the blocked decision")
	}

	// A late correction for the already-frozen window 2 must not re-publish
	// it or re-emit action #1.
	mustIngest(t, c, Observation{"B", 0, 20, 30, 2, 71, 8})
	verdicts := c.FrozenVerdicts()
	if len(verdicts) != 6 {
		t.Fatalf("frozen verdicts = %d, want 6", len(verdicts))
	}
	assertStatus(t, verdicts[2], StatusUnverifiable)
	if len(c.Actions()) != 1 {
		t.Fatalf("late correction must not create actions: %+v", c.Actions())
	}

	// ---- Windows 6,7: only B online, both at 30% -> low, low.
	mustIngest(t, c, Observation{"B", 0, 60, 70, 1, 51, 3})
	mustIngest(t, c, Observation{"B", 0, 70, 80, 1, 52, 3})

	ds = c.Pump(84)
	if len(ds) != 2 {
		t.Fatalf("pump@84: got %d decisions, want 2", len(ds))
	}
	assertStatus(t, ds[0].Verdict, StatusLow)
	assertStatus(t, ds[1].Verdict, StatusLow)
	if ds[1].Action != nil {
		t.Fatalf("scale-in still inside cooldown, got action %+v", ds[1].Action)
	}
	if !ds[1].Blocked {
		t.Fatalf("window 7 scale-in must be blocked by cooldown")
	}

	// Pump inside cooldown: window 8 closes with every instance offline, so
	// it is published as unverifiable (and breaks streaks); the blocked
	// scale-in remains parked until the cooldown ends.
	ds = c.Pump(90)
	if len(ds) != 1 {
		t.Fatalf("pump@90 should publish window 8 only, got %d", len(ds))
	}
	assertStatus(t, ds[0].Verdict, StatusUnverifiable)
	assertProblemKind(t, ds[0].Verdict, ProblemNoOnline)
	if ds[0].Action != nil {
		t.Fatalf("no action may be emitted inside cooldown: %+v", ds[0].Action)
	}
	if c.BlockedDecision() == nil {
		t.Fatalf("blocked scale-in should survive the unverifiable window")
	}
	if len(c.Actions()) != 1 {
		t.Fatalf("still only action #1 before cooldown ends")
	}

	// First legal instant t=96: the blocked scale-in flushes as action #2.
	ds = c.Pump(96)
	if len(ds) != 1 || ds[0].Action == nil {
		t.Fatalf("pump@96 must flush the blocked action, got %+v", ds)
	}
	if !ds[0].Flushed {
		t.Fatalf("action #2 must be flagged as flushed")
	}
	if ds[0].Action.ID != 2 || ds[0].Action.Direction != ScaleIn || ds[0].Action.IssuedAt != 96 {
		t.Fatalf("unexpected flushed action: %+v", ds[0].Action)
	}
	if c.PendingActionID() != 2 || c.CooldownUntil() != 96 {
		t.Fatalf("after flush pending=%d cooldown=%d", c.PendingActionID(), c.CooldownUntil())
	}

	// Whole-sequence accounting: request totals are real increments.
	var totalReqs int64
	for _, v := range c.FrozenVerdicts() {
		totalReqs += v.Requests
	}
	if totalReqs <= 0 {
		t.Fatalf("request accounting should be positive, got %d", totalReqs)
	}
}

// TestMissingEvidenceBreaksStreak verifies that an unverifiable window in
// the middle of a pressure run resets the streak, so scale-out needs two
// fresh consecutive high windows afterwards.
func TestMissingEvidenceBreaksStreak(t *testing.T) {
	c := mustNew(t, DefaultConfig())
	if err := c.OnlineSpan("X", 0, 0, 40); err != nil {
		t.Fatal(err)
	}
	mustIngest(t, c, Observation{"X", 0, 0, 10, 1, 1, 9}) // 90% high
	// Window 1 [10,20) only has a partial record -> an uncovered gap.
	mustIngest(t, c, Observation{"X", 0, 10, 15, 1, 1, 5})
	mustIngest(t, c, Observation{"X", 0, 20, 30, 1, 1, 9}) // high again
	mustIngest(t, c, Observation{"X", 0, 30, 40, 1, 1, 9}) // high again

	ds := c.Pump(40)
	if len(ds) != 4 {
		t.Fatalf("got %d decisions, want 4", len(ds))
	}
	assertStatus(t, ds[0].Verdict, StatusHigh)
	assertStatus(t, ds[1].Verdict, StatusUnverifiable)
	assertProblemKind(t, ds[1].Verdict, ProblemGap)
	assertStatus(t, ds[2].Verdict, StatusHigh)
	assertStatus(t, ds[3].Verdict, StatusHigh)

	// Only the last two consecutive highs may trigger one scale-out.
	if ds[2].Action != nil {
		t.Fatalf("window 2 is the first high after the break, no action yet")
	}
	if ds[3].Action == nil || ds[3].Action.Direction != ScaleOut || ds[3].Action.ID != 1 {
		t.Fatalf("window 3 should emit action #1, got %+v", ds[3].Action)
	}
	high, low := c.Streaks()
	if high != 0 || low != 0 {
		t.Fatalf("streaks should reset after emitting, got high=%d low=%d", high, low)
	}
}

// TestOverlapAndGenerationContradiction checks overlap reporting and the
// rule that old-generation observations cannot cover new-generation time.
func TestOverlapAndGenerationContradiction(t *testing.T) {
	c := mustNew(t, DefaultConfig())
	if err := c.OnlineSpan("X", 0, 0, 10); err != nil {
		t.Fatal(err)
	}
	if err := c.OnlineSpan("X", 1, 10, 20); err != nil {
		t.Fatal(err)
	}
	// Two overlapping gen0 intervals inside window 0.
	mustIngest(t, c, Observation{"X", 0, 0, 6, 1, 1, 5})
	mustIngest(t, c, Observation{"X", 0, 5, 10, 1, 1, 4})
	// Gen0 record claiming time after the restart.
	mustIngest(t, c, Observation{"X", 0, 12, 16, 1, 1, 3})
	// Gen1 only partially covers its online period -> gap as well.
	mustIngest(t, c, Observation{"X", 1, 10, 16, 1, 1, 5})

	ds := c.Pump(20)
	assertStatus(t, ds[0].Verdict, StatusUnverifiable)
	assertProblemKind(t, ds[0].Verdict, ProblemOverlap)
	assertStatus(t, ds[1].Verdict, StatusUnverifiable)
	assertProblemKind(t, ds[1].Verdict, ProblemContradiction)
	assertProblemKind(t, ds[1].Verdict, ProblemGap)
}

// TestWatermarkBoundaries pins the inclusive boundary semantics: exactly
// the high watermark is high, exactly the low watermark is low.
func TestWatermarkBoundaries(t *testing.T) {
	c := mustNew(t, DefaultConfig())
	if err := c.OnlineSpan("X", 0, 0, 20); err != nil {
		t.Fatal(err)
	}
	// Window 0: busy 8/10 = 800 permille -> high.
	mustIngest(t, c, Observation{"X", 0, 0, 10, 1, 1, 8})
	// Window 1: busy 3/10 = 300 permille -> low.
	mustIngest(t, c, Observation{"X", 0, 10, 20, 1, 1, 3})

	ds := c.Pump(20)
	assertStatus(t, ds[0].Verdict, StatusHigh)
	if ds[0].Verdict.UtilizationPermille != 800 {
		t.Fatalf("util = %d, want 800", ds[0].Verdict.UtilizationPermille)
	}
	assertStatus(t, ds[1].Verdict, StatusLow)
	if ds[1].Verdict.UtilizationPermille != 300 {
		t.Fatalf("util = %d, want 300", ds[1].Verdict.UtilizationPermille)
	}
}

// TestRevisionRules covers stale/conflicting revisions and a valid revision
// superseding an earlier record before the window is published.
func TestRevisionRules(t *testing.T) {
	c := mustNew(t, DefaultConfig())
	if err := c.OnlineSpan("X", 0, 0, 10); err != nil {
		t.Fatal(err)
	}
	mustIngest(t, c, Observation{"X", 0, 0, 10, 1, 1, 2}) // 20% low

	if err := c.IngestObservation(Observation{"X", 0, 0, 10, 1, 2, 3}); err == nil {
		t.Fatal("equal revision must be rejected")
	}
	if err := c.IngestObservation(Observation{"X", 0, 0, 10, 0, 1, 1}); err == nil {
		t.Fatal("revision 0 must be rejected")
	}

	// Valid correction before pumping: 90% busy now.
	mustIngest(t, c, Observation{"X", 0, 0, 10, 2, 2, 9})
	if err := c.IngestObservation(Observation{"X", 0, 0, 10, 1, 3, 9}); err == nil {
		t.Fatal("stale revision must be rejected")
	}

	ds := c.Pump(10)
	assertStatus(t, ds[0].Verdict, StatusHigh)
	if ds[0].Verdict.Busy != 9 || ds[0].Verdict.Requests != 2 {
		t.Fatalf("corrected totals = busy %d req %d, want 9/2",
			ds[0].Verdict.Busy, ds[0].Verdict.Requests)
	}
}

// TestAcknowledgeRules checks unknown/back-dated receipts and that cooldown
// does not begin until a valid acknowledgement is seen.
func TestAcknowledgeRules(t *testing.T) {
	c := mustNew(t, DefaultConfig())
	if err := c.OnlineSpan("X", 0, 0, 20); err != nil {
		t.Fatal(err)
	}
	mustIngest(t, c, Observation{"X", 0, 0, 10, 1, 1, 9})
	mustIngest(t, c, Observation{"X", 0, 10, 20, 1, 1, 9})
	ds := c.Pump(20)
	if ds[1].Action == nil || ds[1].Action.ID != 1 {
		t.Fatalf("expected action #1, got %+v", ds[1].Action)
	}
	if err := c.Acknowledge(99, 25); err == nil {
		t.Fatal("unknown action id must be rejected")
	}
	if err := c.Acknowledge(1, 15); err == nil {
		t.Fatal("receipt before issue time must be rejected")
	}
	if c.CooldownUntil() != 0 {
		t.Fatal("cooldown must not start before a valid ack")
	}
	if err := c.Acknowledge(1, 30); err != nil {
		t.Fatalf("valid ack: %v", err)
	}
	if c.CooldownUntil() != 80 {
		t.Fatalf("cooldown until = %d, want 80", c.CooldownUntil())
	}
}

// TestScaleInRequiresThreeLows pins the scale-in side: two low windows do
// nothing, the third consecutive low publishes one scale-in action.
func TestScaleInRequiresThreeLows(t *testing.T) {
	c := mustNew(t, DefaultConfig())
	if err := c.OnlineSpan("X", 0, 0, 30); err != nil {
		t.Fatal(err)
	}
	for i := int64(0); i < 3; i++ {
		mustIngest(t, c, Observation{"X", 0, i * 10, i*10 + 10, 1, i + 1, 3}) // 30% low
	}

	ds := c.Pump(20)
	if len(ds) != 2 || ds[0].Action != nil || ds[1].Action != nil {
		t.Fatalf("first two low windows must not act: %+v", ds)
	}
	ds = c.Pump(30)
	if len(ds) != 1 || ds[0].Action == nil {
		t.Fatalf("third low window must emit an action, got %+v", ds)
	}
	if ds[0].Action.Direction != ScaleIn || ds[0].Action.ID != 1 || ds[0].Action.IssuedAt != 30 {
		t.Fatalf("unexpected action: %+v", ds[0].Action)
	}
	if c.PendingActionID() != 1 || c.CooldownUntil() != 0 {
		t.Fatalf("cooldown must not start before ack: pending=%d cooldown=%d",
			c.PendingActionID(), c.CooldownUntil())
	}
	if err := c.Acknowledge(1, 35); err != nil {
		t.Fatalf("ack: %v", err)
	}
	if c.CooldownUntil() != 85 {
		t.Fatalf("cooldown until = %d, want 85", c.CooldownUntil())
	}
}

// TestLateCorrectionFillsUnpublishedWindow proves that a correction arriving
// after the window closed but before any Pump can complete the evidence:
// Preview first reports a gap, the late record then makes the same window
// verifiable, and Pump freezes the corrected verdict.
func TestLateCorrectionFillsUnpublishedWindow(t *testing.T) {
	c := mustNew(t, DefaultConfig())
	if err := c.OnlineSpan("X", 0, 0, 10); err != nil {
		t.Fatal(err)
	}
	// Only [0,6) is reported initially; [6,10) is missing.
	mustIngest(t, c, Observation{"X", 0, 0, 6, 1, 6, 6})

	preview, err := c.Preview(10)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	assertStatus(t, preview, StatusUnverifiable)
	assertProblemKind(t, preview, ProblemGap)

	// Late piece completes the non-overlapping coverage: busy 9/10 -> high.
	mustIngest(t, c, Observation{"X", 0, 6, 10, 1, 4, 3})
	preview, err = c.Preview(10)
	if err != nil {
		t.Fatalf("preview after correction: %v", err)
	}
	assertStatus(t, preview, StatusHigh)
	if preview.Busy != 9 || preview.Available != 10 || preview.UtilizationPermille != 900 {
		t.Fatalf("corrected window totals wrong: %+v", preview)
	}

	ds := c.Pump(10)
	if len(ds) != 1 || ds[0].Action != nil {
		t.Fatalf("single high window must not emit an action, got %+v", ds)
	}
	assertStatus(t, ds[0].Verdict, StatusHigh)
}

// TestConcurrentInterleaving exercises the mutex under concurrent ingest,
// lifecycle registration, preview and pump interleavings; every frozen
// window must remain internally consistent with no torn state.
func TestConcurrentInterleaving(t *testing.T) {
	c := mustNew(t, DefaultConfig())

	var wg sync.WaitGroup
	// Two instance generations come and go concurrently with observation
	// ingestion. Lifecycle spans overlap only per distinct (instance, gen).
	for _, g := range []struct {
		instance string
		gen      int
		from, to int64
	}{
		{"X", 0, 0, 200},
		{"Y", 0, 0, 200},
	} {
		g := g
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := c.OnlineSpan(g.instance, g.gen, g.from, g.to); err != nil {
				t.Errorf("online span: %v", err)
			}
		}()
	}
	for i := int64(0); i < 20; i++ {
		for _, instance := range []string{"X", "Y"} {
			wg.Add(1)
			go func(instance string, i int64) {
				defer wg.Done()
				start := i * 10
				o := Observation{
					Instance: instance, Gen: 0, Start: start, End: start + 10,
					Rev: 1, Requests: i + 1, Busy: 5,
				}
				if err := c.IngestObservation(o); err != nil {
					t.Errorf("ingest %s %d: %v", instance, i, err)
				}
			}(instance, i)
		}
	}
	// Concurrently read-only previews must never observe torn structures.
	wg.Add(1)
	go func() {
		defer wg.Done()
		for at := int64(10); at <= 200; at += 10 {
			_, _ = c.Preview(at)
			_, _ = c.Streaks()
		}
	}()
	wg.Wait()

	// Deterministic pump after all inputs landed.
	ds := c.Pump(200)
	if len(ds) != 20 {
		t.Fatalf("frozen windows = %d, want 20", len(ds))
	}
	for _, d := range ds {
		v := d.Verdict
		// Two instances, each available 10 ticks, each busy 5 -> 10/20.
		if v.Available != 20 || v.Busy != 10 || v.UtilizationPermille != 500 {
			t.Fatalf("window %d inconsistent: %+v", v.Window, v)
		}
		if v.Status != StatusNormal {
			t.Fatalf("window %d status = %s, want normal", v.Window, v.Status)
		}
	}
	var reqs int64
	for _, d := range ds {
		reqs += d.Verdict.Requests
	}
	if reqs != 2*210 { // two instances, each sum(1..20)
		t.Fatalf("total requests = %d, want %d", reqs, 2*210)
	}
	// Repeated pumps at the same instant must not double-publish.
	if extra := c.Pump(200); len(extra) != 0 {
		t.Fatalf("re-pump returned %d stale decisions", len(extra))
	}
	if id := c.PendingActionID(); id != 0 {
		t.Fatalf("normal-pressure run must leave no pending action, got %d", id)
	}
}
