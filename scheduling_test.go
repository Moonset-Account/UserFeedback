package egress_test

import (
	"fmt"
	"sync"
	"testing"

	"egress"
)

func TestLateFramesAreStillSentAndNormalFIFOHolds(t *testing.T) {
	cfg := egress.Config{
		Table:      &egress.GateTable{Entries: []egress.GateEntry{{Duration: 100, Gates: egress.GateBoth}}},
		MinNonLast: 1,
		Overhead:   1,
		Frames: []egress.Frame{
			{ID: "N1", Class: egress.Normal, Arrival: 0, Length: 2, Deadline: 0},
			{ID: "N2", Class: egress.Normal, Arrival: 0, Length: 1, Deadline: 0},
			{ID: "F1", Class: egress.Fast, Arrival: 0, Length: 1, Deadline: 0},
		},
	}
	plan, err := egress.Schedule(cfg)
	if err != nil {
		t.Fatalf("Schedule: %v", err)
	}
	if plan.FastOnTime != 0 || plan.NormalOnTime != 0 {
		t.Fatalf("all deadlines are impossible, got F%d N%d", plan.FastOnTime, plan.NormalOnTime)
	}
	for _, id := range []string{"N1", "N2", "F1"} {
		r := plan.FrameByID[id]
		if r.Completion == 0 || r.DataBytes != r.Length {
			t.Fatalf("late frame %s not fully sent: %+v", id, r)
		}
	}
	if plan.OverheadBytes != 3 {
		t.Fatalf("OverheadBytes=%d, want one overhead per unsplit frame", plan.OverheadBytes)
	}

	sawN1Done := false
	for _, fr := range plan.Fragments {
		switch fr.FrameID {
		case "N1":
			if sawN1Done {
				t.Fatalf("N1 has fragment after completion marker: %+v", fr)
			}
			if fr.Final {
				sawN1Done = true
			}
		case "N2":
			if !sawN1Done {
				t.Fatalf("N2 overtook N1 at fragment %+v", fr)
			}
		}
	}
	if !sawN1Done {
		t.Fatal("N1 never completed")
	}
}

func TestConcurrentSchedulesDoNotShareMutableState(t *testing.T) {
	cfg := egress.Config{
		Table:      alwaysBothTable(),
		Frames:     fourShortFrames(),
		MinNonLast: 1,
		Overhead:   2,
	}
	const n = 24
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			p, err := egress.Schedule(cfg)
			if err != nil {
				errs <- err
				return
			}
			if p.FastOnTime != 2 || p.NormalOnTime != 2 || len(p.Fragments) != 5 {
				errs <- fmt.Errorf("unexpected plan F%d/N%d/fragments%d", p.FastOnTime, p.NormalOnTime, len(p.Fragments))
			}
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent Schedule assertion/error: %v", err)
		}
	}
}
