package crosssection

import (
	"reflect"
	"sort"
	"testing"
)

// ev is a compact test fixture constructor.
func ev(seq int, kind EventKind, id string) Event {
	return Event{Seq: seq, Kind: kind, MessageID: id}
}

// validateCut independently re-checks every cross-cut constraint against the
// input logs, so the tests do not merely trust the implementation's bookkeeping.
func validateCut(t *testing.T, services []ServiceLog, res *Result) {
	t.Helper()

	byName := map[string]ServiceLog{}
	sendOf := map[string]eventRef{}
	recvOf := map[string][]eventRef{}
	for _, svc := range services {
		byName[svc.Name] = svc
		for _, e := range svc.Events {
			ref := eventRef{service: svc.Name, seq: e.Seq}
			switch e.Kind {
			case Send:
				sendOf[e.MessageID] = ref
			case Receive:
				recvOf[e.MessageID] = append(recvOf[e.MessageID], ref)
			}
		}
	}

	if len(res.Cut) != len(services) {
		t.Fatalf("cut has %d entries, want %d", len(res.Cut), len(services))
	}

	missingIDs := map[string]bool{}
	for _, m := range res.MissingSource {
		missingIDs[m.MessageID] = true
	}

	for _, svc := range services {
		c := res.Cut[svc.Name]
		if c < 0 {
			t.Errorf("service %q cut %d is negative", svc.Name, c)
		}
		if c > svc.MaxSeq {
			t.Errorf("service %q cut %d exceeds cap %d", svc.Name, c, svc.MaxSeq)
		}
		last := 0
		for _, e := range svc.Events {
			if e.Seq > last {
				last = e.Seq
			}
			if e.Seq <= c {
				// Retained event: verify the message rule.
				if e.Kind == Receive {
					if missingIDs[e.MessageID] {
						t.Errorf("service %q retains missing-source receive %q at seq %d",
							svc.Name, e.MessageID, e.Seq)
					}
					send, ok := sendOf[e.MessageID]
					if !ok {
						t.Errorf("service %q retains receive %q at seq %d but no send exists",
							svc.Name, e.MessageID, e.Seq)
						continue
					}
					if res.Cut[send.service] < send.seq {
						t.Errorf("service %q retains receive %q at seq %d but send %s#%d is cut (cut=%d)",
							svc.Name, e.MessageID, e.Seq, send.service, send.seq,
							res.Cut[send.service])
					}
				}
			}
		}
		if c > last {
			t.Errorf("service %q cut %d exceeds last event seq %d", svc.Name, c, last)
		}
	}

	// Independently recompute the expected in-flight set.
	wantInFlight := map[string]bool{}
	for id, send := range sendOf {
		if res.Cut[send.service] < send.seq {
			continue
		}
		retained := false
		for _, recv := range recvOf[id] {
			if res.Cut[recv.service] >= recv.seq {
				retained = true
				break
			}
		}
		if !retained {
			wantInFlight[id] = true
		}
	}
	got := append([]string(nil), res.InFlight...)
	sort.Strings(got)
	wantList := make([]string, 0, len(wantInFlight))
	for id := range wantInFlight {
		wantList = append(wantList, id)
	}
	sort.Strings(wantList)
	if len(got) != len(wantList) {
		t.Errorf("in-flight = %v, want %v", got, wantList)
	} else {
		for i := range got {
			if got[i] != wantList[i] {
				t.Errorf("in-flight = %v, want %v", got, wantList)
				break
			}
		}
	}

	// Every orphan receive must be reported exactly once as missing-source.
	reported := map[string]int{}
	for _, m := range res.MissingSource {
		reported[m.MessageID]++
	}
	for id, refs := range recvOf {
		if _, ok := sendOf[id]; ok {
			continue
		}
		if reported[id] != len(refs) {
			t.Errorf("missing-source reports for %q = %d, want %d", id, reported[id], len(refs))
		}
	}
}

func TestAcceptableMaximalCut(t *testing.T) {
	services := []ServiceLog{
		{Name: "A", MaxSeq: 3, Events: []Event{
			ev(1, Send, "m1"),
			ev(2, Local, ""),
			ev(3, Send, "m2"),
		}},
		{Name: "B", MaxSeq: 3, Events: []Event{
			ev(1, Receive, "m1"), // receive at the earliest retained position
			ev(2, Send, "m3"),
			ev(3, Local, ""),
		}},
		{Name: "C", MaxSeq: 2, Events: []Event{
			ev(1, Receive, "m3"),
			ev(2, Local, ""),
		}},
	}

	res, err := Reconstruct(services)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Every cap is satisfiable simultaneously: no service retreats.
	wantCut := map[string]int{"A": 3, "B": 3, "C": 2}
	if !reflect.DeepEqual(res.Cut, wantCut) {
		t.Fatalf("cut = %v, want %v", res.Cut, wantCut)
	}
	// m2 is sent inside the section and never received.
	if !reflect.DeepEqual(res.InFlight, []string{"m2"}) {
		t.Errorf("in-flight = %v, want [m2]", res.InFlight)
	}
	if len(res.MissingSource) != 0 {
		t.Errorf("missing source = %v, want none", res.MissingSource)
	}
	validateCut(t, services, res)
}

func TestBlockedCutCascadesBackward(t *testing.T) {
	// A's cap hides the send of m1; B must retreat before its receive,
	// which hides B's send of m2 and forces C to retreat as well. The
	// naive cap cut (A1, B3, C3) is rejected; the fixed point is lower.
	services := []ServiceLog{
		{Name: "A", MaxSeq: 1, Events: []Event{
			ev(1, Local, ""),
			ev(2, Send, "m1"), // outside the cap: no send evidence on any cut A can reach
		}},
		{Name: "B", MaxSeq: 3, Events: []Event{
			ev(1, Receive, "m1"),
			ev(2, Local, ""),
			ev(3, Send, "m2"),
		}},
		{Name: "C", MaxSeq: 3, Events: []Event{
			ev(1, Local, ""),
			ev(2, Receive, "m2"),
			ev(3, Local, ""),
		}},
	}

	res, err := Reconstruct(services)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	wantCut := map[string]int{"A": 1, "B": 0, "C": 1}
	if !reflect.DeepEqual(res.Cut, wantCut) {
		t.Fatalf("cut = %v, want %v", res.Cut, wantCut)
	}
	if len(res.InFlight) != 0 {
		t.Errorf("in-flight = %v, want none (both sends are cut away)", res.InFlight)
	}
	if len(res.MissingSource) != 0 {
		t.Errorf("missing source = %v, want none (m1 has a send, merely beyond cap)",
			res.MissingSource)
	}
	validateCut(t, services, res)
}

func TestCascadeRequiresMultipleIterations(t *testing.T) {
	// Input order A, C, B: in the first pass C is checked while B still
	// holds its full cap, so the B -> C propagation only becomes visible
	// on a later outer iteration. This verifies the fixed-point loop does
	// not stop after a single sweep.
	services := []ServiceLog{
		{Name: "A", MaxSeq: 1, Events: []Event{
			ev(1, Local, ""),
			ev(2, Send, "m1"),
		}},
		{Name: "C", MaxSeq: 3, Events: []Event{
			ev(1, Local, ""),
			ev(2, Receive, "m2"),
			ev(3, Local, ""),
		}},
		{Name: "B", MaxSeq: 3, Events: []Event{
			ev(1, Receive, "m1"),
			ev(2, Send, "m2"),
			ev(3, Local, ""),
		}},
	}

	res, err := Reconstruct(services)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	wantCut := map[string]int{"A": 1, "B": 0, "C": 1}
	if !reflect.DeepEqual(res.Cut, wantCut) {
		t.Fatalf("cut = %v, want %v", res.Cut, wantCut)
	}
	validateCut(t, services, res)
}

func TestMissingSourceForcesCutBeforeReceive(t *testing.T) {
	// "ghost" has a receive but no send anywhere: it must be flagged and
	// A forced to just before that receive. The retreat then makes A's
	// send of "ok" unavailable, cascading B back before its receive too.
	services := []ServiceLog{
		{Name: "A", MaxSeq: 3, Events: []Event{
			ev(1, Local, ""),
			ev(2, Receive, "ghost"),
			ev(3, Send, "ok"),
		}},
		{Name: "B", MaxSeq: 2, Events: []Event{
			ev(1, Receive, "ok"),
			ev(2, Local, ""),
		}},
	}

	res, err := Reconstruct(services)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	wantCut := map[string]int{"A": 1, "B": 0}
	if !reflect.DeepEqual(res.Cut, wantCut) {
		t.Fatalf("cut = %v, want %v", res.Cut, wantCut)
	}
	wantMissing := []MissingReceive{{MessageID: "ghost", Service: "A", Seq: 2}}
	if !reflect.DeepEqual(res.MissingSource, wantMissing) {
		t.Fatalf("missing source = %v, want %v", res.MissingSource, wantMissing)
	}
	if len(res.InFlight) != 0 {
		t.Errorf("in-flight = %v, want none (send of ok got cut away)", res.InFlight)
	}
	validateCut(t, services, res)
}

func TestMissingSourceAtFirstSeqCutsToZero(t *testing.T) {
	// Boundary: an orphan receive at seq 1 forces the cut all the way to 0.
	services := []ServiceLog{
		{Name: "D", MaxSeq: 5, Events: []Event{
			ev(1, Receive, "lone"),
			ev(2, Local, ""),
		}},
	}

	res, err := Reconstruct(services)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if res.Cut["D"] != 0 {
		t.Fatalf("cut D = %d, want 0", res.Cut["D"])
	}
	wantMissing := []MissingReceive{{MessageID: "lone", Service: "D", Seq: 1}}
	if !reflect.DeepEqual(res.MissingSource, wantMissing) {
		t.Fatalf("missing source = %v, want %v", res.MissingSource, wantMissing)
	}
	validateCut(t, services, res)
}

func TestBoundaryReceiveAndSendAtCap(t *testing.T) {
	// Both the send (A seq 2 = cap) and the receive (B seq 1 = cap) sit
	// exactly on the boundary: the cut is valid and nothing is in flight.
	services := []ServiceLog{
		{Name: "A", MaxSeq: 2, Events: []Event{
			ev(1, Local, ""),
			ev(2, Send, "edge"),
		}},
		{Name: "B", MaxSeq: 1, Events: []Event{
			ev(1, Receive, "edge"),
		}},
	}

	res, err := Reconstruct(services)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	wantCut := map[string]int{"A": 2, "B": 1}
	if !reflect.DeepEqual(res.Cut, wantCut) {
		t.Fatalf("cut = %v, want %v", res.Cut, wantCut)
	}
	if len(res.InFlight) != 0 {
		t.Errorf("in-flight = %v, want none", res.InFlight)
	}
	validateCut(t, services, res)
}

func TestCapClampingAndEmptyPrefixes(t *testing.T) {
	// Cap beyond the last event clamps down; zero/empty services keep cut 0;
	// a retained send with no receive is reported in flight.
	services := []ServiceLog{
		{Name: "X", MaxSeq: 10, Events: []Event{
			ev(1, Send, "s1"),
			ev(2, Local, ""),
		}},
		{Name: "Y", MaxSeq: 3, Events: nil},
		{Name: "Z", MaxSeq: 0, Events: []Event{
			ev(1, Send, "s2"),
		}},
	}

	res, err := Reconstruct(services)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	wantCut := map[string]int{"X": 2, "Y": 0, "Z": 0}
	if !reflect.DeepEqual(res.Cut, wantCut) {
		t.Fatalf("cut = %v, want %v", res.Cut, wantCut)
	}
	if !reflect.DeepEqual(res.InFlight, []string{"s1"}) {
		t.Errorf("in-flight = %v, want [s1]", res.InFlight)
	}
	validateCut(t, services, res)
}

func TestInFlightWhenReceiveCutButSendRetained(t *testing.T) {
	// Interleaving boundary: the send survives but the receiver retreats,
	// so the message is in flight on the section even though both events
	// exist in the logs.
	services := []ServiceLog{
		{Name: "A", MaxSeq: 2, Events: []Event{
			ev(1, Send, "fly"),
			ev(2, Local, ""),
		}},
		{Name: "B", MaxSeq: 2, Events: []Event{
			ev(1, Local, ""),
			ev(2, Receive, "fly"),
			ev(3, Receive, "late"),
		}},
		{Name: "C", MaxSeq: 1, Events: []Event{
			ev(1, Local, ""),
			ev(2, Send, "late"), // outside C's cap: B must drop before seq 3
		}},
	}

	res, err := Reconstruct(services)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// B keeps seq 1..2 (receive of "fly"), then retreats before seq 3.
	wantCut := map[string]int{"A": 2, "B": 2, "C": 1}
	if !reflect.DeepEqual(res.Cut, wantCut) {
		t.Fatalf("cut = %v, want %v", res.Cut, wantCut)
	}
	// "late" never has its send retained; "fly" is fully delivered.
	if len(res.InFlight) != 0 {
		t.Errorf("in-flight = %v, want none", res.InFlight)
	}

	// Now lower B's cap below the receive of "fly": the retained send
	// becomes an in-flight message.
	services[1].MaxSeq = 1
	res, err = Reconstruct(services)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	wantCut = map[string]int{"A": 2, "B": 1, "C": 1}
	if !reflect.DeepEqual(res.Cut, wantCut) {
		t.Fatalf("cut = %v, want %v", res.Cut, wantCut)
	}
	if !reflect.DeepEqual(res.InFlight, []string{"fly"}) {
		t.Errorf("in-flight = %v, want [fly]", res.InFlight)
	}
	validateCut(t, services, res)
}

func TestMalformedInputsRejected(t *testing.T) {
	cases := []struct {
		name     string
		services []ServiceLog
	}{
		{
			name: "duplicate service name",
			services: []ServiceLog{
				{Name: "A", MaxSeq: 1, Events: []Event{ev(1, Local, "")}},
				{Name: "A", MaxSeq: 1, Events: []Event{ev(1, Local, "")}},
			},
		},
		{
			name: "non increasing sequence",
			services: []ServiceLog{
				{Name: "A", MaxSeq: 2, Events: []Event{ev(2, Local, ""), ev(2, Local, "")}},
			},
		},
		{
			name: "duplicate sends",
			services: []ServiceLog{
				{Name: "A", MaxSeq: 2, Events: []Event{ev(1, Send, "dup")}},
				{Name: "B", MaxSeq: 2, Events: []Event{ev(1, Send, "dup")}},
			},
		},
		{
			name: "send with empty id",
			services: []ServiceLog{
				{Name: "A", MaxSeq: 1, Events: []Event{ev(1, Send, "")}},
			},
		},
		{
			name: "empty service name",
			services: []ServiceLog{
				{Name: "", MaxSeq: 1, Events: []Event{ev(1, Local, "")}},
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Reconstruct(tc.services); err == nil {
				t.Fatal("expected error for malformed input, got nil")
			}
		})
	}
}

func TestEmptyInput(t *testing.T) {
	res, err := Reconstruct(nil)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(res.Cut) != 0 || len(res.InFlight) != 0 || len(res.MissingSource) != 0 {
		t.Fatalf("empty input result = %+v, want all empty", res)
	}
}
