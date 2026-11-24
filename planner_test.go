package migrationplanner

import (
	"sort"
	"testing"
)

// ---- shared spec builders -------------------------------------------------

func basicProfiles() map[string]Version {
	return map[string]Version{
		"v1": {Name: "v1", Profiles: []Profile{{"json", "json"}}},
		"v2": {Name: "v2", Profiles: []Profile{{"proto", "proto"}}},
	}
}

func edgeFullCompat() *Edge {
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
	}
}

func twoServiceSpec(t *testing.T, edge *Edge, maxReplicas, coexist, budget int) *Spec {
	t.Helper()
	return &Spec{
		Services: map[string]*Service{
			"A": {
				Name: "A", Versions: basicProfiles(), Target: "v2",
				MaxReplicas: maxReplicas, CoexistSteps: coexist, RollbackBudget: budget,
			},
			"B": {
				Name: "B", Versions: basicProfiles(), Target: "v2",
				MaxReplicas: maxReplicas, CoexistSteps: coexist, RollbackBudget: budget,
			},
		},
		Edges: []*Edge{edge},
	}
}

func initialAB() map[string]*Replica {
	return map[string]*Replica{
		"A-r1": {ID: "A-r1", Service: "A", Version: "v1", Profile: Profile{"json", "json"}},
		"B-r1": {ID: "B-r1", Service: "B", Version: "v1", Profile: Profile{"json", "json"}},
	}
}

func blockedReasons(f *Frontier) []BlockedReason {
	var out []BlockedReason
	for _, b := range f.Blocked {
		out = append(out, b.Reason)
	}
	return out
}

func containsReason(rs []BlockedReason, want BlockedReason) bool {
	for _, r := range rs {
		if r == want {
			return true
		}
	}
	return false
}

// ---- 1. shortest successful forward plan ----------------------------------

func TestShortestPlanTwoServices(t *testing.T) {
	spec := twoServiceSpec(t, edgeFullCompat(), 1, 10, 4)
	planner, err := NewPlanner(spec, initialAB(), nil, PlannerConfig{})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if frontier != nil {
		t.Fatalf("expected a plan, got frontier: %+v", frontier.Blocked)
	}
	if !plan.GoalReached {
		t.Fatal("plan not marked as goal")
	}
	// Two replicas each need one replacement: shortest length is 2.
	if len(plan.Steps) != 2 {
		t.Fatalf("expected 2 shortest steps, got %d: %v", len(plan.Steps), plan.Steps)
	}
	// Every step must be a single replacement.
	for i, s := range plan.Steps {
		if s.Action.Kind != ActReplace {
			t.Fatalf("step %d is %s, want replace", i, s.Action.Kind)
		}
	}
	// Final state must run v2 everywhere.
	fin := planner.initial
	for _, s := range plan.Steps {
		fin = planner.apply(fin, s.Action)
	}
	for id, r := range fin.Replicas {
		if r.Version != "v2" {
			t.Fatalf("replica %s still on %s", id, r.Version)
		}
	}
}

// ---- 2. full-mesh codec incompatibility blocks the frontier ---------------

func TestCodecIncompatibilityBlocks(t *testing.T) {
	// Strict edge: json only talks to json, proto only to proto. A mixed
	// A=v2(proto) / B=v1(json) fleet cannot form a full mesh.
	strict := &Edge{
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
	spec := twoServiceSpec(t, strict, 1, 10, 4)
	planner, err := NewPlanner(spec, initialAB(), nil, PlannerConfig{})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if plan != nil {
		t.Fatalf("did not expect a plan, got %d steps", len(plan.Steps))
	}
	if frontier == nil || len(frontier.Blocked) == 0 {
		t.Fatal("expected blocked diagnostics on frontier")
	}
	if !containsReason(blockedReasons(frontier), ReasonCodec) {
		t.Fatalf("expected codec block, got %v", blockedReasons(frontier))
	}
	// The conflict must name both replicas of the offending caller/callee pair.
	found := false
	for _, b := range frontier.Blocked {
		if b.Reason == ReasonCodec && b.Caller != "" && b.Callee != "" && b.Edge == "A->B" {
			found = true
		}
	}
	if !found {
		t.Fatalf("blocked entry missing conflicting caller/callee pair: %+v", frontier.Blocked)
	}
}

// ---- 3. every successful prefix reserves an in-budget, drain-free rollback -

func TestEveryPrefixHasRollback(t *testing.T) {
	spec := twoServiceSpec(t, edgeFullCompat(), 1, 10, 4)
	planner, err := NewPlanner(spec, initialAB(), nil, PlannerConfig{})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if frontier != nil {
		t.Fatalf("unexpected frontier: %+v", frontier.Blocked)
	}
	st := planner.initial
	for _, step := range plan.Steps {
		for _, a := range step.Rollback.Actions {
			if a.Kind == ActDrain {
				t.Fatalf("rollback for prefix %d contains a drain step", step.Index)
			}
		}
		// Actually replay the reserved sequence and assert we land on the
		// starting layout, staying connected throughout.
		rb := st
		for _, a := range step.Rollback.Actions {
			rb = planner.apply(rb, a)
			if fail := planner.checkConnectivity(rb, a); fail != nil {
				t.Fatalf("rollback for prefix %d breaks connectivity: %s", step.Index, fail.detail)
			}
		}
		if rb.configKey() != planner.initialConfigKey {
			t.Fatalf("rollback for prefix %d does not reach initial layout", step.Index)
		}
		// Per-service budget respected.
		spent := map[string]int{}
		for _, a := range step.Rollback.Actions {
			spent[a.Service]++
		}
		for svc, n := range spent {
			if n > spec.Services[svc].RollbackBudget {
				t.Fatalf("rollback for prefix %d exceeds budget of %s", step.Index, svc)
			}
		}
		st = planner.apply(st, step.Action)
	}
}

// ---- 4. rollback budget exhausted => frontier names the position -----------

func TestRollbackBudgetExceeded(t *testing.T) {
	// Budget 0 forbids any forward change, because no return action remains.
	spec := twoServiceSpec(t, edgeFullCompat(), 1, 10, 0)
	planner, err := NewPlanner(spec, initialAB(), nil, PlannerConfig{})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	plan, frontier := planner.Plan()
	if plan != nil {
		t.Fatal("expected no plan under zero rollback budget")
	}
	if !containsReason(blockedReasons(frontier), ReasonRollbackUnreachable) {
		t.Fatalf("expected rollback block, got %v", blockedReasons(frontier))
	}
}

// ensure deterministic ordering for slice comparisons in later tests.
func sortedStrings(in []string) []string {
	out := append([]string(nil), in...)
	sort.Strings(out)
	return out
}

// ---- 8. capacity boundary: MaxReplicas blocks the +1 path ------------------

func TestCapacityBoundaryBlocksAdd(t *testing.T) {
	// Single-replica capacity means no transitional copy can ever be added.
	spec := twoServiceSpec(t, strictEdge(), 1, 10, 6)
	planner, err := NewPlanner(spec, initialAB(), []*Session{pinnedSession()}, PlannerConfig{
		ObservedEvents: []string{"drained-1"},
	})
	if err != nil {
		t.Fatalf("NewPlanner: %v", err)
	}
	_, frontier := planner.Plan()
	if frontier == nil {
		t.Fatal("strict single-replica table must hit a blocked frontier")
	}
	if !containsReason(blockedReasons(frontier), ReasonCapacity) {
		t.Fatalf("expected a capacity block, got %v", blockedReasons(frontier))
	}
}

// ---- 9. validation rejects an inconsistent static table --------------------

func TestValidationRejectsUnknownTarget(t *testing.T) {
	spec := twoServiceSpec(t, edgeFullCompat(), 1, 10, 4)
	spec.Services["A"].Target = "v9"
	if _, err := NewPlanner(spec, initialAB(), nil, PlannerConfig{}); err == nil {
		t.Fatal("expected a validation error for an undeployable target")
	}
}

func TestValidationRejectsBadInitialReplica(t *testing.T) {
	spec := twoServiceSpec(t, edgeFullCompat(), 1, 10, 4)
	bad := initialAB()
	bad["A-r1"].Profile = Profile{"yaml", "yaml"}
	if _, err := NewPlanner(spec, bad, nil, PlannerConfig{}); err == nil {
		t.Fatal("expected a validation error for an unsupported initial profile")
	}
}

var _ = sortedStrings
