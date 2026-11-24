package admit

import (
	"testing"
)

func correlationProblem(withBothWorstCase bool) Problem {
	p := Problem{
		Requests: []Request{
			{ID: "X", Release: 0, Deadline: 10, Shape: "block", Committed: true},
			{ID: "A", Release: 0, Deadline: 6, Shape: "wide", Profit: 10},
			{ID: "B", Release: 0, Deadline: 6, Shape: "wide", Profit: 10},
		},
		Durations: map[BatchCost]int{
			{Stage: Stage1, Shape: "wide", Size: 1}:  2,
			{Stage: Stage1, Shape: "wide", Size: 2}:  2,
			{Stage: Stage2, Shape: "wide", Size: 1}:  4,
			{Stage: Stage2, Shape: "wide", Size: 2}:  8,
			{Stage: Stage1, Shape: "block", Size: 1}: 5,
			{Stage: Stage2, Shape: "block", Size: 1}: 5,
		},
		Scenarios: []Scenario{
			{Name: "A-only", Continue: map[string]bool{"A": true, "X": true}},
			{Name: "B-only", Continue: map[string]bool{"B": true, "X": true}},
		},
	}
	if withBothWorstCase {
		p.Scenarios = append(p.Scenarios, Scenario{
			Name:     "both-independent-worst-case",
			Continue: map[string]bool{"A": true, "B": true, "X": true},
		})
	}
	return p
}

func TestCorrelatedWorstCasesCanBeExploited(t *testing.T) {
	p := correlationProblem(false)
	res, err := Admit(p)
	if err != nil {
		t.Fatalf("Admit returned error: %v", err)
	}
	if !res.Feasible {
		t.Fatalf("expected feasible admission, got witness: %+v", res.Witness)
	}
	if got, want := len(res.NewlyAccepted), 2; got != want {
		t.Fatalf("newly accepted count = %d (%v), want %d [A B]", got, res.NewlyAccepted, want)
	}
	if res.TotalProfit != 20 {
		t.Fatalf("total profit = %d, want 20", res.TotalProfit)
	}
	if res.OptimalMargin != 0 {
		t.Fatalf("worst margin = %d, want 0", res.OptimalMargin)
	}
	if res.Plan == nil {
		t.Fatal("expected executable plan")
	}

	for _, name := range []string{"A-only", "B-only"} {
		d, err := NewVirtualDriver(p, res.Plan, name)
		if err != nil {
			t.Fatalf("NewVirtualDriver(%s): %v", name, err)
		}
		run, err := d.Run()
		if err != nil {
			t.Fatalf("Run(%s): %v", name, err)
		}
		if !run.DeadlineMet || run.Margin != 0 {
			t.Fatalf("scenario %s report = %+v, want deadline met with margin 0", name, run)
		}
		if run.CompletionAt["X"] != 10 {
			t.Fatalf("X completion = %d, want 10", run.CompletionAt["X"])
		}
	}
}

func TestUncorrelatedBothWorstCaseIsRejectedWithWitness(t *testing.T) {
	p := correlationProblem(true)
	v, err := EvaluateSet(p, []string{"X", "A", "B"})
	if err != nil {
		t.Fatalf("EvaluateSet error: %v", err)
	}
	if v.Feasible {
		t.Fatalf("expected rejection, got feasible plan margin %d", v.OptimalMargin)
	}
	if v.Witness == nil || v.Witness.Root == nil {
		t.Fatal("expected structured rejection witness")
	}
	if err := VerifyWitness(v.Witness); err != nil {
		t.Fatalf("invalid witness: %v", err)
	}
}
