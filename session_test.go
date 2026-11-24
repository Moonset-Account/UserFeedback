package migrationplanner

import "testing"

// directionalEdge models a read-compatible new receiver: it accepts old json
// requests (json->proto on the request side), but the new proto response is
// not decodable by an old json caller (proto->json response is false).
func directionalEdge() *Edge {
	return &Edge{
		Caller: "A", Callee: "B",
		RequestOK: map[Codec]map[Codec]bool{
			"json":  {"json": true, "proto": true}, // new receiver reads old requests
			"proto": {"json": false, "proto": true},
		},
		ResponseOK: map[Codec]map[Codec]bool{
			"json":  {"json": true, "proto": true},
			"proto": {"json": false, "proto": true}, // old caller cannot parse new responses
		},
	}
}

func strictEdge() *Edge {
	return &Edge{
		Caller: "A", Callee: "B",
		RequestOK: map[Codec]map[Codec]bool{
			"json":  {"json": true, "proto": false},
			"proto": {"json": false, "proto": true},
		},
		ResponseOK: map[Codec]map[Codec]bool{
			"json":  {"json": true, "proto": false},
			"proto": {"json": false, "proto": true},
		},
	}
}

func pinnedSession() *Session {
	return &Session{
		ID:            "sess-1",
		Edge:          "A->B",
		CallerRep:     "A-r1",
		CalleeRep:     "B-r1",
		CallerVersion: "v1",
		CalleeVersion: "v1",
		ReqCodec:      "json",
		RespCodec:     "json",
		DrainEvent:    "drained-1",
	}
}

// ---- 5. pinned session keeps its codec until the named drain event ---------

func TestPinnedSessionWaitsForDrainEvent(t *testing.T) {
	spec := twoServiceSpec(t, strictEdge(), 1, 10, 6)
	// No observed drain event: the session must block every replacement.
	planner, err := NewPlanner(spec, initialAB(), []*Session{pinnedSession()}, PlannerConfig{})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if plan != nil {
		t.Fatal("expected no plan while the drain event is absent")
	}
	if !containsReason(blockedReasons(frontier), ReasonDrainNotObserved) {
		t.Fatalf("expected drain-not-observed block, got %v", blockedReasons(frontier))
	}
	if !containsString(frontier.WaitingEvents, "drained-1") {
		t.Fatalf("frontier must name the awaited event, got %v", frontier.WaitingEvents)
	}
	if !containsReason(blockedReasons(frontier), ReasonPinBroken) {
		t.Fatalf("pinned endpoint replacement must be blocked, got %v", blockedReasons(frontier))
	}
}

func TestPinnedSessionReleasesAfterObservedDrain(t *testing.T) {
	// Fully compatible edge: once the pin is released by drain, either
	// replica may be replaced in any order.
	spec := twoServiceSpec(t, edgeFullCompat(), 1, 10, 6)
	planner, err := NewPlanner(spec, initialAB(), []*Session{pinnedSession()}, PlannerConfig{
		ObservedEvents: []string{"drained-1"},
	})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if frontier != nil {
		t.Fatalf("expected a plan after drain, got %+v", frontier.Blocked)
	}
	drains := 0
	st := planner.initial
	if len(st.pins) != 1 {
		t.Fatalf("pinned session should survive construction, pins=%d", len(st.pins))
	}
	for _, step := range plan.Steps {
		st = planner.apply(st, step.Action)
		if step.Action.Kind == ActDrain {
			drains++
			if step.Action.DrainEvent != "drained-1" {
				t.Fatalf("unexpected drain event %q", step.Action.DrainEvent)
			}
			if len(st.pins) != 0 {
				t.Fatal("session pin must be released by the drain")
			}
		}
	}
	if drains != 1 {
		t.Fatalf("expected exactly one drain step, got %d", drains)
	}
	for id, r := range st.Replicas {
		if r.Version != "v2" {
			t.Fatalf("replica %s not migrated: %s", id, r.Version)
		}
	}
}

// ---- 6. a +1 read-compatible transitional replica unlocks the migration ----

func transitionalSpec(t *testing.T, withTransitional bool, coexist, budget int) *Spec {
	t.Helper()
	bVersions := map[string]Version{
		"v1": {Name: "v1", Profiles: []Profile{{"json", "json"}}},
		"v2": {Name: "v2", Profiles: []Profile{{"proto", "proto"}}},
	}
	if withTransitional {
		// vt can hold the old json/json pair and a bridge proto/json pair: it
		// reads old json requests while still answering in json.
		bVersions["vt"] = Version{Name: "vt", Profiles: []Profile{
			{"json", "json"},
			{"proto", "json"},
		}}
	}
	return &Spec{
		Services: map[string]*Service{
			"A": {
				Name: "A", Versions: basicProfiles(), Target: "v2",
				MaxReplicas: 2, CoexistSteps: coexist, RollbackBudget: budget,
			},
			"B": {
				Name: "B", Versions: bVersions, Target: "v2",
				MaxReplicas: 2, CoexistSteps: coexist, RollbackBudget: budget,
			},
		},
		Edges: []*Edge{directionalEdge()},
	}
}

func TestWithoutTransitionalReplicaMigrationIsBlocked(t *testing.T) {
	spec := transitionalSpec(t, false, 10, 6)
	planner, err := NewPlanner(spec, initialAB(), []*Session{pinnedSession()}, PlannerConfig{
		ObservedEvents: []string{"drained-1"},
	})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if plan != nil {
		t.Fatal("strict v1/v2 table must not admit a safe migration")
	}
	if !containsReason(blockedReasons(frontier), ReasonCodec) {
		t.Fatalf("expected conflicting caller/callee pairs, got %v", blockedReasons(frontier))
	}
}

func TestTransitionalReplicaEnablesForwardAndRollback(t *testing.T) {
	spec := transitionalSpec(t, true, 10, 6)
	planner, err := NewPlanner(spec, initialAB(), []*Session{pinnedSession()}, PlannerConfig{
		ObservedEvents: []string{"drained-1"},
	})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if frontier != nil {
		t.Fatalf("transitional replica should unlock the plan, got %+v", frontier.Blocked)
	}
	if len(plan.Steps) == 0 {
		t.Fatal("expected a non-empty plan")
	}

	// The shortest plan routes through the read-compatible transitional
	// version so every intermediate full mesh stays connected.
	usedTransitional := false
	st := planner.initial
	for _, step := range plan.Steps {
		st = planner.apply(st, step.Action)
		if step.Action.ToVer == "vt" || step.Action.FromVer == "vt" {
			usedTransitional = true
		}
		if _, pinned := st.pins["sess-1"]; pinned {
			// While pinned, the original endpoint pair must still exist.
			if st.Replicas["B-r1"] == nil || st.Replicas["A-r1"] == nil {
				t.Fatalf("pinned endpoint removed before drain at step %d", step.Index)
			}
		}
		// Every prefix keeps an in-budget, drain-free rollback to the start.
		if len(step.Rollback.Actions) > 0 {
			rb := st
			for _, a := range step.Rollback.Actions {
				if a.Kind == ActDrain {
					t.Fatalf("prefix %d rollback contains a drain", step.Index)
				}
				rb = planner.apply(rb, a)
				if fail := planner.checkConnectivity(rb, a); fail != nil {
					t.Fatalf("prefix %d rollback breaks connectivity: %s", step.Index, fail.detail)
				}
			}
			if rb.configKey() != planner.initialConfigKey {
				t.Fatalf("prefix %d rollback misses the initial layout", step.Index)
			}
		}
	}
	if !usedTransitional {
		t.Fatal("the solution must pass through the read-compatible transitional version")
	}
	for id, r := range st.Replicas {
		if r.Version != "v2" {
			t.Fatalf("replica %s finished on %s, want v2", id, r.Version)
		}
	}
}

// While the drain event is still pending the pinned B-r1 replica cannot be
// replaced or removed. A +1 transitional copy can still be brought up and
// kept mesh-connected (bounded forward progress), but the full target cannot
// be reached until the drain event arrives. The planner must report this as a
// reachable frontier that is waiting on the event.
func TestPlusOneTransitionalReplicaWhileDrainPending(t *testing.T) {
	spec := transitionalSpec(t, true, 10, 8)
	// No observed drain event: B-r1 stays pinned for the whole search.
	planner, err := NewPlanner(spec, initialAB(), []*Session{pinnedSession()}, PlannerConfig{})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if plan != nil {
		t.Fatal("the full target is unreachable while the drain event never arrives")
	}
	if frontier == nil {
		t.Fatal("expected a reachable frontier")
	}
	if !containsString(frontier.WaitingEvents, "drained-1") {
		t.Fatalf("frontier must name the awaited drain event, got %v", frontier.WaitingEvents)
	}
	if !containsReason(blockedReasons(frontier), ReasonDrainNotObserved) {
		t.Fatalf("frontier must show the pending drain block, got %v", blockedReasons(frontier))
	}

	// Independently verify bounded forward progress: adding a +1 bridge
	// replica on vt/proto-json is legal and keeps every mesh pair connected,
	// while the pinned endpoints remain untouched.
	st := planner.initial
	add := Action{
		Kind: ActAdd, Service: "B", ToVer: "vt",
		ToProf: Profile{Request: "proto", Response: "json"},
	}
	next := planner.apply(st, add)
	if fail := planner.checkConnectivity(next, add); fail != nil {
		t.Fatalf("+1 bridge replica must keep the mesh connected: %s", fail.detail)
	}
	if next.Replicas["B-r1"] == nil || next.Replicas["A-r1"] == nil {
		t.Fatal("pinned endpoints must survive the transitional add")
	}
	if len(next.pins) != 1 {
		t.Fatal("the session stays pinned without a drain event")
	}
	// Its rollback is a single remove that restores the initial layout.
	rb, ok := planner.reserveRollback(next)
	if !ok {
		t.Fatal("the transitional +1 layout must keep an in-budget rollback")
	}
	if len(rb) != 1 || rb[0].Kind != ActRemove {
		t.Fatalf("expected one remove as rollback, got %v", rb)
	}
}

// ---- 7. coexistence step limit is an acceptance condition ------------------

func TestCoexistenceLimitBlocksTransitionalLayout(t *testing.T) {
	// Drain is pending, so the only way forward is a +1 transitional replica
	// that briefly runs two versions side by side. A zero coexistence budget
	// forbids that mixed layout the moment it appears.
	spec := transitionalSpec(t, true, 0, 8)
	planner, err := NewPlanner(spec, initialAB(), []*Session{pinnedSession()}, PlannerConfig{})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if plan != nil {
		t.Fatal("zero coexistence budget must reject the +1 mixed layout")
	}
	if !containsReason(blockedReasons(frontier), ReasonCoexistLimit) {
		t.Fatalf("expected coexistence-limit block, got %v", blockedReasons(frontier))
	}
}

func TestCoexistenceLimitAllowsWithinBudget(t *testing.T) {
	// With a positive coexistence budget the very first mixed-version layout
	// (original v1 plus a +1 vt copy) is admitted; with budget zero the same
	// action is rejected. This isolates the bound from the (separately tested)
	// fact that a never-draining pin keeps the full goal unreachable.
	for _, tc := range []struct {
		coexist int
		allowed bool
	}{
		{0, false},
		{1, true},
		{2, true},
	} {
		spec := transitionalSpec(t, true, tc.coexist, 8)
		planner, err := NewPlanner(spec, initialAB(), []*Session{pinnedSession()}, PlannerConfig{})
		if err != nil {
			t.Fatalf("NewPlanner coexist=%d: %v", tc.coexist, err)
		}
		st := planner.initial
		add := Action{
			Kind: ActAdd, Service: "B", ToVer: "vt",
			ToProf: Profile{Request: "json", Response: "json"},
		}
		next := planner.apply(st, add)
		overBudget := planner.coexistExceeded(next) != nil
		meshOK := planner.checkMesh(next) == nil
		if tc.allowed {
			if overBudget || !meshOK {
				t.Fatalf("coexist=%d should admit the first mixed layout", tc.coexist)
			}
		} else if !overBudget {
			t.Fatalf("coexist=%d must reject the mixed layout the moment it appears", tc.coexist)
		}
	}
}

// ---- 10. interleaved drain / migration keeps every intermediate mesh live --

func TestInterleavedDrainAndMigrationMesh(t *testing.T) {
	spec := transitionalSpec(t, true, 10, 6)
	planner, err := NewPlanner(spec, initialAB(), []*Session{pinnedSession()}, PlannerConfig{
		ObservedEvents: []string{"drained-1"},
	})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if frontier != nil {
		t.Fatalf("unexpected frontier: %+v", frontier.Blocked)
	}
	st := planner.initial
	for _, step := range plan.Steps {
		st = planner.apply(st, step.Action)
		// Every still-traffic-receiving caller must reach every candidate
		// receiver after each single-item change.
		if fail := planner.checkConnectivity(st, step.Action); fail != nil {
			t.Fatalf("step %d (%s) broke the mesh: %s", step.Index, step.Action, fail.detail)
		}
		// Rollback reservation must never pretend drain is reversible.
		for _, a := range step.Rollback.Actions {
			if a.Kind == ActDrain {
				t.Fatalf("step %d reserves a drain inside rollback", step.Index)
			}
		}
	}
}

func containsString(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}
