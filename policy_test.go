package egress_test

import (
	"testing"

	"egress"
)

func TestAmbiguousTwoTableReviewer(t *testing.T) {
	rep, err := egress.ReviewAmbiguousChoices()
	if err != nil {
		t.Fatalf("ReviewAmbiguousChoices: %v", err)
	}
	c := rep.Case
	b := c.IndistinguishableUntil

	// Before the first slice boundary both tables expose an open normal gate.
	for tick := 0; tick < b; tick++ {
		for _, sc := range c.Scenarios {
			if !sc.Table.GateOpenAt(tick, egress.Normal) {
				t.Fatalf("scenario %s normal gate not open at shared prefix tick %d", sc.Name, tick)
			}
		}
	}

	// Continuing one 6-byte fragment is legal in B but misses A's close at 5.
	if rep.ContinueInA.Legal {
		t.Fatal("continue choice should miss gate in phase A")
	}
	if rep.ContinueInA.FirstMiss == nil || rep.ContinueInA.FirstMiss.GateClose != 5 {
		t.Fatalf("continue A first miss = %+v, want gate close 5", rep.ContinueInA.FirstMiss)
	}
	if !rep.ContinueInB.Legal {
		t.Fatalf("continue choice should be legal in B: %+v", rep.ContinueInB.Evaluation.Violations)
	}

	// The early two-fragment choice is legal in A but its overhead-bearing
	// second slice [7,11) misses B's short 7..10 reopening.
	if !rep.SplitEarlyInA.Legal {
		t.Fatalf("split choice should be legal in A: %+v", rep.SplitEarlyInA.Evaluation.Violations)
	}
	if rep.SplitEarlyInB.Legal {
		t.Fatal("split choice should miss gate in phase B")
	}
	if rep.SplitEarlyInB.FirstMiss == nil || rep.SplitEarlyInB.FirstMiss.GateClose != 10 {
		t.Fatalf("split B first miss = %+v, want gate close 10", rep.SplitEarlyInB.FirstMiss)
	}
}

func onlinePolicyFrames() []egress.Frame {
	return []egress.Frame{
		{ID: "N1", Class: egress.Normal, Arrival: 0, Length: 3, Deadline: 10},
		{ID: "N2", Class: egress.Normal, Arrival: 0, Length: 1, Deadline: 15},
		{ID: "F1", Class: egress.Fast, Arrival: 3, Length: 1, Deadline: 6, Committed: true},
	}
}

func onlinePolicyConfig() egress.PolicyConfig {
	tableA := &egress.GateTable{Entries: []egress.GateEntry{
		{Duration: 3, Gates: egress.GateBoth},
		{Duration: 3, Gates: egress.GateBoth},
		{Duration: 14, Gates: egress.GateBoth},
	}}
	tableB := &egress.GateTable{Entries: []egress.GateEntry{
		{Duration: 3, Gates: egress.GateBoth},
		{Duration: 5, Gates: egress.GateBoth},
		{Duration: 12, Gates: egress.GateBoth},
	}}
	return egress.PolicyConfig{
		Scenarios: []egress.Scenario{
			{Name: "A", Table: tableA},
			{Name: "B", Table: tableB},
		},
		Frames:     onlinePolicyFrames(),
		MinNonLast: 1,
		Overhead:   2,
		Probes:     []egress.Probe{{ReadAt: 3}},
	}
}

func TestBuildPolicySharesPrefixThenBranches(t *testing.T) {
	pol, err := egress.BuildPolicy(onlinePolicyConfig())
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	if err := pol.Verify(); err != nil {
		t.Fatalf("Verify policy: %v", err)
	}
	if pol.Root == nil || pol.Root.Kind != "action" || pol.Root.Action == nil {
		t.Fatalf("root = %+v, want shared action", pol.Root)
	}
	if got := pol.Root.Action.FrameID; got != "N1" || pol.Root.Action.Start != 0 || pol.Root.Action.End != 3 || pol.Root.Action.Data != 1 {
		t.Fatalf("shared root action = %+v, want N1 [0,3) data1", pol.Root.Action)
	}
	if len(pol.Root.Candidates) != 2 {
		t.Fatalf("root candidates = %v, want A,B", pol.Root.Candidates)
	}

	obs := pol.Root.Next
	if obs == nil || obs.Kind != "observe" || obs.Observation == nil {
		t.Fatalf("root next = %+v, want observation node", obs)
	}
	if obs.Observation.LatchedAt != 3 || obs.Observation.BranchAt != 3 {
		t.Fatalf("branch timing = latch %d branch %d, want 3/3", obs.Observation.LatchedAt, obs.Observation.BranchAt)
	}
	if len(obs.Branches) != 2 {
		t.Fatalf("branches = %d, want 2 distinct timestamps", len(obs.Branches))
	}

	for _, name := range []string{"A", "B"} {
		plan, err := pol.ScenarioPlan(name)
		if err != nil {
			t.Fatalf("ScenarioPlan(%s): %v", name, err)
		}
		if !plan.IsOnTime("F1") {
			t.Fatalf("committed F1 not on time in %s: %+v", name, plan.FrameByID["F1"])
		}
		if got := plan.FrameByID["F1"].Completion; got != 6 {
			t.Fatalf("%s F1 completion = %d, want 6", name, got)
		}
	}

	planA, _ := pol.ScenarioPlan("A")
	planB, _ := pol.ScenarioPlan("B")
	if !planA.IsOnTime("N1") || !planA.IsOnTime("N2") {
		t.Fatalf("A normal frames should both be on time: N1=%+v N2=%+v", planA.FrameByID["N1"], planA.FrameByID["N2"])
	}
	if !planB.IsOnTime("N1") || !planB.IsOnTime("N2") {
		t.Fatalf("shared prefix should preserve both normal completions in B: N1=%+v N2=%+v", planB.FrameByID["N1"], planB.FrameByID["N2"])
	}
	if planB.NormalOnTime != 2 {
		t.Fatalf("B normal on-time = %d, want robust worst-case 2", planB.NormalOnTime)
	}

	// Waiting until the probe would complete N1 at 11, missing deadline 10;
	// the selected productive shared prefix keeps worst-case normal count 2
	// instead of 1.
	waitCfg := onlinePolicyConfig()
	waitPol, err := egress.BuildPolicy(waitCfg)
	if err != nil {
		t.Fatal(err)
	}
	foundWait := false
	var walk func(n *egress.PolicyNode)
	walk = func(n *egress.PolicyNode) {
		if n == nil {
			return
		}
		if n.Kind == "wait" && n.WaitUntil == 3 {
			foundWait = true
		}
		walk(n.Next)
		for _, b := range n.Branches {
			walk(b.Next)
		}
	}
	walk(waitPol.Root)
	if foundWait {
		t.Fatal("optimizer selected wait candidate despite worse worst-normal outcome")
	}
}

func TestTwoTimestampObservationsGraduallyNarrowPhase(t *testing.T) {
	frames := onlinePolicyFrames()
	table1 := &egress.GateTable{Entries: []egress.GateEntry{
		{Duration: 3, Gates: egress.GateBoth},
		{Duration: 3, Gates: egress.GateBoth},
		{Duration: 14, Gates: egress.GateBoth},
	}}
	table2 := &egress.GateTable{Entries: []egress.GateEntry{
		{Duration: 3, Gates: egress.GateBoth},
		{Duration: 3, Gates: egress.GateBoth},
		{Duration: 2, Gates: egress.GateBoth},
		{Duration: 12, Gates: egress.GateBoth},
	}}
	table3 := &egress.GateTable{Entries: []egress.GateEntry{
		{Duration: 3, Gates: egress.GateBoth},
		{Duration: 2, Gates: egress.GateBoth},
		{Duration: 15, Gates: egress.GateBoth},
	}}
	cfg := egress.PolicyConfig{
		Scenarios: []egress.Scenario{
			{Name: "P1", Table: table1},
			{Name: "P2", Table: table2},
			{Name: "P3", Table: table3},
		},
		Frames:     frames,
		MinNonLast: 1,
		Overhead:   2,
		Probes:     []egress.Probe{{ReadAt: 3}, {ReadAt: 6}},
	}
	pol, err := egress.BuildPolicy(cfg)
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	if err := pol.Verify(); err != nil {
		t.Fatalf("Verify: %v", err)
	}

	first := pol.Root.Next
	if first == nil || first.Kind != "observe" || len(first.Branches) != 2 {
		t.Fatalf("first observation = %+v, want two stamp groups (3->2+1)", first)
	}
	var pairBranch *egress.PolicyBranch
	for i := range first.Branches {
		b := &first.Branches[i]
		if len(b.Candidates) == 2 {
			pairBranch = b
		}
	}
	if pairBranch == nil {
		t.Fatalf("no two-scenario branch after first stamp: %+v", first.Branches)
	}

	// The pair shares F1 [3,6), then the second timestamp separates P1/P2.
	node := pairBranch.Next
	if node == nil || node.Kind != "action" || node.Action == nil || node.Action.FrameID != "F1" ||
		node.Action.Start != 3 || node.Action.End != 6 {
		t.Fatalf("pair common action = %+v, want F1 [3,6)", node)
	}
	second := node.Next
	if second == nil || second.Kind != "observe" || second.Observation.ProbeIndex != 1 {
		t.Fatalf("second observation = %+v", second)
	}
	if len(second.Branches) != 2 {
		t.Fatalf("second observation branches = %d, want P1/P2 split", len(second.Branches))
	}
	for _, name := range []string{"P1", "P2", "P3"} {
		plan, err := pol.ScenarioPlan(name)
		if err != nil {
			t.Fatalf("ScenarioPlan(%s): %v", name, err)
		}
		if !plan.IsOnTime("F1") || plan.FrameByID["F1"].Completion != 6 {
			t.Fatalf("%s committed F1 = %+v, want completion 6 on time", name, plan.FrameByID["F1"])
		}
	}
}

func TestTimestampLatchedInsideUnwithdrawableFragment(t *testing.T) {
	cfg := onlinePolicyConfig()
	cfg.Probes = []egress.Probe{{ReadAt: 2}, {ReadAt: 3}}
	pol, err := egress.BuildPolicy(cfg)
	if err != nil {
		t.Fatalf("BuildPolicy: %v", err)
	}
	if err := pol.Verify(); err != nil {
		t.Fatalf("Verify: %v", err)
	}
	a := pol.Root.Action
	if a == nil || a.Start != 0 || a.End != 3 {
		t.Fatalf("root action = %+v, want fragment crossing latch tick 2 and ending at 3", a)
	}
	first := pol.Root.Next
	if first == nil || first.Kind != "observe" || first.Observation == nil {
		t.Fatalf("first node = %+v, want observation", first)
	}
	if first.Observation.LatchedAt != 2 || first.Observation.BranchAt != 3 {
		t.Fatalf("first event = %+v, want latch 2 and branch only at fragment end 3", first.Observation)
	}
	if len(first.Branches) != 1 || len(first.Branches[0].Candidates) != 2 {
		t.Fatalf("mid-fragment timestamp should remain ambiguous: %+v", first.Branches)
	}
	second := first.Branches[0].Next
	if second == nil || second.Kind != "observe" || len(second.Branches) != 2 {
		t.Fatalf("boundary observation = %+v, want A/B split", second)
	}
	for _, name := range []string{"A", "B"} {
		plan, err := pol.ScenarioPlan(name)
		if err != nil {
			t.Fatalf("ScenarioPlan(%s): %v", name, err)
		}
		if !plan.IsOnTime("F1") {
			t.Fatalf("committed F1 missed in %s after unwithdrawable prefix: %+v", name, plan.FrameByID["F1"])
		}
	}
}

func TestPolicyRejectsMoreThanTwoObservations(t *testing.T) {
	cfg := onlinePolicyConfig()
	cfg.Probes = []egress.Probe{{ReadAt: 1}, {ReadAt: 2}, {ReadAt: 3}}
	if _, err := egress.BuildPolicy(cfg); err == nil {
		t.Fatal("BuildPolicy accepted three timestamp observations")
	}
}
