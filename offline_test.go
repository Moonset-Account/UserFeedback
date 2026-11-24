package egress_test

import (
	"errors"
	"testing"

	"egress"
)

func fourShortFrames() []egress.Frame {
	return []egress.Frame{
		{ID: "N1", Class: egress.Normal, Arrival: 0, Length: 3, Deadline: 13},
		{ID: "N2", Class: egress.Normal, Arrival: 0, Length: 1, Deadline: 16},
		{ID: "F1", Class: egress.Fast, Arrival: 3, Length: 1, Deadline: 6},
		{ID: "F2", Class: egress.Fast, Arrival: 3, Length: 1, Deadline: 9},
	}
}

func alwaysBothTable() *egress.GateTable {
	return &egress.GateTable{Entries: []egress.GateEntry{{Duration: 100, Gates: egress.GateBoth}}}
}

func TestScheduleFourShortFramesAvoidsCounterproductiveExtraSlice(t *testing.T) {
	cfg := egress.Config{
		Table:      alwaysBothTable(),
		Frames:     fourShortFrames(),
		MinNonLast: 1,
		Overhead:   2,
	}
	plan, err := egress.Schedule(cfg)
	if err != nil {
		t.Fatalf("Schedule returned error: %v", err)
	}
	if plan.FastOnTime != 2 || plan.NormalOnTime != 2 {
		t.Fatalf("on-time counts = fast %d normal %d, want 2/2; fragments=%+v",
			plan.FastOnTime, plan.NormalOnTime, plan.Fragments)
	}

	want := []struct {
		id    string
		start int
		end   int
		data  int
		final bool
	}{
		{"N1", 0, 3, 1, false},
		{"F1", 3, 6, 1, true},
		{"F2", 6, 9, 1, true},
		{"N1", 9, 13, 2, true},
		{"N2", 13, 16, 1, true},
	}
	if len(plan.Fragments) != len(want) {
		t.Fatalf("fragment count = %d (%+v), want %d", len(plan.Fragments), plan.Fragments, len(want))
	}
	for i, w := range want {
		got := plan.Fragments[i]
		if got.FrameID != w.id || got.Start != w.start || got.End != w.end || got.Data != w.data || got.Final != w.final {
			t.Fatalf("fragment %d = %+v, want %+v", i, got, w)
		}
		if got.Bytes != got.Data+got.Overhead {
			t.Fatalf("fragment %d Bytes=%d, want data+overhead", i, got.Bytes)
		}
	}
	if plan.OverheadBytes != 10 {
		t.Fatalf("OverheadBytes = %d, want 10", plan.OverheadBytes)
	}
	if plan.OccupiedBytes != 16 {
		t.Fatalf("OccupiedBytes = %d, want 16", plan.OccupiedBytes)
	}
	if plan.Makespan != 16 {
		t.Fatalf("Makespan = %d, want 16", plan.Makespan)
	}

	// Counterfactual: split N1 into 1+1+1 before the fast FIFO queue. The
	// extra overhead slice delays both fast frames and both normal frames.
	extra := []egress.Fragment{
		{FrameID: "N1", Class: egress.Normal, Start: 0, Overhead: 2, Data: 1, End: 3, Bytes: 3},
		{FrameID: "N1", Class: egress.Normal, Start: 3, Overhead: 2, Data: 1, End: 6, Bytes: 3},
		{FrameID: "F1", Class: egress.Fast, Start: 6, Overhead: 2, Data: 1, End: 9, Bytes: 3, Final: true},
		{FrameID: "F2", Class: egress.Fast, Start: 9, Overhead: 2, Data: 1, End: 12, Bytes: 3, Final: true},
		{FrameID: "N1", Class: egress.Normal, Start: 12, Overhead: 2, Data: 1, End: 15, Bytes: 3, Final: true},
		{FrameID: "N2", Class: egress.Normal, Start: 15, Overhead: 2, Data: 1, End: 18, Bytes: 3, Final: true},
	}
	ev, err := egress.CheckFragments(cfg, extra)
	if err != nil {
		t.Fatalf("CheckFragments error: %v", err)
	}
	if !ev.Valid() {
		t.Fatalf("extra-slice counterfactual should be structurally/gate valid: %+v", ev.Violations)
	}
	if ev.FastOnTime != 0 || ev.NormalOnTime != 0 {
		t.Fatalf("extra slice on-time = fast %d normal %d, want 0/0", ev.FastOnTime, ev.NormalOnTime)
	}
	for _, id := range []string{"F1", "F2", "N1", "N2"} {
		r := ev.Plan.FrameByID[id]
		if r.OnTime {
			t.Fatalf("frame %s unexpectedly on time at %d (deadline %d)", id, r.Completion, r.Deadline)
		}
	}
}

func TestMillionPeriodIdlePrefixIsPureTranslation(t *testing.T) {
	baseCfg := egress.Config{
		Table: &egress.GateTable{Entries: []egress.GateEntry{
			{Duration: 4, Gates: egress.GateNormal},
			{Duration: 3, Gates: egress.GateBoth},
			{Duration: 3, Gates: egress.GateBoth},
		}},
		Frames:     fourShortFrames(),
		MinNonLast: 1,
		Overhead:   1,
	}
	base, err := egress.Schedule(baseCfg)
	if err != nil {
		t.Fatalf("base Schedule: %v", err)
	}

	const reps = 1_000_000
	shift := reps * baseCfg.Table.EffectivePeriod()
	shiftedCfg := baseCfg
	shiftedCfg.Frames = append([]egress.Frame(nil), baseCfg.Frames...)
	for i := range shiftedCfg.Frames {
		shiftedCfg.Frames[i].Arrival += shift
		shiftedCfg.Frames[i].Deadline += shift
	}
	shifted, err := egress.Schedule(shiftedCfg)
	if err != nil {
		t.Fatalf("shifted Schedule: %v", err)
	}
	if shifted.FastOnTime != base.FastOnTime || shifted.NormalOnTime != base.NormalOnTime ||
		shifted.OverheadBytes != base.OverheadBytes {
		t.Fatalf("shift changed objective: base F%d/N%d/OH%d shifted F%d/N%d/OH%d",
			base.FastOnTime, base.NormalOnTime, base.OverheadBytes,
			shifted.FastOnTime, shifted.NormalOnTime, shifted.OverheadBytes)
	}
	if len(shifted.Fragments) != len(base.Fragments) {
		t.Fatalf("fragment length changed: %d vs %d", len(shifted.Fragments), len(base.Fragments))
	}
	for i := range base.Fragments {
		b, s := base.Fragments[i], shifted.Fragments[i]
		if s.Start != b.Start+shift || s.End != b.End+shift {
			t.Fatalf("fragment %d shifted mismatch: base [%d,%d), shifted [%d,%d), want +%d",
				i, b.Start, b.End, s.Start, s.End, shift)
		}
		s.Start -= shift
		s.End -= shift
		if s != b {
			t.Fatalf("fragment %d non-time fields changed: base %+v shifted %+v", i, b, s)
		}
	}
	for _, bf := range base.Frames {
		sf := shifted.FrameByID[bf.ID]
		if sf.Completion != bf.Completion+shift {
			t.Fatalf("frame %s completion %d, want %d+%d", bf.ID, sf.Completion, bf.Completion, shift)
		}
		if sf.OnTime != bf.OnTime {
			t.Fatalf("frame %s on-time changed under translation", bf.ID)
		}
	}
}

func TestUnsendableWhenNoMinimumLegalFragmentFits(t *testing.T) {
	frames := []egress.Frame{{ID: "N-long", Class: egress.Normal, Arrival: 0, Length: 3, Deadline: 100}}

	// Window length 2 with overhead 2 leaves zero data bytes.
	_, err := egress.Schedule(egress.Config{
		Table:      &egress.GateTable{Entries: []egress.GateEntry{{Duration: 2, Gates: egress.GateNormal}, {Duration: 3, Gates: egress.GateClosed}}},
		Frames:     frames,
		MinNonLast: 2,
		Overhead:   2,
	})
	var un *egress.UnsendableError
	if !errors.As(err, &un) || !errors.Is(err, egress.ErrUnsendable) {
		t.Fatalf("zero-payload error = %v, want UnsendableError/ErrUnsendable", err)
	}

	// One payload byte fits, but a length-3 frame cannot be whole and M=2.
	_, err = egress.Schedule(egress.Config{
		Table:      &egress.GateTable{Entries: []egress.GateEntry{{Duration: 3, Gates: egress.GateNormal}, {Duration: 4, Gates: egress.GateClosed}}},
		Frames:     frames,
		MinNonLast: 2,
		Overhead:   2,
	})
	if !errors.As(err, &un) {
		t.Fatalf("minimum-fragment error = %v, want UnsendableError", err)
	}

	// A one-byte final remainder may be smaller than MinNonLast and is legal.
	plan, err := egress.Schedule(egress.Config{
		Table:      &egress.GateTable{Entries: []egress.GateEntry{{Duration: 4, Gates: egress.GateNormal}, {Duration: 3, Gates: egress.GateClosed}}},
		Frames:     []egress.Frame{{ID: "N-one", Class: egress.Normal, Length: 1, Deadline: 4}},
		MinNonLast: 2,
		Overhead:   2,
	})
	if err != nil {
		t.Fatalf("one-byte final fragment should be sendable: %v", err)
	}
	if got := plan.FrameByID["N-one"].Completion; got != 3 {
		t.Fatalf("completion = %d, want 3", got)
	}
}

func TestSharedFastWindowIsAlsoUsableByNormal(t *testing.T) {
	table := &egress.GateTable{Entries: []egress.GateEntry{{Duration: 10, Gates: egress.GateBoth}}}
	for tick := 0; tick < 10; tick++ {
		if !table.GateOpenAt(tick, egress.Fast) || !table.GateOpenAt(tick, egress.Normal) {
			t.Fatalf("tick %d is not shared by both classes", tick)
		}
	}
}

func TestOriginModelsUnknownPhaseOffset(t *testing.T) {
	table := &egress.GateTable{
		Origin: 1,
		Entries: []egress.GateEntry{
			{Duration: 2, Gates: egress.GateNormal},
			{Duration: 3, Gates: egress.GateClosed},
		},
	}
	if !table.GateOpenAt(2, egress.Normal) {
		t.Fatal("Origin=1 should place normal opening at [1,3), so tick 2 is open")
	}
	if table.GateOpenAt(0, egress.Normal) {
		t.Fatal("tick 0 should be in the closed portion of the phase-shifted period")
	}
	start, ok := table.EarliestFit(0, 1, 0, egress.Normal)
	if !ok || start != 1 {
		t.Fatalf("EarliestFit(0) = %d,%v; want start 1", start, ok)
	}
}

func TestGuardBandForbidsFinishingIntoSwitch(t *testing.T) {
	table := &egress.GateTable{
		Guard: 1,
		Entries: []egress.GateEntry{
			{Duration: 5, Gates: egress.GateNormal},
			{Duration: 5, Gates: egress.GateClosed},
		},
	}
	// OH=2, data=2 ends at 4; End+Guard=5 is exactly legal.
	if !table.FitsAt(0, 2, 2, egress.Normal) {
		t.Fatal("fragment ending at tick 4 with guard 1 should fit [0,5)")
	}
	// Starting one tick later ends at 5 and leaves no guard before close 5.
	if table.FitsAt(1, 2, 2, egress.Normal) {
		t.Fatal("fragment ending at close does not leave required guard")
	}

	plan, err := egress.Schedule(egress.Config{
		Table:      table,
		Frames:     []egress.Frame{{ID: "N", Class: egress.Normal, Length: 2, Deadline: 30}},
		MinNonLast: 1,
		Overhead:   2,
	})
	if err != nil {
		t.Fatalf("Schedule: %v", err)
	}
	if plan.Fragments[0].Start != 0 || plan.Fragments[0].End != 4 {
		t.Fatalf("first fragment = %+v, want [0,4)", plan.Fragments[0])
	}
}
