package admit

import (
	"testing"
)

func cancellationLookaheadProblem() Problem {
	dur := map[BatchCost]int{}
	for _, id := range []string{"a", "b", "c"} {
		shape := "shape-" + id
		dur[BatchCost{Stage: Stage1, Shape: shape, Size: 1}] = 2
		dur[BatchCost{Stage: Stage2, Shape: shape, Size: 1}] = 4
	}
	return Problem{
		Requests: []Request{
			{ID: "A", Release: 0, Deadline: 8, Shape: "shape-a", Committed: true},
			{ID: "B", Release: 0, Deadline: 8, Shape: "shape-b", Profit: 10},
			{ID: "C", Release: 0, Deadline: 8, Shape: "shape-c", Profit: 10},
		},
		Durations: dur,
		Scenarios: []Scenario{{
			Name:     "A-cancelled-at-2",
			Continue: map[string]bool{"A": true, "B": true, "C": true},
			Cancellations: []Cancellation{{
				RequestID: "A",
				RevealAt:  2,
			}},
		}},
	}
}

func TestHiddenCancellationIsNotUsedForAdmission(t *testing.T) {
	p := cancellationLookaheadProblem()

	safe, err := Admit(p)
	if err != nil {
		t.Fatal(err)
	}
	if !safe.Feasible {
		t.Fatalf("committed A should remain feasible: %+v", safe.Witness)
	}
	if len(safe.NewlyAccepted) != 1 {
		t.Fatalf("safe newly accepted = %v, want exactly one of B/C", safe.NewlyAccepted)
	}
	if safe.TotalProfit != 10 {
		t.Fatalf("safe profit = %d, want 10; both B and C would require hidden cancellation", safe.TotalProfit)
	}

	fixed, err := EvaluateSet(p, []string{"A", "B", "C"})
	if err != nil {
		t.Fatal(err)
	}
	if fixed.Feasible {
		t.Fatalf("safe barrier must reject A+B+C; got margin %d", fixed.OptimalMargin)
	}
	if fixed.Witness == nil {
		t.Fatal("expected rejection branch witness")
	}
	if err := VerifyWitness(fixed.Witness); err != nil {
		t.Fatalf("witness verification: %v", err)
	}

	oracle, err := AdmitWithOptions(p, Options{DisableCancellationBarrier: true})
	if err != nil {
		t.Fatal(err)
	}
	if !oracle.Feasible || len(oracle.NewlyAccepted) != 2 || oracle.TotalProfit != 20 {
		t.Fatalf("oracle/look-ahead contrast = feasible=%v accepted=%v profit=%d; expected it to over-admit B,C",
			oracle.Feasible, oracle.NewlyAccepted, oracle.TotalProfit)
	}

	v, err := EvaluateSetWithOptions(p, []string{"A", "B", "C"}, Options{DisableCancellationBarrier: true})
	if err != nil {
		t.Fatal(err)
	}
	if !v.Feasible {
		t.Fatalf("cancellation-aware oracle schedule should be feasible: %+v", v.Witness)
	}
	d, err := NewVirtualDriver(p, v.Plan, "A-cancelled-at-2")
	if err != nil {
		t.Fatal(err)
	}
	// Drain the t=0 observation and start the two t=0 batches.
	for i := 0; i < 3; i++ {
		if _, err := d.Step(); err != nil {
			t.Fatal(err)
		}
	}
	if got := d.RevealedCancellations(); len(got) != 0 {
		t.Fatalf("cancellation revealed before boundary: %v", got)
	}
	run, err := d.Run()
	if err != nil {
		t.Fatal(err)
	}
	if !run.DeadlineMet {
		t.Fatalf("oracle contrast plan failed in virtual driver: %+v", run)
	}
	var revealTimes []int
	for _, ev := range run.Events {
		if ev.Type == EventCancellationRevealed {
			revealTimes = append(revealTimes, ev.Time)
		}
	}
	if len(revealTimes) != 1 || revealTimes[0] != 2 {
		t.Fatalf("cancellation reveal times = %v, want exactly [2]", revealTimes)
	}
}
