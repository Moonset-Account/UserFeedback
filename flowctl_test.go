package flowctl

import (
	"fmt"
	"strings"
	"testing"
)

func mustController(t *testing.T, cfg Config) *Controller {
	t.Helper()
	c, err := New(cfg)
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	return c
}

func assertSnapshotInvariants(t *testing.T, c *Controller) Snapshot {
	t.Helper()
	if err := c.CheckInvariants(); err != nil {
		t.Fatalf("invariant violation: %v", err)
	}
	s := c.Snapshot()
	var used, reserved Bytes
	for _, f := range s.Flows {
		used += f.Used
		reserved += f.Reservation
		if f.Used < 0 || f.Reservation < 0 {
			t.Fatalf("negative flow accounting: %+v", f)
		}
	}
	if used != s.Used || reserved != s.Reserved {
		t.Fatalf("snapshot sums mismatch: %+v flows used=%d reserved=%d", s, used, reserved)
	}
	if s.SharedMargin != s.Capacity-s.Used-s.Reserved {
		t.Fatalf("bad shared margin: %+v", s)
	}
	return s
}

func TestConfigValidation(t *testing.T) {
	tests := []struct {
		name string
		cfg  Config
		want string
	}{
		{"capacity", Config{Capacity: 0, PauseThreshold: 4, ResumeThreshold: 1}, "capacity"},
		{"pause threshold", Config{Capacity: 10, PauseThreshold: 0, ResumeThreshold: 0}, "pause threshold"},
		{"negative resume", Config{Capacity: 10, PauseThreshold: 4, ResumeThreshold: -1}, "resume threshold"},
		{"hysteresis", Config{Capacity: 10, PauseThreshold: 4, ResumeThreshold: 4}, "below pause"},
		{"pause over capacity", Config{Capacity: 10, PauseThreshold: 11, ResumeThreshold: 2}, "must not exceed capacity"},
		{"negative inflight", Config{Capacity: 10, PauseThreshold: 4, ResumeThreshold: 2, InFlight: -1}, "in-flight"},
		{"negative port inflight", Config{
			Capacity: 10, PauseThreshold: 4, ResumeThreshold: 2,
			InFlightByPort: map[PortID]Bytes{1: -2},
		}, "port 1"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := New(tt.cfg)
			if err == nil {
				t.Fatal("expected validation error")
			}
			if tt.want != "" && !strings.Contains(err.Error(), tt.want) {
				t.Fatalf("error %q does not contain %q", err.Error(), tt.want)
			}
		})
	}

	valid := Config{Capacity: 10, PauseThreshold: 1, ResumeThreshold: 0, InFlight: 0}
	if _, err := New(valid); err != nil {
		t.Fatalf("zero watermark boundary should be valid: %v", err)
	}
}

func TestPauseThresholdReservationAndWriteOff(t *testing.T) {
	c := mustController(t, Config{
		Capacity: 10, PauseThreshold: 6, ResumeThreshold: 2, InFlight: 2,
	})
	key := Key{Port: 1, Priority: 0}

	ar, err := c.Arrive(Frame{Bytes: 5, Port: key.Port, Priority: key.Priority})
	if err != nil || !ar.Accepted || ar.Pause != nil {
		t.Fatalf("below-threshold arrival = %+v, %v", ar, err)
	}
	if s := assertSnapshotInvariants(t, c); s.Used != 5 || s.Reserved != 0 {
		t.Fatalf("unexpected state before threshold: used=%d reserved=%d", s.Used, s.Reserved)
	}

	ar, err = c.Arrive(Frame{Bytes: 1, Port: key.Port, Priority: key.Priority})
	if err != nil {
		t.Fatal(err)
	}
	if ar.Pause == nil || ar.Pause.Key != key {
		t.Fatalf("threshold-boundary frame did not pause %s: %+v", key, ar)
	}
	if ar.Pause.InFlight != 2 || ar.Pause.Occupancy != 6 || ar.Pause.Reservation != 2 {
		t.Fatalf("bad pause directive: %+v", ar.Pause)
	}
	if ar.Pause.SharedMargin != 2 {
		t.Fatalf("pause shared margin = %d, want 2", ar.Pause.SharedMargin)
	}

	// One more byte while paused must not create a duplicate pause.
	if _, err := c.Arrive(Frame{Bytes: 1, Port: key.Port, Priority: key.Priority}); err != nil {
		t.Fatal(err)
	}
	if got := len(c.Pauses()); got != 1 {
		t.Fatalf("pause count = %d, want 1", got)
	}

	// The preceding one-byte arrival wrote one of the two promised bytes off,
	// so this frame writes off the remaining one and then adds one new byte.
	ar, err = c.Arrive(Frame{Bytes: 2, Port: key.Port, Priority: key.Priority})
	if err != nil {
		t.Fatal(err)
	}
	if !ar.Accepted || ar.WrittenOff != 1 {
		t.Fatalf("expected one remaining reservation write-off: %+v", ar)
	}
	if f, _ := c.Flow(key); f.Used != 9 || f.Reservation != 0 {
		t.Fatalf("unexpected state after write-off: %+v", f)
	}

	// Fresh controller for the exact full-reservation write-off identity.
	c = mustController(t, Config{Capacity: 10, PauseThreshold: 6, ResumeThreshold: 2, InFlight: 2})
	_, _ = c.Arrive(Frame{Bytes: 6, Port: 1})
	before := c.Snapshot()
	ar, err = c.Arrive(Frame{Bytes: 2, Port: 1})
	if err != nil {
		t.Fatal(err)
	}
	if !ar.Accepted || ar.WrittenOff != 2 {
		t.Fatalf("write-off = %+v, want 2 written off", ar)
	}
	after := assertSnapshotInvariants(t, c)
	if after.Used != before.Used+2 || after.Reserved != 0 {
		t.Fatalf("bad write-off state: before=%+v after=%+v", before, after)
	}
	if after.SharedMargin != before.SharedMargin {
		t.Fatalf("reserved arrival changed shared margin: before=%d after=%d", before.SharedMargin, after.SharedMargin)
	}
}

func TestPartialReservationWriteOff(t *testing.T) {
	c := mustController(t, Config{Capacity: 12, PauseThreshold: 6, ResumeThreshold: 2, InFlight: 3})
	key := Key{Port: 1, Priority: 2}
	_, _ = c.Arrive(Frame{Bytes: 6, Port: 1, Priority: 2})
	ar, err := c.Arrive(Frame{Bytes: 2, Port: 1, Priority: 2})
	if err != nil {
		t.Fatal(err)
	}
	if ar.WrittenOff != 2 {
		t.Fatalf("written off = %d, want 2", ar.WrittenOff)
	}
	f, ok := c.Flow(key)
	if !ok || f.Reservation != 1 {
		t.Fatalf("flow = %+v ok=%v, want residual reservation 1", f, ok)
	}
}

func TestResumeLowWaterBoundary(t *testing.T) {
	c := mustController(t, Config{Capacity: 10, PauseThreshold: 6, ResumeThreshold: 2, InFlight: 2})
	key := Key{Port: 1, Priority: 0}
	_, _ = c.Arrive(Frame{Bytes: 6, Port: 1})
	_, _ = c.Arrive(Frame{Bytes: 2, Port: 1}) // used 8, reservation 0

	if _, err := c.Depart(Departure{Key: key, Bytes: 5}); err != nil { // used 3
		t.Fatal(err)
	}
	chk := c.CheckResume(key)
	if chk.Eligible || !strings.Contains(chk.Reason, "exceeds resume threshold") {
		t.Fatalf("used=3 should be above low water: %+v", chk)
	}
	if d, ok := c.ResumeIfEligible(key); ok {
		t.Fatalf("unexpected resume at used=3: %+v", d)
	}

	if _, err := c.Depart(Departure{Key: key, Bytes: 1}); err != nil { // used 2 boundary
		t.Fatal(err)
	}
	chk = c.CheckResume(key)
	if !chk.Eligible {
		t.Fatalf("used=2 should satisfy low water and margin: %+v", chk)
	}
	d, ok := c.ResumeIfEligible(key)
	if !ok || d == nil || d.Kind != "resume" {
		t.Fatalf("boundary resume failed: %+v ok=%v", d, ok)
	}
	if d.SharedMargin != 8 {
		t.Fatalf("resume margin = %d, want 8", d.SharedMargin)
	}
	if _, ok := c.ResumeIfEligible(key); ok {
		t.Fatal("second resume must not be emitted for an already-resumed flow")
	}
}

func TestResumeBlockedBySharedInFlightMargin(t *testing.T) {
	c := mustController(t, Config{Capacity: 12, PauseThreshold: 6, ResumeThreshold: 2, InFlight: 2})
	a := Key{Port: 1, Priority: 0}
	b := Key{Port: 2, Priority: 0}

	_, _ = c.Arrive(Frame{Bytes: 6, Port: a.Port})  // A=6, RA=2
	_, _ = c.Arrive(Frame{Bytes: 2, Port: a.Port})  // A=8, RA=0
	_, err := c.Depart(Departure{Key: a, Bytes: 6}) // A=2
	if err != nil {
		t.Fatal(err)
	}

	_, _ = c.Arrive(Frame{Bytes: 6, Port: b.Port}) // total=8, B=6 RB=2, margin=2
	if m := c.SharedMargin(); m != 2 {
		t.Fatalf("margin after B pause = %d, want 2", m)
	}
	_, _ = c.Arrive(Frame{Bytes: 3, Port: b.Port}) // write off 2; one unreserved byte
	if m := c.SharedMargin(); m != 1 {
		t.Fatalf("margin after unreserved B byte = %d, want 1", m)
	}

	chk := c.CheckResume(a)
	if chk.Eligible || !strings.Contains(chk.Reason, "shared margin") {
		t.Fatalf("A should be blocked by shared pool: %+v", chk)
	}

	if _, err := c.Depart(Departure{Key: b, Bytes: 1}); err != nil {
		t.Fatal(err)
	}
	if m := c.SharedMargin(); m != 2 {
		t.Fatalf("margin after B drain = %d, want 2", m)
	}
	if d, ok := c.ResumeIfEligible(a); !ok {
		t.Fatalf("A should resume at exact in-flight margin: %+v", c.CheckResume(a))
	} else if d.SharedMargin != 2 {
		t.Fatalf("resume directive margin = %d, want 2", d.SharedMargin)
	}
}

func TestDroppedFramesAreWholeAndRecorded(t *testing.T) {
	c := mustController(t, Config{Capacity: 10, PauseThreshold: 6, ResumeThreshold: 2, InFlight: 2})
	ar, err := c.Arrive(Frame{Bytes: 10, Port: 1})
	if err != nil || !ar.Accepted {
		t.Fatalf("exact-capacity frame should be accepted: %+v %v", ar, err)
	}
	before := assertSnapshotInvariants(t, c)

	ar, err = c.Arrive(Frame{Bytes: 1, Port: 9, Priority: 7})
	if err != nil {
		t.Fatal(err)
	}
	if ar.Accepted || ar.Drop == nil || ar.Drop.Reason != DropBufferFull {
		t.Fatalf("overflow frame should be recorded buffer_full: %+v", ar)
	}
	after := assertSnapshotInvariants(t, c)
	if after.Used != before.Used || after.EventCount != before.EventCount+1 {
		t.Fatalf("drop mutated state unexpectedly: before=%+v after=%+v", before, after)
	}
	drops := c.Drops()
	if len(drops) != 1 || drops[0].Frame.Port != 9 || drops[0].OccupancyBefore != 10 {
		t.Fatalf("bad drop records: %+v", drops)
	}

	giant := mustController(t, Config{Capacity: 10, PauseThreshold: 6, ResumeThreshold: 2})
	ar, _ = giant.Arrive(Frame{Bytes: 11, Port: 1})
	if ar.Accepted || ar.Drop.Reason != DropFrameTooLarge {
		t.Fatalf("oversized frame reason = %q, want frame_too_large", ar.Drop.Reason)
	}
	if s := giant.Snapshot(); s.Used != 0 || len(s.Flows) != 0 {
		t.Fatalf("rejected oversized frame created state: %+v", s)
	}

	partial := mustController(t, Config{Capacity: 10, PauseThreshold: 9, ResumeThreshold: 4})
	_, _ = partial.Arrive(Frame{Bytes: 7, Port: 1})
	ar, _ = partial.Arrive(Frame{Bytes: 6, Port: 1}) // only three physical bytes free
	if ar.Accepted {
		t.Fatal("frame exceeding free space was accepted")
	}
	if f, _ := partial.Flow(Key{Port: 1}); f.Used != 7 {
		t.Fatalf("whole-frame drop partially admitted bytes: used=%d", f.Used)
	}
}

func TestProtectReservationsAdmissionPolicy(t *testing.T) {
	c := mustController(t, Config{
		Capacity: 10, PauseThreshold: 4, ResumeThreshold: 1, InFlight: 2,
		Admission: ProtectReservations,
	})
	a := Key{Port: 1}
	b := Key{Port: 2}

	_, _ = c.Arrive(Frame{Bytes: 4, Port: 1}) // A reservation 2, margin 4
	ar, _ := c.Arrive(Frame{Bytes: 5, Port: 2})
	if ar.Accepted || ar.Drop == nil || ar.Drop.Reason != DropReservedHeadroom {
		t.Fatalf("B should not consume A's reserved headroom: %+v", ar)
	}
	if s := c.Snapshot(); s.Used != 4 {
		t.Fatalf("reservation-policy rejection mutated used=%d", s.Used)
	}

	ar, _ = c.Arrive(Frame{Bytes: 4, Port: 2}) // exactly the four uncommitted bytes
	if !ar.Accepted {
		t.Fatalf("B should fit in uncommitted margin: %+v", ar.Drop)
	}
	_, _ = c.Arrive(Frame{Bytes: 2, Port: 1}) // A consumes its own reservation
	if err := c.CheckInvariants(); err != nil {
		t.Fatal(err)
	}
	if f, _ := c.Flow(a); f.Used != 6 || f.Reservation != 0 {
		t.Fatalf("A=%+v, want used6 reservation0", f)
	}
	if f, _ := c.Flow(b); !f.Paused || f.Reservation != 2 {
		t.Fatalf("B should have its own pause reservation: %+v", f)
	}

	ar, _ = c.Arrive(Frame{Bytes: 1, Port: 3})
	if ar.Accepted || ar.Drop.Reason != DropBufferFull {
		t.Fatalf("physical overflow should still be recorded as buffer full: %+v", ar)
	}
}

func TestInterleavedPortsPrioritiesAndWriteOffs(t *testing.T) {
	c := mustController(t, Config{
		Capacity: 32, PauseThreshold: 8, ResumeThreshold: 4,
		InFlight:       1,
		InFlightByPort: map[PortID]Bytes{1: 3, 2: 2},
	})
	a := Key{Port: 1, Priority: 0}
	b := Key{Port: 1, Priority: 1}
	d := Key{Port: 2, Priority: 0}

	for _, x := range []struct {
		key Key
		n   Bytes
	}{{a, 8}, {b, 8}, {d, 8}} {
		ar, err := c.Arrive(Frame{Bytes: x.n, Port: x.key.Port, Priority: x.key.Priority})
		if err != nil || ar.Pause == nil || ar.Pause.Key != x.key {
			t.Fatalf("expected pause for %s: %+v err=%v", x.key, ar, err)
		}
	}
	if s := assertSnapshotInvariants(t, c); s.Used != 24 || s.Reserved != 8 || s.SharedMargin != 0 {
		t.Fatalf("after three pauses: %+v", s)
	}

	// Arrivals are interleaved and write off only their own flow.
	_, _ = c.Arrive(Frame{Bytes: 3, Port: a.Port, Priority: a.Priority})
	_, _ = c.Arrive(Frame{Bytes: 2, Port: d.Port, Priority: d.Priority})
	if f, _ := c.Flow(b); f.Reservation != 3 {
		t.Fatalf("B reservation changed by another flow: %+v", f)
	}
	_, _ = c.Arrive(Frame{Bytes: 3, Port: b.Port, Priority: b.Priority})
	s := assertSnapshotInvariants(t, c)
	if s.Used != 32 || s.Reserved != 0 || s.SharedMargin != 0 {
		t.Fatalf("after all in-flight write-offs: used=%d reserved=%d margin=%d", s.Used, s.Reserved, s.SharedMargin)
	}

	if _, err := c.Depart(Departure{Key: a, Bytes: 7}); err != nil {
		t.Fatal(err)
	}
	resumes := c.ReconcileResumes()
	if len(resumes) != 1 || resumes[0].Key != a {
		t.Fatalf("only A should resume, got %+v", resumes)
	}
	if _, err := c.Depart(Departure{Key: b, Bytes: 7}); err != nil {
		t.Fatal(err)
	}
	resumes = c.ReconcileResumes()
	if len(resumes) != 1 || resumes[0].Key != b {
		t.Fatalf("only B should resume, got %+v", resumes)
	}
	if _, err := c.Depart(Departure{Key: d, Bytes: 6}); err != nil {
		t.Fatal(err)
	}
	resumes = c.ReconcileResumes()
	if len(resumes) != 1 || resumes[0].Key != d {
		t.Fatalf("only D should resume, got %+v", resumes)
	}
	if len(c.Pauses()) != 3 || len(c.Resumes()) != 3 {
		t.Fatalf("directive counts pauses=%d resumes=%d", len(c.Pauses()), len(c.Resumes()))
	}
}

func TestZeroInFlightResumeBoundary(t *testing.T) {
	c := mustController(t, Config{Capacity: 10, PauseThreshold: 4, ResumeThreshold: 1, InFlight: 0})
	key := Key{Port: 1}
	ar, _ := c.Arrive(Frame{Bytes: 4, Port: 1})
	if ar.Pause == nil || ar.Pause.InFlight != 0 || ar.Pause.Reservation != 0 {
		t.Fatalf("bad zero-inflight pause: %+v", ar.Pause)
	}
	_, _ = c.Depart(Departure{Key: key, Bytes: 3})
	if d, ok := c.ResumeIfEligible(key); !ok {
		t.Fatalf("zero-inflight flow should resume at low water: %+v", c.CheckResume(key))
	} else if d.SharedMargin != 9 {
		t.Fatalf("margin=%d want9", d.SharedMargin)
	}
}

func TestDrainOrderAndRoundEvents(t *testing.T) {
	c := mustController(t, Config{Capacity: 20, PauseThreshold: 10, ResumeThreshold: 2, InFlight: 1})
	_, _ = c.Arrive(Frame{Bytes: 5, Port: 1, Priority: 0})
	_, _ = c.Arrive(Frame{Bytes: 5, Port: 2, Priority: 1})
	rr, err := c.Step(Round{EgressAvailable: 6})
	if err != nil {
		t.Fatal(err)
	}
	if rr.Egress == nil || len(rr.Egress.Departures) != 2 {
		t.Fatalf("default drain allocations = %+v", rr.Egress)
	}
	if rr.Egress.Departures[0].Key != (Key{Port: 1, Priority: 0}) || rr.Egress.Departures[0].Bytes != 5 {
		t.Fatalf("first drain = %+v", rr.Egress.Departures[0])
	}
	if rr.Egress.Departures[1].Bytes != 1 || rr.Egress.Departures[1].Key.Port != 2 {
		t.Fatalf("second drain = %+v", rr.Egress.Departures[1])
	}

	pc := mustController(t, Config{
		Capacity: 20, PauseThreshold: 10, ResumeThreshold: 2, InFlight: 1,
		DrainOrder: DrainPriorityDesc,
	})
	_, _ = pc.Arrive(Frame{Bytes: 5, Port: 1, Priority: 0})
	_, _ = pc.Arrive(Frame{Bytes: 5, Port: 2, Priority: 7})
	dr, err := pc.Drain(6)
	if err != nil {
		t.Fatal(err)
	}
	if dr.Departures[0].Key.Priority != 7 || dr.Departures[0].Bytes != 5 {
		t.Fatalf("priority drain first = %+v", dr.Departures[0])
	}
	if dr.Departures[1].Key.Priority != 0 || dr.Departures[1].Bytes != 1 {
		t.Fatalf("priority drain second = %+v", dr.Departures[1])
	}
}

func TestArrivalsFirstRoundPauseThenResume(t *testing.T) {
	c := mustController(t, Config{Capacity: 20, PauseThreshold: 6, ResumeThreshold: 2, InFlight: 2})
	rr, err := c.Step(Round{
		Arrivals:        []Frame{{Bytes: 6, Port: 1}},
		EgressAvailable: 6,
		Order:           ArrivalsFirst,
	})
	if err != nil {
		t.Fatal(err)
	}
	gotKinds := make([]EventKind, 0, len(rr.Events))
	for _, ev := range rr.Events {
		gotKinds = append(gotKinds, ev.Kind)
	}
	want := []EventKind{EventAccepted, EventPaused, EventDeparted, EventResumed}
	if fmt.Sprint(gotKinds) != fmt.Sprint(want) {
		t.Fatalf("event order = %v, want %v", gotKinds, want)
	}
}

func TestEgressFirstRoundRePauseWritesResidualReservation(t *testing.T) {
	c := mustController(t, Config{Capacity: 20, PauseThreshold: 6, ResumeThreshold: 2, InFlight: 2})
	_, _ = c.Arrive(Frame{Bytes: 6, Port: 1})
	rr, err := c.Step(Round{
		Arrivals:        []Frame{{Bytes: 6, Port: 1}},
		EgressAvailable: 6,
		Order:           EgressFirst,
	})
	if err != nil {
		t.Fatal(err)
	}
	kinds := []EventKind{}
	for _, ev := range rr.Events {
		kinds = append(kinds, ev.Kind)
	}
	want := []EventKind{EventDeparted, EventResumed, EventAccepted, EventPaused}
	if fmt.Sprint(kinds) != fmt.Sprint(want) {
		t.Fatalf("event order=%v want%v", kinds, want)
	}
	f, _ := c.Flow(Key{Port: 1})
	if f.Used != 6 || f.Reservation != 2 || !f.Paused {
		t.Fatalf("unexpected re-pause state: %+v", f)
	}
	if f.PauseThreshold != 6 || f.ResumeThreshold != 2 {
		t.Fatalf("threshold snapshot wrong: %+v", f)
	}
}

func TestStepInvalidRoundIsAtomic(t *testing.T) {
	c := mustController(t, Config{Capacity: 10, PauseThreshold: 6, ResumeThreshold: 2})
	_, _ = c.Arrive(Frame{Bytes: 3, Port: 1})
	before := c.Snapshot()

	_, err := c.Step(Round{Arrivals: []Frame{{Bytes: 1}, {Bytes: -1}}})
	if err == nil {
		t.Fatal("expected invalid frame error")
	}
	after := c.Snapshot()
	if after.Used != before.Used || after.EventCount != before.EventCount || after.DropCount != before.DropCount {
		t.Fatalf("invalid Step mutated state: before=%+v after=%+v", before, after)
	}

	_, err = c.Step(Round{Departures: []Departure{{Key: Key{Port: 1}, Bytes: 99}}})
	if err == nil {
		t.Fatal("expected over-departure error")
	}
	if after := c.Snapshot(); after.Used != before.Used {
		t.Fatalf("over-departure mutated used to %d", after.Used)
	}
}

func TestSyntheticInterleavedSimulationInvariants(t *testing.T) {
	c := mustController(t, Config{
		Capacity:        100,
		PauseThreshold:  30,
		ResumeThreshold: 10,
		InFlight:        8,
		InFlightByPort:  map[PortID]Bytes{0: 12, 3: 4},
	})
	keys := []Key{{0, 0}, {0, 1}, {1, 0}, {2, 7}, {3, 4}}

	// Deterministic linear congruential generator keeps the test dependency-free.
	seed := int64(123456789)
	next := func(n int64) int64 {
		seed = (seed*1103515245 + 12345) & 0x7fffffff
		return seed % n
	}

	for i := 0; i < 1000; i++ {
		k := keys[next(int64(len(keys)))]
		n := Bytes(1 + next(40))
		ar, err := c.Arrive(Frame{Bytes: n, Port: k.Port, Priority: k.Priority})
		if err != nil {
			t.Fatal(err)
		}
		if ar.Accepted {
			if err := c.CheckInvariants(); err != nil {
				t.Fatalf("iteration %d arrival %+v: %v", i, ar.Frame, err)
			}
		}

		switch next(3) {
		case 0:
			_, err := c.Drain(Bytes(next(25)))
			if err != nil {
				t.Fatal(err)
			}
		case 1:
			if f, ok := c.Flow(k); ok && f.Used > 0 {
				amount := Bytes(next(int64(f.Used) + 1))
				_, err := c.Depart(Departure{Key: k, Bytes: amount})
				if err != nil {
					t.Fatal(err)
				}
			}
		}
		c.ReconcileResumes()
		if err := c.CheckInvariants(); err != nil {
			t.Fatalf("iteration %d: %v", i, err)
		}
	}

	for _, d := range c.Drops() {
		if d.OccupancyBefore+d.Frame.Bytes <= c.Config().Capacity {
			t.Fatalf("PhysicalOnly drop was not physically full: %+v", d)
		}
	}
	s := c.Snapshot()
	if s.Used < 0 || s.Used > s.Capacity {
		t.Fatalf("synthetic simulation left used=%d", s.Used)
	}
	t.Logf("synthetic run: used=%d reserved=%d drops=%d pauses=%d resumes=%d events=%d",
		s.Used, s.Reserved, s.DropCount, s.PauseCount, s.ResumeCount, s.EventCount)
}

func TestInFlightPortOverrideAndDefault(t *testing.T) {
	c := mustController(t, Config{
		Capacity: 100, PauseThreshold: 4, ResumeThreshold: 1,
		InFlight:       8,
		InFlightByPort: map[PortID]Bytes{0: 12, 3: 4},
	})
	for _, p := range []PortID{0, 1, 3} {
		_, _ = c.Arrive(Frame{Bytes: 4, Port: p})
	}
	want := map[PortID]Bytes{0: 12, 1: 8, 3: 4}
	for p, n := range want {
		f, ok := c.Flow(Key{Port: p})
		if !ok || f.InFlight != n || f.Reservation != n {
			t.Fatalf("port %d flow=%+v ok=%v, want inflight/reservation %d", p, f, ok, n)
		}
	}
}

func TestStepExactDepartures(t *testing.T) {
	c := mustController(t, Config{Capacity: 10, PauseThreshold: 8, ResumeThreshold: 2})
	key := Key{Port: 4, Priority: 2}
	_, _ = c.Arrive(Frame{Bytes: 5, Port: key.Port, Priority: key.Priority})

	rr, err := c.Step(Round{Departures: []Departure{{Key: key, Bytes: 3}}})
	if err != nil {
		t.Fatal(err)
	}
	if rr.Egress == nil || rr.Egress.Sent != 3 || len(rr.Egress.Departures) != 1 {
		t.Fatalf("exact departure result = %+v", rr.Egress)
	}
	if f, _ := c.Flow(key); f.Used != 2 {
		t.Fatalf("used after exact departure = %d, want 2", f.Used)
	}
}
