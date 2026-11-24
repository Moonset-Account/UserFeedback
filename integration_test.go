package migrationplanner

import "testing"

// streamEdge attaches FSMs to ordered version pairs of the A->B edge.
func streamEdge(t *testing.T, v1Proto, v2Proto *StreamProtocol) *Edge {
	t.Helper()
	return &Edge{
		Caller: "A", Callee: "B",
		RequestOK: map[Codec]map[Codec]bool{
			"json":  {"json": true, "proto": true},
			"proto": {"json": true, "proto": true},
		},
		ResponseOK: map[Codec]map[Codec]bool{
			"json":  {"json": true, "proto": true},
			"proto": {"json": true, "proto": true},
		},
		Streams: map[VPair]*StreamProtocol{
			{CallerVersion: "v1", CalleeVersion: "v1"}: v1Proto,
			{CallerVersion: "v2", CalleeVersion: "v2"}: v2Proto,
			// A cut-over callee speaks its own version's protocol, so any pair
			// whose callee is v2 uses the v2 FSM; this is where an in-flight
			// v1 session first observes non-equivalence.
			{CallerVersion: "v1", CalleeVersion: "v2"}: v2Proto,
			{CallerVersion: "v2", CalleeVersion: "v1"}: v1Proto,
		},
	}
}

func streamSession() *Session {
	return &Session{
		ID: "stream-sess", Edge: "A->B",
		CallerRep: "A-r1", CalleeRep: "B-r1",
		CallerVersion: "v1", CalleeVersion: "v1",
		ReqCodec: "json", RespCodec: "json",
		DrainEvent: "drain-x",
	}
}

// Even though the static codec table permits the upgrade, a pinned in-flight
// session whose stream FSM changes non-equivalently must block the move and
// surface a replayable shortest divergence. The planner routes around it by
// waiting for the drain event.
func TestInFlightStreamDivergenceBlocksUntilDrain(t *testing.T) {
	v1 := buildV1(t)
	bad := buildV2Divergent(t)
	spec := twoServiceSpec(t, streamEdge(t, v1, bad), 2, 10, 8)

	planner, err := NewPlanner(spec, initialAB(), []*Session{streamSession()}, PlannerConfig{})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}

	// Simulate the precise transition the gate is designed for: the pinned
	// callee endpoint is cut over to the new, non-equivalent stream version
	// while the old session is still pinned to it. The connectivity check must
	// reject this state and carry a replayable divergence.
	st := planner.initial
	cutover := Action{
		Kind: ActReplace, Service: "B", Replica: "B-r1",
		FromVer: "v1", ToVer: "v2",
		FromProf: Profile{"json", "json"}, ToProf: Profile{"proto", "proto"},
	}
	next := planner.apply(st, cutover)
	fail := planner.checkConnectivity(next, cutover)
	if fail == nil {
		t.Fatal("cut-over across non-equivalent stream FSMs must be rejected")
	}
	if fail.reason != ReasonStreamDivergence || fail.diverge == nil {
		t.Fatalf("expected stream divergence, got %s: %s", fail.reason, fail.detail)
	}
	d := fail.diverge
	if d.At.Kind != KindHalfClose {
		t.Fatalf("divergent frame should be the missing half-close, got %s", d.At.label())
	}
	// The reported dialogue must be replayable on the old FSM.
	rst := v1.Initial()
	for _, f := range d.Replay {
		to, _, ok := v1.Step(rst, f)
		if !ok {
			t.Fatalf("reported replay frame %s is not valid on the old FSM", f.label())
		}
		rst = to
	}

	// End to end: while drain is pending the full upgrade stays unreachable
	// and the frontier names the event the controller must wait for.
	if plan, frontier := planner.Plan(); plan != nil {
		t.Fatal("non-equivalent in-flight stream must block the upgrade")
	} else if !containsString(frontier.WaitingEvents, "drain-x") {
		t.Fatalf("frontier must name the awaited drain event, got %v", frontier.WaitingEvents)
	}

	// After the drain event the pin (and its stream obligation) is released,
	// so the same static upgrade succeeds: the planner routes around the
	// divergence by waiting rather than forcing the incompatible cut-over.
	planner2, err := NewPlanner(spec, initialAB(), []*Session{streamSession()}, PlannerConfig{
		ObservedEvents: []string{"drain-x"},
	})
	if err != nil {
		t.Fatalf("NewPlanner after drain: %v", err)
	}
	plan, frontier := planner2.Plan()
	if frontier != nil {
		t.Fatalf("after drain the upgrade should proceed: %+v", frontier.Blocked)
	}
	if len(plan.Steps) == 0 {
		t.Fatal("expected a non-empty post-drain plan")
	}
}

func TestEquivalentInFlightStreamAllowsUpgrade(t *testing.T) {
	v1 := buildV1(t)
	v2 := buildV2Equivalent(t)
	spec := twoServiceSpec(t, streamEdge(t, v1, v2), 1, 10, 6)
	planner, err := NewPlanner(spec, initialAB(), []*Session{streamSession()}, PlannerConfig{
		ObservedEvents: []string{"drain-x"},
	})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if frontier != nil {
		t.Fatalf("equivalent FSMs must not block the upgrade: %+v", frontier.Blocked)
	}
	if len(plan.Steps) == 0 {
		t.Fatal("expected a non-empty plan")
	}
}
