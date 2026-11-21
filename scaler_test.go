package scaler

import (
	"errors"
	"testing"
	"time"
)

// fakeClock is a manually advanced clock for deterministic window tests.
type fakeClock struct {
	now time.Time
}

func (f *fakeClock) Now() time.Time          { return f.now }
func (f *fakeClock) Advance(d time.Duration) { f.now = f.now.Add(d) }

func at(clk *fakeClock) time.Time { return clk.Now() }

func intPtr(v int) *int { return &v }

func newTestController(t *testing.T, spec Spec, clk *fakeClock) *Controller {
	t.Helper()
	c, err := New(spec, WithClock(clk.Now))
	if err != nil {
		t.Fatalf("New(%+v): %v", spec, err)
	}
	return c
}

func defaultSpec() Spec {
	return Spec{MinReplicas: 0, MaxReplicas: 10, TargetPerReplica: 10}
}

func mustApply(t *testing.T, c *Controller, s Sample) Result {
	t.Helper()
	res, err := c.Apply(s)
	if err != nil {
		t.Fatalf("Apply(%+v): %v", s, err)
	}
	return res
}

func mustReady(t *testing.T, c *Controller, ids ...string) {
	t.Helper()
	for _, id := range ids {
		if err := c.MarkReady(id); err != nil {
			t.Fatalf("MarkReady(%q): %v", id, err)
		}
	}
}

func actionSummary(actions []Action) []string {
	out := make([]string, 0, len(actions))
	for _, a := range actions {
		out = append(out, string(a.Kind)+":"+a.ReplicaID)
	}
	return out
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// TestScaleUpIsImmediateAndCountsPendingReplicas covers the acceptable path:
// a single high sample creates the exact difference at once, and replicas that
// were requested but are not ready yet already occupy the target.
func TestScaleUpIsImmediateAndCountsPendingReplicas(t *testing.T) {
	clk := &fakeClock{now: time.Unix(1000, 0)}
	c := newTestController(t, defaultSpec(), clk)

	res := mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(35)})
	if got, want := res.Target, 4; got != want {
		t.Fatalf("target = %d, want %d", got, want)
	}
	want := []string{"create:replica-1", "create:replica-2", "create:replica-3", "create:replica-4"}
	if got := actionSummary(res.Actions); !equalStrings(got, want) {
		t.Fatalf("actions = %v, want %v", got, want)
	}
	if res.Assigned != 4 {
		t.Fatalf("assigned = %d, want 4", res.Assigned)
	}

	counts := c.Counts()
	if counts.Pending != 4 || counts.Ready != 0 || counts.Assigned != 4 {
		t.Fatalf("counts = %+v, want 4 pending / 4 assigned", counts)
	}

	// Nothing is ready yet, but the pending replicas already count towards the
	// target: a second identical sample must not create more replicas.
	res = mustApply(t, c, Sample{ID: "s2", At: at(clk), Backlog: intPtr(35)})
	if len(res.Actions) != 0 {
		t.Fatalf("second sample produced actions %v, want none", actionSummary(res.Actions))
	}
	if res.Assigned != 4 {
		t.Fatalf("assigned after second sample = %d, want 4", res.Assigned)
	}

	// Only readiness acknowledgements change state; no extra create intents.
	// Backlog 39 still yields target 4, so readiness alone must not scale.
	mustReady(t, c, "replica-1", "replica-2")
	// The same readiness acknowledgement may be redelivered: it must be a
	// no-op rather than an error or an extra replica.
	if err := c.MarkReady("replica-1"); err != nil {
		t.Fatalf("duplicate MarkReady(replica-1): %v", err)
	}
	if err := c.MarkReady("replica-1"); err != nil {
		t.Fatalf("second duplicate MarkReady(replica-1): %v", err)
	}
	if got := c.Counts(); got.Ready != 2 || got.Pending != 2 || got.Assigned != 4 {
		t.Fatalf("duplicate acks changed state: %+v", got)
	}
	res = mustApply(t, c, Sample{ID: "s3", At: at(clk), Backlog: intPtr(39)})
	if len(res.Actions) != 0 {
		t.Fatalf("third sample produced actions %v, want none", actionSummary(res.Actions))
	}
	if got := c.Counts(); got.Ready != 2 || got.Pending != 2 || got.Assigned != 4 {
		t.Fatalf("counts = %+v, want 2 ready / 2 pending", got)
	}

	// Once the target genuinely exceeds the assigned replicas, only the
	// difference is requested.
	res = mustApply(t, c, Sample{ID: "s4", At: at(clk), Backlog: intPtr(41)})
	if got := actionSummary(res.Actions); !equalStrings(got, []string{"create:replica-5"}) {
		t.Fatalf("actions = %v, want create replica-5 only", got)
	}
}

// TestCeilingAndBounds verifies ceiling division and clamping at both ends.
func TestCeilingAndBounds(t *testing.T) {
	cases := []struct {
		name    string
		spec    Spec
		backlog int
		want    int
	}{
		{"zero backlog keeps min", Spec{MinReplicas: 1, MaxReplicas: 5, TargetPerReplica: 10}, 0, 1},
		{"one rounds up", Spec{MinReplicas: 1, MaxReplicas: 5, TargetPerReplica: 10}, 1, 1},
		{"exact division", Spec{MinReplicas: 1, MaxReplicas: 5, TargetPerReplica: 10}, 10, 1},
		{"just above division rounds up", Spec{MinReplicas: 1, MaxReplicas: 5, TargetPerReplica: 10}, 11, 2},
		{"clamped to max", Spec{MinReplicas: 1, MaxReplicas: 5, TargetPerReplica: 10}, 1_000, 5},
		{"min above zero", Spec{MinReplicas: 3, MaxReplicas: 5, TargetPerReplica: 10}, 0, 3},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			clk := &fakeClock{now: time.Unix(1000, 0)}
			c := newTestController(t, tc.spec, clk)
			res := mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(tc.backlog)})
			if res.Target != tc.want {
				t.Fatalf("target for backlog %d = %d, want %d", tc.backlog, res.Target, tc.want)
			}
			counts := c.Counts()
			if counts.Assigned != tc.want {
				t.Fatalf("assigned = %d, want %d", counts.Assigned, tc.want)
			}
		})
	}
}

// TestMissingBacklogKeepsScale covers the "no measurement" branch: no action,
// no window entry, no scale change in either direction.
func TestMissingBacklogKeepsScale(t *testing.T) {
	clk := &fakeClock{now: time.Unix(1000, 0)}
	c := newTestController(t, defaultSpec(), clk)

	mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(30)})
	mustReady(t, c, "replica-1", "replica-2", "replica-3")

	clk.Advance(5 * time.Second)
	res := mustApply(t, c, Sample{ID: "s2", At: at(clk), Backlog: nil})
	if res.Observed {
		t.Fatal("missing backlog reported as observed")
	}
	if len(res.Actions) != 0 {
		t.Fatalf("missing backlog produced actions %v", actionSummary(res.Actions))
	}
	if res.Assigned != 3 {
		t.Fatalf("assigned = %d, want 3", res.Assigned)
	}
	if res.WindowSamples != 1 || res.WindowMax != 3 {
		t.Fatalf("missing backlog changed the window: samples=%d max=%d, want 1/3",
			res.WindowSamples, res.WindowMax)
	}

	// Even a low target must not shrink the fleet while only the old sample is
	// visible, because the missing sample does not lower the window maximum.
	clk.Advance(1 * time.Second)
	res = mustApply(t, c, Sample{ID: "s3", At: at(clk), Backlog: intPtr(1)})
	if len(res.Actions) != 0 {
		t.Fatalf("low target inside window produced actions %v", actionSummary(res.Actions))
	}
	if c.Counts().Assigned != 3 {
		t.Fatalf("assigned = %d, want 3", c.Counts().Assigned)
	}

	// After the window empties, a missing measurement still must not shrink the
	// fleet: maintain means no action even though a scale-down would be due.
	clk.Advance(ScaleDownWindow)
	res = mustApply(t, c, Sample{ID: "s4", At: at(clk), Backlog: nil})
	if res.Observed || len(res.Actions) != 0 {
		t.Fatalf("missing backlog after window expiry produced %+v", res)
	}
	if res.WindowSamples != 0 || res.WindowMax != -1 {
		t.Fatalf("window = %d/%d, want empty", res.WindowSamples, res.WindowMax)
	}
	if c.Counts().Assigned != 3 {
		t.Fatalf("assigned = %d, want 3", c.Counts().Assigned)
	}
}

// TestScaleDownUsesHighestWindowTargetAndExactBoundary verifies the scale-down
// window, including that a sample exactly 60s old has already left it.
func TestScaleDownUsesHighestWindowTargetAndExactBoundary(t *testing.T) {
	const window = ScaleDownWindow

	t.Run("sample exactly 60s old is outside the window", func(t *testing.T) {
		clk := &fakeClock{now: time.Unix(1000, 0)}
		c := newTestController(t, defaultSpec(), clk)
		mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(50)})
		mustReady(t, c, "replica-1", "replica-2", "replica-3", "replica-4", "replica-5")

		clk.Advance(window)
		res := mustApply(t, c, Sample{ID: "s2", At: at(clk), Backlog: intPtr(20)})
		if res.WindowSamples != 1 {
			t.Fatalf("window samples = %d, want 1 (the 60s old sample must have expired)", res.WindowSamples)
		}
		want := []string{"terminate:replica-1", "terminate:replica-2", "terminate:replica-3"}
		if got := actionSummary(res.Actions); !equalStrings(got, want) {
			t.Fatalf("actions = %v, want %v", got, want)
		}
		if res.Assigned != 2 || c.Counts().Terminating != 3 {
			t.Fatalf("assigned=%d terminating=%d, want assigned=2 terminating=3", res.Assigned, c.Counts().Terminating)
		}
	})

	t.Run("sample 1ns younger than the window is still inside", func(t *testing.T) {
		clk := &fakeClock{now: time.Unix(1000, 0)}
		c := newTestController(t, defaultSpec(), clk)
		mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(50)})
		mustReady(t, c, "replica-1", "replica-2", "replica-3", "replica-4", "replica-5")

		clk.Advance(window - time.Nanosecond)
		res := mustApply(t, c, Sample{ID: "s2", At: at(clk), Backlog: intPtr(20)})
		if res.WindowSamples != 2 || res.WindowMax != 5 {
			t.Fatalf("window samples=%d max=%d, want 2/5", res.WindowSamples, res.WindowMax)
		}
		if len(res.Actions) != 0 {
			t.Fatalf("actions = %v, want none: old high target still holds the scale", actionSummary(res.Actions))
		}
		if c.Counts().Assigned != 5 {
			t.Fatalf("assigned = %d, want 5", c.Counts().Assigned)
		}
	})

	t.Run("highest target in window wins over current target", func(t *testing.T) {
		clk := &fakeClock{now: time.Unix(1000, 0)}
		c := newTestController(t, defaultSpec(), clk)
		mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(20)}) // target 2
		clk.Advance(10 * time.Second)
		mustApply(t, c, Sample{ID: "s2", At: at(clk), Backlog: intPtr(60)}) // target 6
		mustReady(t, c, "replica-1", "replica-2", "replica-3", "replica-4", "replica-5", "replica-6")

		clk.Advance(10 * time.Second)
		res := mustApply(t, c, Sample{ID: "s3", At: at(clk), Backlog: intPtr(10)}) // target 1
		if res.WindowMax != 6 {
			t.Fatalf("window max = %d, want 6", res.WindowMax)
		}
		if len(res.Actions) != 0 {
			t.Fatalf("actions = %v, want none (window max 6 equals assigned 6)", actionSummary(res.Actions))
		}

		// 60s after the target-6 sample, that sample expires and the window max
		// drops to 1, allowing the scale-down.
		clk.Advance(50 * time.Second)
		res = mustApply(t, c, Sample{ID: "s4", At: at(clk), Backlog: intPtr(10)})
		if res.WindowMax != 1 {
			t.Fatalf("window max = %d, want 1 after expiry", res.WindowMax)
		}
		if len(res.Actions) != 5 {
			t.Fatalf("actions = %v, want 5 terminations", actionSummary(res.Actions))
		}
	})
}

// TestScaleDownCancelsNewestPendingThenReadyAscending covers the ordering
// constraint for shrinking: pending replicas are cancelled newest-first, then
// ready replicas are terminated in ascending replica-number order.
func TestScaleDownCancelsNewestPendingThenReadyAscending(t *testing.T) {
	clk := &fakeClock{now: time.Unix(1000, 0)}
	c := newTestController(t, defaultSpec(), clk)

	mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(40)}) // target 4
	mustReady(t, c, "replica-1", "replica-2")
	// replicas 3 and 4 stay pending; 4 was requested after 3.

	clk.Advance(ScaleDownWindow + time.Second)
	res := mustApply(t, c, Sample{ID: "s2", At: at(clk), Backlog: intPtr(10)}) // target 1

	want := []string{
		"cancel_create:replica-4",
		"cancel_create:replica-3",
		"terminate:replica-1",
	}
	if got := actionSummary(res.Actions); !equalStrings(got, want) {
		t.Fatalf("actions = %v, want %v", got, want)
	}
	if res.Assigned != 1 {
		t.Fatalf("assigned = %d, want 1", res.Assigned)
	}
	counts := c.Counts()
	if counts.Ready != 1 || counts.Pending != 0 || counts.Terminating != 1 || counts.Assigned != 1 {
		t.Fatalf("counts = %+v, want 1 ready / 1 terminating", counts)
	}
	views := c.Replicas()
	if len(views) != 2 || views[0].ID != "replica-1" || views[0].State != ReplicaTerminating {
		t.Fatalf("replicas = %+v, want replica-1 terminating plus replica-2 ready", views)
	}
	if views[1].ID != "replica-2" || views[1].State != ReplicaReady {
		t.Fatalf("replicas = %+v, want replica-2 ready", views)
	}
}

// TestTerminatingReplicasLeaveComputation verifies that a replica with a
// termination intent is not counted, not terminated twice, and cannot become
// ready again.
func TestTerminatingReplicasLeaveComputation(t *testing.T) {
	clk := &fakeClock{now: time.Unix(1000, 0)}
	c := newTestController(t, defaultSpec(), clk)

	mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(30)})
	mustReady(t, c, "replica-1", "replica-2", "replica-3")

	clk.Advance(ScaleDownWindow + time.Second)
	res := mustApply(t, c, Sample{ID: "s2", At: at(clk), Backlog: intPtr(10)}) // target 1
	if got := actionSummary(res.Actions); !equalStrings(got, []string{"terminate:replica-1", "terminate:replica-2"}) {
		t.Fatalf("actions = %v, want two terminations", got)
	}
	if res.Assigned != 1 {
		t.Fatalf("assigned = %d, want 1", res.Assigned)
	}

	// A lost readiness acknowledgement for an already terminating replica must
	// not resurrect it or produce any action.
	if err := c.MarkReady("replica-1"); err != nil {
		t.Fatalf("MarkReady on terminating replica: %v", err)
	}
	if got := c.Counts(); got.Ready != 1 || got.Terminating != 2 || got.Assigned != 1 {
		t.Fatalf("counts = %+v, want 1 ready / 2 terminating / assigned 1", got)
	}

	// The same scale-down condition again must not re-terminate replica-1/2.
	clk.Advance(time.Second)
	res = mustApply(t, c, Sample{ID: "s3", At: at(clk), Backlog: intPtr(10)})
	if len(res.Actions) != 0 {
		t.Fatalf("actions = %v, want none", actionSummary(res.Actions))
	}
}

// TestDuplicateSampleReplayIsIdempotent covers replay of one sample: no new
// replica identities may appear anywhere, including the action log.
func TestDuplicateSampleReplayIsIdempotent(t *testing.T) {
	clk := &fakeClock{now: time.Unix(1000, 0)}
	c := newTestController(t, defaultSpec(), clk)

	first := mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(35)})
	if len(first.Actions) != 4 {
		t.Fatalf("first apply actions = %v, want 4 creates", actionSummary(first.Actions))
	}
	logBefore := c.Actions()
	repsBefore := c.Replicas()

	// Replay the very same sample (and one with a conflicting backlog under the
	// same ID) - both must be ignored.
	for _, backlog := range []int{35, 999} {
		res := mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(backlog)})
		if !res.Duplicate {
			t.Fatal("replayed sample not reported as duplicate")
		}
		if len(res.Actions) != 0 {
			t.Fatalf("replayed sample produced actions %v", actionSummary(res.Actions))
		}
		if res.Assigned != 4 {
			t.Fatalf("assigned after replay = %d, want 4", res.Assigned)
		}
	}

	if got := c.Actions(); len(got) != len(logBefore) {
		t.Fatalf("action log grew from %d to %d on replay", len(logBefore), len(got))
	}
	if got := c.Replicas(); len(got) != len(repsBefore) {
		t.Fatalf("replica set changed on replay: %d -> %d", len(repsBefore), len(got))
	}
	seen := map[string]bool{}
	for _, a := range c.Actions() {
		if seen[a.ReplicaID] && a.Kind == ActionCreate {
			t.Fatalf("duplicate create for %q in action log", a.ReplicaID)
		}
		seen[a.ReplicaID] = true
	}
	for _, r := range repsBefore {
		if r.ID == "replica-5" {
			t.Fatalf("replay allocated a new replica identity %q", r.ID)
		}
	}
}

// TestCreateFailureReleasesReservation covers a failed create: the placeholder
// is withdrawn, the action log records it, and a later sample allocates a fresh
// identifier instead of reusing the withdrawn one.
func TestCreateFailureReleasesReservation(t *testing.T) {
	clk := &fakeClock{now: time.Unix(1000, 0)}
	c := newTestController(t, defaultSpec(), clk)

	mustApply(t, c, Sample{ID: "s1", At: at(clk), Backlog: intPtr(20)}) // target 2
	if got := c.Counts(); got.Pending != 2 || got.Assigned != 2 {
		t.Fatalf("counts = %+v, want 2 pending", got)
	}

	if err := c.FailCreate("replica-2"); err != nil {
		t.Fatalf("FailCreate: %v", err)
	}
	if got := c.Counts(); got.Pending != 1 || got.Assigned != 1 {
		t.Fatalf("counts after failed create = %+v, want 1 pending", got)
	}
	log := c.Actions()
	last := log[len(log)-1]
	if last.Kind != ActionCancel || last.ReplicaID != "replica-2" {
		t.Fatalf("last action = %+v, want cancel_create replica-2", last)
	}

	// The withdrawn replica must not be resurrected by an ack or a second
	// failure report.
	if err := c.MarkReady("replica-2"); !errors.Is(err, ErrUnknownReplica) {
		t.Fatalf("MarkReady(withdrawn) error = %v, want ErrUnknownReplica", err)
	}
	if err := c.FailCreate("replica-2"); !errors.Is(err, ErrUnknownReplica) {
		t.Fatalf("FailCreate(withdrawn) error = %v, want ErrUnknownReplica", err)
	}

	// The next sample reconciles the missing slot with a brand-new identity.
	res := mustApply(t, c, Sample{ID: "s2", At: at(clk), Backlog: intPtr(20)})
	if got := actionSummary(res.Actions); !equalStrings(got, []string{"create:replica-3"}) {
		t.Fatalf("actions = %v, want create replica-3", got)
	}
	if res.Assigned != 2 {
		t.Fatalf("assigned = %d, want 2", res.Assigned)
	}

	// A ready replica cannot fail its create, and unknown replicas are rejected.
	mustReady(t, c, "replica-1")
	if err := c.FailCreate("replica-1"); !errors.Is(err, ErrInvalidTransition) {
		t.Fatalf("FailCreate(ready) error = %v, want ErrInvalidTransition", err)
	}
	if err := c.FailCreate("replica-99"); !errors.Is(err, ErrUnknownReplica) {
		t.Fatalf("FailCreate(unknown) error = %v, want ErrUnknownReplica", err)
	}
}

// TestStaleMeasurementIsIgnored covers a backdated sample: a measurement whose
// timestamp is already older than the window can neither enter the window nor
// trigger scale-up.
func TestStaleMeasurementIsIgnored(t *testing.T) {
	clk := &fakeClock{now: time.Unix(2000, 0)}
	c := newTestController(t, defaultSpec(), clk)

	stale := at(clk).Add(-ScaleDownWindow) // exactly 60s old: outside the window
	res := mustApply(t, c, Sample{ID: "old", At: stale, Backlog: intPtr(100)})
	if !res.Stale || res.Observed {
		t.Fatalf("stale sample = %+v, want Stale and not Observed", res)
	}
	if res.Target != -1 || res.WindowSamples != 0 {
		t.Fatalf("stale sample entered computation: target=%d window=%d", res.Target, res.WindowSamples)
	}
	if len(res.Actions) != 0 || c.Counts().Assigned != 0 {
		t.Fatalf("stale sample changed scale: actions=%v assigned=%d",
			actionSummary(res.Actions), c.Counts().Assigned)
	}

	// A stale sample must not poison the window for later fresh samples either.
	res = mustApply(t, c, Sample{ID: "new", At: at(clk), Backlog: intPtr(20)})
	if res.WindowMax != 2 || res.WindowSamples != 1 {
		t.Fatalf("window = %d/%d, want 2/1", res.WindowMax, res.WindowSamples)
	}
	if got := actionSummary(res.Actions); !equalStrings(got, []string{"create:replica-1", "create:replica-2"}) {
		t.Fatalf("actions = %v, want two creates", got)
	}
}

// TestInvalidInputsAreRejected covers the blocked/rejected branches.
func TestInvalidInputsAreRejected(t *testing.T) {
	badSpecs := []Spec{
		{MinReplicas: 0, MaxReplicas: 3, TargetPerReplica: 0},
		{MinReplicas: 0, MaxReplicas: 3, TargetPerReplica: -1},
		{MinReplicas: -1, MaxReplicas: 3, TargetPerReplica: 10},
		{MinReplicas: 5, MaxReplicas: 3, TargetPerReplica: 10},
	}
	for _, spec := range badSpecs {
		if _, err := New(spec); !errors.Is(err, ErrInvalidSpec) {
			t.Fatalf("New(%+v) error = %v, want ErrInvalidSpec", spec, err)
		}
	}

	clk := &fakeClock{now: time.Unix(1000, 0)}
	c := newTestController(t, defaultSpec(), clk)
	if _, err := c.Apply(Sample{ID: "", At: at(clk), Backlog: intPtr(1)}); !errors.Is(err, ErrMissingSampleID) {
		t.Fatalf("empty sample id error = %v, want ErrMissingSampleID", err)
	}
	if _, err := c.Apply(Sample{ID: "neg", At: at(clk), Backlog: intPtr(-1)}); !errors.Is(err, ErrInvalidBacklog) {
		t.Fatalf("negative backlog error = %v, want ErrInvalidBacklog", err)
	}
	if got := c.Counts().Assigned; got != 0 {
		t.Fatalf("rejected samples changed state: assigned=%d", got)
	}
	if err := c.MarkReady("nope"); !errors.Is(err, ErrUnknownReplica) {
		t.Fatalf("MarkReady(unknown) error = %v, want ErrUnknownReplica", err)
	}
}

// TestDuplicateScaleDownReplayIsIdempotent is the shrinking counterpart of the
// create replay test: redelivering the sample that caused terminations must not
// terminate further replicas or introduce new replica numbers.
func TestDuplicateScaleDownReplayIsIdempotent(t *testing.T) {
	clk := &fakeClock{now: time.Unix(1000, 0)}
	c := newTestController(t, defaultSpec(), clk)

	mustApply(t, c, Sample{ID: "up", At: at(clk), Backlog: intPtr(50)}) // target 5
	mustReady(t, c, "replica-1", "replica-2", "replica-3", "replica-4", "replica-5")

	clk.Advance(ScaleDownWindow + time.Second)
	first := mustApply(t, c, Sample{ID: "down", At: at(clk), Backlog: intPtr(10)}) // target 1
	if got := actionSummary(first.Actions); !equalStrings(got,
		[]string{"terminate:replica-1", "terminate:replica-2", "terminate:replica-3", "terminate:replica-4"}) {
		t.Fatalf("actions = %v, want 4 terminations", got)
	}

	logBefore := c.Actions()
	repsBefore := c.Replicas()
	for i := 0; i < 2; i++ {
		res := mustApply(t, c, Sample{ID: "down", At: at(clk), Backlog: intPtr(0)})
		if !res.Duplicate || len(res.Actions) != 0 {
			t.Fatalf("replay #%d = %+v, want duplicate with no actions", i+1, res)
		}
		if res.Assigned != 1 {
			t.Fatalf("replay #%d assigned = %d, want 1", i+1, res.Assigned)
		}
	}
	if got := c.Actions(); len(got) != len(logBefore) {
		t.Fatalf("action log grew on replay: %d -> %d", len(logBefore), len(got))
	}
	if got := c.Replicas(); len(got) != len(repsBefore) {
		t.Fatalf("replica set changed on replay: %d -> %d", len(repsBefore), len(got))
	}
	if got := c.Counts(); got.Terminating != 4 || got.Ready != 1 || got.Assigned != 1 {
		t.Fatalf("counts = %+v, want 4 terminating / 1 ready", got)
	}
}

// TestActionLogIsAppendOnlyAndStable verifies the full history: one create per
// replica identity, no identity reused after withdrawal or termination, and no
// growth from replayed samples.
func TestActionLogIsAppendOnlyAndStable(t *testing.T) {
	clk := &fakeClock{now: time.Unix(1000, 0)}
	c := newTestController(t, Spec{MinReplicas: 0, MaxReplicas: 6, TargetPerReplica: 10}, clk)

	mustApply(t, c, Sample{ID: "a", At: at(clk), Backlog: intPtr(30)}) // 1,2,3
	if err := c.FailCreate("replica-2"); err != nil {
		t.Fatalf("FailCreate: %v", err)
	}
	mustReady(t, c, "replica-1", "replica-3")
	mustApply(t, c, Sample{ID: "b", At: at(clk), Backlog: intPtr(30)}) // creates 4
	mustReady(t, c, "replica-4")

	clk.Advance(ScaleDownWindow + time.Second)
	mustApply(t, c, Sample{ID: "c", At: at(clk), Backlog: intPtr(10)}) // target 1, keeps 1
	mustApply(t, c, Sample{ID: "d", At: at(clk), Backlog: intPtr(30)}) // target 3, creates 5,6

	created := map[string]int{}
	for _, a := range c.Actions() {
		if a.Kind == ActionCreate {
			created[a.ReplicaID]++
		}
	}
	for id, n := range created {
		if n != 1 {
			t.Fatalf("replica %q has %d create actions, want exactly 1", id, n)
		}
	}
	if len(created) != 6 {
		t.Fatalf("created identities = %v, want six distinct identities", created)
	}
	if got := c.Counts().Assigned; got != 3 {
		t.Fatalf("assigned = %d, want 3", got)
	}

	// Replaying every sample must not append anything.
	before := len(c.Actions())
	for _, id := range []string{"a", "b", "c", "d"} {
		res := mustApply(t, c, Sample{ID: id, At: at(clk), Backlog: intPtr(999)})
		if !res.Duplicate || len(res.Actions) != 0 {
			t.Fatalf("replay of %q = %+v, want duplicate with no actions", id, res)
		}
	}
	if after := len(c.Actions()); after != before {
		t.Fatalf("action log grew on replay: %d -> %d", before, after)
	}
}
