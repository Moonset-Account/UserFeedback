package admit

import (
	"testing"
)

func TestBatchingSameShapeAndParallelChannels(t *testing.T) {
	p := Problem{
		Requests: []Request{
			{ID: "X", Release: 0, Deadline: 5, Shape: "blocker", Committed: true},
			{ID: "A", Release: 0, Deadline: 4, Shape: "wide", Profit: 5},
			{ID: "B", Release: 0, Deadline: 4, Shape: "wide", Profit: 5},
		},
		Durations: map[BatchCost]int{
			{Stage: Stage1, Shape: "blocker", Size: 1}: 4,
			{Stage: Stage1, Shape: "wide", Size: 1}:    2,
			{Stage: Stage1, Shape: "wide", Size: 2}:    3,
		},
		Scenarios: []Scenario{{
			Name:     "all-early",
			Continue: map[string]bool{},
		}},
	}
	res, err := Admit(p)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Feasible {
		t.Fatalf("expected feasible: %+v", res.Witness)
	}
	t.Logf("accepted=%v newly=%v profit=%d margin=%d", res.Accepted, res.NewlyAccepted, res.TotalProfit, res.OptimalMargin)

	var foundPair bool
	var starts []BatchAction
	walkPlan(res.Plan.Root, func(n *PlanNode) {
		if n.Start != nil {
			starts = append(starts, *n.Start)
			if n.Start.Stage == Stage1 && n.Start.Shape == "wide" && len(n.Start.RequestIDs) == 2 {
				foundPair = true
			}
		}
	})
	if !foundPair {
		t.Fatalf("expected an A+B same-shape stage1 batch; starts=%+v", starts)
	}

	run, err := NewVirtualDriver(p, res.Plan, "all-early")
	if err != nil {
		t.Fatal(err)
	}
	report, err := run.Run()
	if err != nil {
		t.Fatal(err)
	}
	if !report.DeadlineMet || report.Margin != 1 {
		t.Fatalf("report=%+v, want deadline met margin 1", report)
	}
	if report.CompletionAt["A"] != 3 || report.CompletionAt["B"] != 3 {
		t.Fatalf("A/B completion=%v, want 3", report.CompletionAt)
	}
	if report.CompletionAt["X"] != 4 {
		t.Fatalf("X completion=%d, want 4", report.CompletionAt["X"])
	}
}

func TestCancellationBoundaryInterleavesWithNonpreemptibleBatch(t *testing.T) {
	p := Problem{
		Requests: []Request{
			{ID: "A-long", Release: 0, Deadline: 4, Shape: "long"},
			{ID: "B-short", Release: 0, Deadline: 5, Shape: "short"},
		},
		Durations: map[BatchCost]int{
			{Stage: Stage1, Shape: "long", Size: 1}:  4,
			{Stage: Stage1, Shape: "short", Size: 1}: 2,
			{Stage: Stage2, Shape: "short", Size: 1}: 2,
		},
		Scenarios: []Scenario{
			{
				Name: "cancel-A-at-1",
				// A is early after stage1 anyway; cancellation changes its
				// terminal obligation but must not preempt the running batch.
				Continue:      map[string]bool{"B-short": true},
				Cancellations: []Cancellation{{RequestID: "A-long", RevealAt: 1}},
			},
			{
				Name:     "no-cancel",
				Continue: map[string]bool{"B-short": true},
			},
		},
	}
	v, err := EvaluateSet(p, []string{"A-long", "B-short"})
	if err != nil {
		t.Fatal(err)
	}
	if !v.Feasible {
		t.Fatalf("expected interleaved schedule: %+v", v.Witness)
	}

	d, err := NewVirtualDriver(p, v.Plan, "cancel-A-at-1")
	if err != nil {
		t.Fatal(err)
	}
	report, err := d.Run()
	if err != nil {
		t.Fatal(err)
	}
	if !report.DeadlineMet {
		t.Fatalf("deadline missed: %+v", report)
	}
	if got := report.RevealedCancellations; len(got) != 1 || got[0] != "A-long" {
		t.Fatalf("revealed cancellations=%v", got)
	}

	var cancelAt int = -1
	type interval struct {
		id     string
		ch     int
		start  int
		finish int
		stage  Stage
	}
	var intervals []interval
	for _, ev := range report.Events {
		if ev.Type == EventCancellationRevealed && ev.RequestID == "A-long" {
			cancelAt = ev.Time
		}
		if ev.Type == EventBatchStarted && ev.Batch != nil {
			intervals = append(intervals, interval{
				id: ev.Batch.RequestIDs[0], ch: ev.Channel, start: ev.Batch.Start,
				finish: ev.Batch.Finish, stage: ev.Batch.Stage,
			})
		}
	}
	if cancelAt != 1 {
		t.Fatalf("cancellation boundary = %d, want 1", cancelAt)
	}
	var longA *interval
	var bStage2 *interval
	for i := range intervals {
		if intervals[i].id == "A-long" && intervals[i].stage == Stage1 {
			longA = &intervals[i]
		}
		if intervals[i].id == "B-short" && intervals[i].stage == Stage2 {
			bStage2 = &intervals[i]
		}
	}
	if longA == nil || bStage2 == nil {
		t.Fatalf("missing expected intervals: %+v", intervals)
	}
	if longA.start != 0 || longA.finish != 4 {
		t.Fatalf("long batch interval=[%d,%d], want [0,4]", longA.start, longA.finish)
	}
	if bStage2.start != 2 || bStage2.finish != 4 {
		t.Fatalf("B stage2 interval=[%d,%d], want [2,4]", bStage2.start, bStage2.finish)
	}
	if !(bStage2.start < longA.finish && bStage2.finish <= longA.finish) {
		t.Fatalf("expected B stage2 %+v to overlap tail of nonpreemptible A %+v", bStage2, longA)
	}
	if report.CompletionAt["B-short"] != 4 {
		t.Fatalf("B completion=%d, want 4", report.CompletionAt["B-short"])
	}
	if _, ok := report.CompletionAt["A-long"]; ok {
		t.Fatalf("canceled A should not have normal completion: %+v", report.CompletionAt)
	}
}

func TestSequentialCancellationBoundariesRemainHiddenUntilRevealed(t *testing.T) {
	p := Problem{
		Requests: []Request{
			{ID: "A", Release: 0, Deadline: 4, Shape: "s"},
			{ID: "B", Release: 0, Deadline: 4, Shape: "s"},
		},
		Durations: map[BatchCost]int{
			{Stage: Stage1, Shape: "s", Size: 1}: 4,
			{Stage: Stage1, Shape: "s", Size: 2}: 4,
		},
		Scenarios: []Scenario{{
			Name:     "two-cancellations",
			Continue: map[string]bool{},
			Cancellations: []Cancellation{
				{RequestID: "A", RevealAt: 1},
				{RequestID: "B", RevealAt: 2},
			},
		}},
	}
	v, err := EvaluateSet(p, []string{"A", "B"})
	if err != nil {
		t.Fatal(err)
	}
	if !v.Feasible {
		t.Fatalf("parallel nonpreemptible batches should remain executable: %+v", v.Witness)
	}
	d, err := NewVirtualDriver(p, v.Plan, "two-cancellations")
	if err != nil {
		t.Fatal(err)
	}
	run, err := d.Run()
	if err != nil {
		t.Fatal(err)
	}
	var reveals []int
	for _, ev := range run.Events {
		if ev.Type == EventCancellationRevealed {
			reveals = append(reveals, ev.Time)
		}
	}
	if len(reveals) != 2 || reveals[0] != 1 || reveals[1] != 2 {
		t.Fatalf("reveal times=%v, want [1 2]", reveals)
	}
}

func TestVirtualClockRejectsBackwardsMovement(t *testing.T) {
	c := NewVirtualClock(3)
	if c.Now() != 3 {
		t.Fatalf("Now=%d want 3", c.Now())
	}
	if err := c.AdvanceTo(2); err == nil {
		t.Fatal("expected backward AdvanceTo error")
	}
	if err := c.Advance(-1); err == nil {
		t.Fatal("expected negative Advance error")
	}
	if err := c.Advance(2); err != nil || c.Now() != 5 {
		t.Fatalf("Advance(2) now=%d err=%v, want now 5", c.Now(), err)
	}
}

func walkPlan(n *PlanNode, fn func(*PlanNode)) {
	if n == nil {
		return
	}
	fn(n)
	if n.Start != nil {
		walkPlan(n.Then, fn)
	}
	for _, b := range n.Branches {
		walkPlan(b.Next, fn)
	}
}
