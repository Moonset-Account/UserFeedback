package admit

import (
	"testing"
)

func TestEmptyProblemIsFeasibleVacuously(t *testing.T) {
	res, err := Admit(Problem{})
	if err != nil {
		t.Fatalf("Admit empty: %v", err)
	}
	if !res.Feasible {
		t.Fatal("empty problem should be feasible")
	}
	if len(res.Accepted) != 0 || res.Plan == nil {
		t.Fatalf("bad empty result: %+v", res)
	}
	if res.OptimalMargin != 0 {
		t.Fatalf("vacuous margin=%d, want documented 0", res.OptimalMargin)
	}
	run, err := NewVirtualDriver(Problem{}, res.Plan, "")
	if err != nil {
		t.Fatal(err)
	}
	report, err := run.Run()
	if err != nil {
		t.Fatal(err)
	}
	if !report.DeadlineMet {
		t.Fatal("empty run should meet all deadlines vacuously")
	}
}

func TestMoreThanEightRequestsIsValidationError(t *testing.T) {
	p := Problem{Durations: map[BatchCost]int{}}
	for i := 0; i < 9; i++ {
		id := string(rune('a' + i))
		p.Requests = append(p.Requests, Request{ID: id, Release: 0, Deadline: 10, Shape: "s"})
		p.Durations[BatchCost{Stage: Stage1, Shape: "s", Size: 1}] = 1
	}
	if _, err := Admit(p); err == nil {
		t.Fatal("expected >8 request validation error")
	}
}

func TestEightRequestBoundaryIsSolvable(t *testing.T) {
	p := Problem{Durations: map[BatchCost]int{}}
	for i := 0; i < 8; i++ {
		id := "r" + string(rune('1'+i))
		p.Requests = append(p.Requests, Request{
			ID: id, Release: 0, Deadline: 4, Shape: "serial", Profit: 1,
		})
	}
	p.Durations[BatchCost{Stage: Stage1, Shape: "serial", Size: 1}] = 1
	p.Scenarios = []Scenario{{Name: "all-early"}}

	res, err := Admit(p)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Feasible {
		t.Fatalf("eight singleton stage1 requests in four slots per channel should fit: %+v", res.Witness)
	}
	if len(res.Accepted) != 8 {
		t.Fatalf("accepted %v, want all 8", res.Accepted)
	}
	run, err := NewVirtualDriver(p, res.Plan, "all-early")
	if err != nil {
		t.Fatal(err)
	}
	report, err := run.Run()
	if err != nil {
		t.Fatal(err)
	}
	if !report.DeadlineMet {
		t.Fatalf("eight-request run missed deadline: %+v", report)
	}
}

func TestAdmitChoosesHighestProfit(t *testing.T) {
	p := cancellationLookaheadProblem()
	// Make B strictly more valuable than C.
	p.Requests[1].Profit = 100
	p.Requests[2].Profit = 1

	res, err := Admit(p)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Feasible {
		t.Fatal(res.Witness)
	}
	if len(res.NewlyAccepted) != 1 || res.NewlyAccepted[0] != "B" {
		t.Fatalf("newly accepted=%v, want [B]", res.NewlyAccepted)
	}
	if res.TotalProfit != 100 {
		t.Fatalf("profit=%d, want 100", res.TotalProfit)
	}
}

func TestDeadlineEqualityIsAcceptableZeroMargin(t *testing.T) {
	p := Problem{
		Requests: []Request{{ID: "r", Release: 0, Deadline: 3, Shape: "s", Profit: 1}},
		Durations: map[BatchCost]int{
			{Stage: Stage1, Shape: "s", Size: 1}: 1,
			{Stage: Stage2, Shape: "s", Size: 1}: 2,
		},
		Scenarios: []Scenario{{Name: "continues", Continue: map[string]bool{"r": true}}},
	}
	v, err := EvaluateSet(p, []string{"r"})
	if err != nil {
		t.Fatal(err)
	}
	if !v.Feasible || v.OptimalMargin != 0 {
		t.Fatalf("verdict feasible=%v margin=%d witness=%+v; completion exactly at deadline should pass",
			v.Feasible, v.OptimalMargin, v.Witness)
	}
}

func TestWildcardDurationTableIsSupported(t *testing.T) {
	p := Problem{
		Requests: []Request{{ID: "r", Release: 0, Deadline: 2, Shape: "any-shape"}},
		Durations: map[BatchCost]int{
			{Stage: Stage1, Shape: "*", Size: 1}: 1,
		},
		Scenarios: []Scenario{{Name: "early"}},
	}
	v, err := EvaluateSet(p, []string{"r"})
	if err != nil {
		t.Fatal(err)
	}
	if !v.Feasible || v.OptimalMargin != 1 {
		t.Fatalf("wildcard verdict feasible=%v margin=%d witness=%+v", v.Feasible, v.OptimalMargin, v.Witness)
	}
}

func TestRejectionWitnessContainsForcingDeadlineConflict(t *testing.T) {
	p := Problem{
		Requests: []Request{{ID: "too-slow", Release: 0, Deadline: 1, Shape: "s"}},
		Durations: map[BatchCost]int{
			{Stage: Stage1, Shape: "s", Size: 1}: 2,
		},
		Scenarios: []Scenario{{Name: "early"}},
	}
	v, err := EvaluateSet(p, []string{"too-slow"})
	if err != nil {
		t.Fatal(err)
	}
	if v.Feasible {
		t.Fatal("expected impossible request to be rejected")
	}
	if err := VerifyWitness(v.Witness); err != nil {
		t.Fatalf("witness malformed: %v", err)
	}
	if !witnessHasDeadlineConflict(v.Witness.Root) {
		t.Fatalf("witness does not contain a deadline conflict: %+v", v.Witness.Root)
	}
}

func witnessHasDeadlineConflict(n *WitnessNode) bool {
	if n == nil {
		return false
	}
	if n.RequestID != "" && n.Completion > n.Deadline {
		return true
	}
	if witnessHasDeadlineConflict(n.Child) {
		return true
	}
	for _, dp := range n.DecisionProofs {
		if witnessHasDeadlineConflict(dp.Failure) {
			return true
		}
	}
	return false
}
