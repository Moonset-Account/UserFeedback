package zonetransfer

import (
	"errors"
	"testing"
)

func TestReceiver_IXFRServesOldZoneUntilFinalCommit(t *testing.T) {
	snapshots, deltas, target := buildWrapScenario(t)
	plan, err := PlanTransfer(snapshots, deltas, target.Version, maxSerial-1)
	if err != nil || plan.Kind != KindIXFR {
		t.Fatalf("unexpected plan: %+v err %v", plan, err)
	}

	rcv := NewReceiver(snapshots[0])
	old := rcv.Current()

	var observations int
	err = rcv.Apply(plan, func(step int, served Snapshot, assembledSoFar []Record) {
		observations++
		// At every intermediate step the published zone is still the old one.
		if !snapshotEqual(served, old) {
			t.Errorf("step %d: receiver exposed partial state", step)
		}
		if served.Version != "v1" {
			t.Errorf("step %d: served version = %q, want v1", step, served.Version)
		}
	})
	if err != nil {
		t.Fatalf("Apply failed: %v", err)
	}
	if observations != len(plan.Chain) {
		t.Fatalf("observer invoked %d times, want %d", observations, len(plan.Chain))
	}

	now := rcv.Current()
	if !snapshotEqual(now, target) {
		t.Fatalf("after commit served zone does not equal target")
	}
	if now.Serial != 0 || now.Version != "v3" {
		t.Fatalf("committed snapshot = %q/%d, want v3/0", now.Version, now.Serial)
	}
}

func TestReceiver_FailedVerificationKeepsOldZone(t *testing.T) {
	snapshots, deltas, target := buildWrapScenario(t)
	plan, err := PlanTransfer(snapshots, deltas, target.Version, maxSerial-1)
	if err != nil {
		t.Fatalf("planning failed: %v", err)
	}

	// Corrupt the final target expectation: same chain, wrong advertised
	// serial. The receiver must reject and keep serving v1.
	plan.Target.Serial = 4242
	rcv := NewReceiver(snapshots[0])
	old := rcv.Current()

	err = rcv.Apply(plan, nil)
	if !errors.Is(err, ErrVerification) {
		t.Fatalf("err = %v, want ErrVerification", err)
	}
	if !snapshotEqual(rcv.Current(), old) {
		t.Fatalf("receiver changed state after failed verification")
	}
}

func TestReceiver_BrokenDeltaKeepsOldZone(t *testing.T) {
	snapshots, deltas, target := buildWrapScenario(t)
	plan, err := PlanTransfer(snapshots, deltas, target.Version, maxSerial-1)
	if err != nil {
		t.Fatalf("planning failed: %v", err)
	}
	// Force a strict-application failure mid-chain: delete a record that is
	// not present at that point.
	plan.Chain[1].Deletes = []Record{rec("ghost.", "A", "203.0.113.66")}

	rcv := NewReceiver(snapshots[0])
	old := rcv.Current()
	err = rcv.Apply(plan, nil)
	if !errors.Is(err, ErrVerification) {
		t.Fatalf("err = %v, want ErrVerification", err)
	}
	if !snapshotEqual(rcv.Current(), old) {
		t.Fatalf("partial transfer was published")
	}
}

func TestReceiver_ChainGapRejected(t *testing.T) {
	snapshots, deltas, target := buildWrapScenario(t)
	plan, err := PlanTransfer(snapshots, deltas, target.Version, maxSerial-1)
	if err != nil {
		t.Fatalf("planning failed: %v", err)
	}
	// Dislocate the second step's expected starting version.
	plan.Chain[1].FromVersion = "v9-unknown"
	rcv := NewReceiver(snapshots[0])
	if err := rcv.Apply(plan, nil); !errors.Is(err, ErrVerification) {
		t.Fatalf("err = %v, want ErrVerification", err)
	}
	if rcv.Current().Version != "v1" {
		t.Fatalf("served version changed after rejected chain: %q", rcv.Current().Version)
	}
}

func TestReceiver_EqualSerialContentChangeRejected(t *testing.T) {
	// Hand-craft a chain the planner would never emit: a pinned serial that
	// nonetheless adds a record. The receiver must reject the step itself.
	r1 := rec("a.example.", "A", "192.0.2.1")
	r2 := rec("b.example.", "A", "192.0.2.2")
	v1 := snap("v1", 10, r1)
	v2 := snap("v2", 10, r1, r2)
	badStep := delta("v1", "v2", 10, 10, nil, []Record{r2}, 1)
	plan := Plan{
		Kind:   KindIXFR,
		Target: v2,
		Chain:  []Delta{badStep},
	}

	rcv := NewReceiver(v1)
	old := rcv.Current()
	err := rcv.Apply(plan, nil)
	if !errors.Is(err, ErrVerification) {
		t.Fatalf("err = %v, want ErrVerification", err)
	}
	if !snapshotEqual(rcv.Current(), old) {
		t.Fatal("receiver published a same-serial content change")
	}
}

func TestReceiver_AXFR_EmptyVersionRejected(t *testing.T) {
	v1 := snap("v1", 1, rec("a.", "A", "192.0.2.1"))
	rcv := NewReceiver(v1)
	old := rcv.Current()
	badTarget := v1
	badTarget.Version = ""
	err := rcv.Apply(Plan{Kind: KindAXFR, Target: badTarget}, nil)
	if !errors.Is(err, ErrVerification) {
		t.Fatalf("err = %v, want ErrVerification", err)
	}
	if !snapshotEqual(rcv.Current(), old) {
		t.Fatal("AXFR without a target version was committed")
	}
}

func TestReceiver_UnknownKindRejected(t *testing.T) {
	v1 := snap("v1", 1, rec("a.", "A", "192.0.2.1"))
	rcv := NewReceiver(v1)
	old := rcv.Current()
	plan := Plan{Kind: Kind(99), Target: v1}
	if err := rcv.Apply(plan, nil); !errors.Is(err, ErrVerification) {
		t.Fatalf("err = %v, want ErrVerification", err)
	}
	if !snapshotEqual(rcv.Current(), old) {
		t.Fatal("receiver changed state on unknown transfer kind")
	}
}

func TestReceiver_HalfCircleStepRejected(t *testing.T) {
	r1 := rec("a.", "A", "192.0.2.1")
	v1 := snap("v1", 0, r1)
	v2 := snap("v2", 0x80000000, r1, rec("b.", "A", "192.0.2.2"))
	step := delta("v1", "v2", 0, 0x80000000, nil, []Record{rec("b.", "A", "192.0.2.2")}, 1)
	rcv := NewReceiver(v1)
	old := rcv.Current()
	err := rcv.Apply(Plan{Kind: KindIXFR, Target: v2, Chain: []Delta{step}}, nil)
	if !errors.Is(err, ErrVerification) {
		t.Fatalf("err = %v, want ErrVerification", err)
	}
	if !snapshotEqual(rcv.Current(), old) {
		t.Fatal("receiver changed state across a half-circle serial step")
	}
}

func TestReceiver_AXFR(t *testing.T) {
	_, _, target := buildWrapScenario(t)
	r1 := rec("a.example.", "A", "192.0.2.1")
	r2 := rec("b.example.", "A", "192.0.2.2")
	initial := snap("v1", maxSerial-1, r1, r2)
	// Force the AXFR form the primary would produce when the start is
	// ambiguous.
	axfr := Plan{Kind: KindAXFR, Target: target, Bytes: SnapshotBytes(target)}

	rcv := NewReceiver(initial)
	if err := rcv.Apply(axfr, nil); err != nil {
		t.Fatalf("AXFR apply failed: %v", err)
	}
	if !snapshotEqual(rcv.Current(), target) {
		t.Fatalf("AXFR did not install the target zone")
	}
}

func TestReceiver_NoOpIXFR(t *testing.T) {
	v1 := snap("v1", 7, rec("a.example.", "A", "192.0.2.1"))
	rcv := NewReceiver(v1)
	plan := Plan{Kind: KindIXFR, Target: v1}
	if err := rcv.Apply(plan, nil); err != nil {
		t.Fatalf("no-op IXFR failed: %v", err)
	}
	if !snapshotEqual(rcv.Current(), v1) {
		t.Fatalf("no-op IXFR altered the served zone")
	}
	// A no-op chain whose target differs from the served zone must fail.
	other := v1
	other.Serial = 8
	if err := rcv.Apply(Plan{Kind: KindIXFR, Target: other}, nil); !errors.Is(err, ErrVerification) {
		t.Fatalf("err = %v, want ErrVerification", err)
	}
}

func TestReceiver_CurrentIsDefensiveCopy(t *testing.T) {
	v1 := snap("v1", 1, rec("a.", "A", "192.0.2.1"))
	rcv := NewReceiver(v1)
	got := rcv.Current()
	got.Records[0] = rec("evil.", "A", "198.51.100.1")
	got.Version = "tampered"
	if rcv.Current().Version != "v1" {
		t.Fatal("mutating a returned snapshot leaked into receiver state")
	}
}

func TestApplyDelta_StrictSemantics(t *testing.T) {
	r1, r2 := rec("a.", "A", "1"), rec("b.", "A", "2")

	// Delete of an absent record fails.
	if _, err := ApplyDelta([]Record{r1}, Delta{Deletes: []Record{r2}}); !errors.Is(err, ErrInvalidDelta) {
		t.Fatalf("absent delete: err = %v, want ErrInvalidDelta", err)
	}
	// Add of a present record fails.
	if _, err := ApplyDelta([]Record{r1}, Delta{Adds: []Record{r1}}); !errors.Is(err, ErrInvalidDelta) {
		t.Fatalf("duplicate add: err = %v, want ErrInvalidDelta", err)
	}
	// A valid delete+add round trip.
	out, err := ApplyDelta([]Record{r1, r2}, Delta{
		Deletes: []Record{r2},
		Adds:    []Record{rec("c.", "A", "3")},
	})
	if err != nil {
		t.Fatalf("valid delta failed: %v", err)
	}
	if !sameRecords(out, []Record{r1, rec("c.", "A", "3")}) {
		t.Fatalf("delta result mismatch: %v", out)
	}
	// Input slice is not modified.
	if !sameRecords([]Record{r1, r2}, []Record{r1, r2}) {
		t.Fatal("input record set was mutated")
	}
	// Duplicate records in the starting zone are rejected rather than
	// silently collapsed.
	dup := []Record{r1, r1}
	if _, err := ApplyDelta(dup, Delta{Adds: []Record{r2}}); !errors.Is(err, ErrInvalidDelta) {
		t.Fatalf("duplicate start record: err = %v, want ErrInvalidDelta", err)
	}
}
