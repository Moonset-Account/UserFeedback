package zonetransfer

import (
	"errors"
	"fmt"
	"testing"
)

const maxSerial = uint32(0xFFFFFFFF)

// buildWrapScenario builds a v1 -> v2 -> v3 history whose serials walk
// 0xFFFFFFFE -> 0xFFFFFFFF -> 0, exercising wrap-around through zero.
func buildWrapScenario(t *testing.T) ([]Snapshot, []Delta, Snapshot) {
	t.Helper()
	r1 := rec("a.example.", "A", "192.0.2.1")
	r2 := rec("b.example.", "A", "192.0.2.2")
	r3 := rec("c.example.", "A", "192.0.2.3")
	r4 := rec("d.example.", "A", "192.0.2.4")
	r5 := rec("e.example.", "A", "192.0.2.5")

	v1 := snap("v1", maxSerial-1, r1, r2)
	v2 := snap("v2", maxSerial, r1, r2, r3)
	v3 := snap("v3", 0, r1, r2, r3, r4, r5)

	// Cheap per-delta wire sizes; the full zone carries 5 records (6 bytes
	// under the package's size model).
	d12 := delta("v1", "v2", maxSerial-1, maxSerial, nil, []Record{r3}, 2)
	d23 := delta("v2", "v3", maxSerial, 0, nil, []Record{r4, r5}, 3)
	return []Snapshot{v1, v2, v3}, []Delta{d12, d23}, v3
}

func TestPlan_IXFRAcrossSerialWrap(t *testing.T) {
	snapshots, deltas, target := buildWrapScenario(t)

	plan, err := PlanTransfer(snapshots, deltas, target.Version, maxSerial-1)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindIXFR {
		t.Fatalf("kind = %s (reason %q), want IXFR", plan.Kind, plan.Reason)
	}
	if len(plan.Chain) != 2 {
		t.Fatalf("chain length = %d, want 2", len(plan.Chain))
	}
	if plan.Bytes != 5 {
		t.Fatalf("chain bytes = %d, want 5", plan.Bytes)
	}
	if plan.Chain[0].ToSerial != maxSerial || plan.Chain[1].ToSerial != 0 {
		t.Fatalf("wrap sequence wrong: %d -> %d", plan.Chain[0].ToSerial, plan.Chain[1].ToSerial)
	}
	// The chain must genuinely replay to the target content.
	got := mustReplay(t, snapshots[0], plan.Chain)
	if !sameRecords(got, target.Records) {
		t.Fatalf("replayed records do not match target")
	}
}

func TestPlan_ShorterEdgeWithMismatchedStartContent(t *testing.T) {
	snapshots, deltas, target := buildWrapScenario(t)
	r3 := rec("c.example.", "A", "192.0.2.3")
	r4 := rec("d.example.", "A", "192.0.2.4")
	r5 := rec("e.example.", "A", "192.0.2.5")

	// A shorter (1 byte) edge that *claims* to start at v1's identity but
	// carries v2's content semantics: it deletes r3 (absent in v1), so it
	// cannot replay against the backup's actual v1 zone.
	fraud := delta("v1", "v3", maxSerial-1, 0,
		[]Record{r3}, []Record{r3, r4, r5}, 1)
	deltas = append(deltas, fraud)

	plan, err := PlanTransfer(snapshots, deltas, target.Version, maxSerial-1)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindIXFR {
		t.Fatalf("kind = %s (reason %q), want the honest IXFR chain", plan.Kind, plan.Reason)
	}
	if plan.Bytes != 5 {
		t.Fatalf("bytes = %d: the 1-byte mismatched edge must not be selected", plan.Bytes)
	}
	for _, d := range plan.Chain {
		if d.Bytes == 1 {
			t.Fatal("edge whose start content does not match was selected")
		}
	}
}

func TestPlan_NonUniqueStartFallsBackToAXFR(t *testing.T) {
	base, deltas, target := buildWrapScenario(t)
	// A different snapshot reusing v1's serial with different records.
	other := snap("v1-prime", maxSerial-1, rec("x.example.", "A", "198.51.100.9"))
	snapshots := append(base, other)

	plan, err := PlanTransfer(snapshots, deltas, target.Version, maxSerial-1)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindAXFR || plan.Reason != ReasonNonUniqueStart {
		t.Fatalf("got %s/%q, want AXFR/%s", plan.Kind, plan.Reason, ReasonNonUniqueStart)
	}
	if plan.Bytes != SnapshotBytes(target) {
		t.Fatalf("AXFR bytes = %d, want full snapshot size %d", plan.Bytes, SnapshotBytes(target))
	}
}

func TestPlan_NonUniqueStartEvenWithIdenticalContent(t *testing.T) {
	// Two distinct version IDs sharing serial AND records: a secondary that
	// only reports the serial still cannot name its start, so the primary
	// must not guess and sends AXFR.
	base, deltas, target := buildWrapScenario(t)
	twin := snap("v1-twin", maxSerial-1, base[0].Records...)
	snapshots := append(base, twin)

	plan, err := PlanTransfer(snapshots, deltas, target.Version, maxSerial-1)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindAXFR || plan.Reason != ReasonNonUniqueStart {
		t.Fatalf("got %s/%q, want AXFR/%s", plan.Kind, plan.Reason, ReasonNonUniqueStart)
	}
}

func TestPlan_EqualSerialDifferentContentNotChainable(t *testing.T) {
	// The backup starts at v0, whose serial 9 is unique and unambiguous.
	// The only route to v4 passes through an equal-serial v2 -> v3 edge that
	// changes content: that edge is invalid, so no chain reaches v4 and the
	// primary must fall back to AXFR with ReasonNoChain.
	r1 := rec("a.example.", "A", "192.0.2.1")
	r2 := rec("b.example.", "A", "192.0.2.2")
	r3 := rec("c.example.", "A", "192.0.2.3")
	v0 := snap("v0", 9, r1)
	v1 := snap("v1", 10, r1)
	v2 := snap("v2", 20, r1)
	v3 := snap("v3", 20, r1, r2) // serial 20 reused with changed content
	v4 := snap("v4", 21, r1, r2, r3)
	step01 := delta("v0", "v1", 9, 10, nil, nil, 1)
	step12 := delta("v1", "v2", 10, 20, nil, nil, 1)
	bad := delta("v2", "v3", 20, 20, nil, []Record{r2}, 1)
	step34 := delta("v3", "v4", 20, 21, nil, []Record{r3}, 1)

	plan, err := PlanTransfer([]Snapshot{v0, v1, v2, v3, v4},
		[]Delta{step01, step12, bad, step34}, "v4", 9)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindAXFR || plan.Reason != ReasonNoChain {
		t.Fatalf("got %s/%q, want AXFR/%s", plan.Kind, plan.Reason, ReasonNoChain)
	}
}

func TestPlan_EqualSerialIdenticalContentIsChainable(t *testing.T) {
	// Equal serial with byte-identical content is a no-op marker edge and
	// remains usable as a chain step. The backup starts at unique v0.
	r1 := rec("a.example.", "A", "192.0.2.1")
	v0 := snap("v0", 9, r1)
	v1 := snap("v1", 10, r1)
	v2 := snap("v2", 20, r1)
	v3 := snap("v3", 20, r1) // serial 20 reused, content byte-identical
	v4 := snap("v4", 21, r1, rec("b.", "A", "192.0.2.2"))
	// Zero-cost marker edges are plausible on the wire (an empty change set
	// carries only framing already accounted elsewhere); the final step is
	// the only payload, so the chain beats the 3-byte full zone.
	step01 := delta("v0", "v1", 9, 10, nil, nil, 0)
	step12 := delta("v1", "v2", 10, 20, nil, nil, 0)
	marker := delta("v2", "v3", 20, 20, nil, nil, 0)
	step := delta("v3", "v4", 20, 21, nil, []Record{rec("b.", "A", "192.0.2.2")}, 1)

	plan, err := PlanTransfer([]Snapshot{v0, v1, v2, v3, v4},
		[]Delta{step01, step12, marker, step}, "v4", 9)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindIXFR || len(plan.Chain) != 4 || plan.Bytes != 1 {
		t.Fatalf("equal-serial identical-content chain rejected: %+v", plan)
	}
}

func TestPlan_HalfCircleSerialIsIncomparable(t *testing.T) {
	r1 := rec("a.example.", "A", "192.0.2.1")
	r2 := rec("b.example.", "A", "192.0.2.2")
	v1 := snap("v1", 0, r1)
	v2 := snap("v2", 0x80000000, r1, r2)
	edge := delta("v1", "v2", 0, 0x80000000, nil, []Record{r2}, 1)

	plan, err := PlanTransfer([]Snapshot{v1, v2}, []Delta{edge}, "v2", 0)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindAXFR || plan.Reason != ReasonNoChain {
		t.Fatalf("got %s/%q, want AXFR/%s for half-circle edge", plan.Kind, plan.Reason, ReasonNoChain)
	}
}

func TestPlan_FullZoneNoMoreExpensiveWins(t *testing.T) {
	r1 := rec("a.example.", "A", "192.0.2.1")
	v1 := snap("v1", 1, r1)
	// Two-record target: full zone model cost is 3 bytes.
	v2 := snap("v2", 2, r1, rec("b.example.", "A", "192.0.2.2"))
	expensive := delta("v1", "v2", 1, 2, nil,
		[]Record{rec("b.example.", "A", "192.0.2.2")}, 3) // equal to AXFR cost

	plan, err := PlanTransfer([]Snapshot{v1, v2}, []Delta{expensive}, "v2", 1)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindAXFR || plan.Reason != ReasonFullZoneCheaper {
		t.Fatalf("got %s/%q, want AXFR/%s on equal cost", plan.Kind, plan.Reason, ReasonFullZoneCheaper)
	}
}

func TestPlan_TamperedDeltaReplaysWrong(t *testing.T) {
	r1 := rec("a.example.", "A", "192.0.2.1")
	r2 := rec("b.example.", "A", "192.0.2.2")
	r3 := rec("c.example.", "A", "192.0.2.3")
	v1 := snap("v1", 1, r1)
	v2 := snap("v2", 2, r1, r2)
	// Declares v2 as target but adds the wrong record.
	lying := delta("v1", "v2", 1, 2, nil, []Record{r3}, 1)

	plan, err := PlanTransfer([]Snapshot{v1, v2}, []Delta{lying}, "v2", 1)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindAXFR || plan.Reason != ReasonNoChain {
		t.Fatalf("got %s/%q, want AXFR/%s", plan.Kind, plan.Reason, ReasonNoChain)
	}
}

func TestPlan_NoMatchingSnapshot(t *testing.T) {
	v1 := snap("v1", 1, rec("a.example.", "A", "192.0.2.1"))
	plan, err := PlanTransfer([]Snapshot{v1}, nil, "v1", 99)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindAXFR || plan.Reason != ReasonNoSnapshot {
		t.Fatalf("got %s/%q, want AXFR/%s", plan.Kind, plan.Reason, ReasonNoSnapshot)
	}
}

func TestPlan_MinimumByteChainAcrossMultipleHops(t *testing.T) {
	// History v0 -> v1 -> v2 -> v3 with three replayable routes from v0:
	//   direct v0->v3:                 10 bytes
	//   v0->v1->v2->v3:                2 + 2 + 4 = 8 bytes
	//   v0->v2 (shortcut) ->v3:        3 + 4 = 7 bytes (must win)
	mk := func(name, data string) Record { return rec(name, "A", data) }
	m1, m2 := mk("m1.", "192.0.2.11"), mk("m2.", "192.0.2.12")
	// Give the target many records so the full-zone wire cost (1 per record
	// plus framing) clearly exceeds every candidate delta chain.
	targetMarkers := make([]Record, 0, 20)
	for i := 0; i < 20; i++ {
		targetMarkers = append(targetMarkers,
			mk(fmt.Sprintf("t%d.", i), fmt.Sprintf("192.0.2.%d", 100+i)))
	}
	base := []Record{mk("a.", "192.0.2.1"), mk("b.", "192.0.2.2")}

	v0 := snap("v0", 100, base...)
	v1 := snap("v1", 101, append(CloneRecords(base), m1)...)
	v2 := snap("v2", 102, append(CloneRecords(base), m2)...)
	v3 := snap("v3", 103, append(CloneRecords(base), targetMarkers...)...)

	d01 := delta("v0", "v1", 100, 101, nil, []Record{m1}, 2)
	d12 := delta("v1", "v2", 101, 102, []Record{m1}, []Record{m2}, 2)
	d02 := delta("v0", "v2", 100, 102, nil, []Record{m2}, 3)
	d23 := delta("v2", "v3", 102, 103, []Record{m2}, targetMarkers, 4)
	d03 := delta("v0", "v3", 100, 103, nil, targetMarkers, 10)

	snapshots := []Snapshot{v0, v1, v2, v3}
	deltas := []Delta{d01, d12, d02, d23, d03}

	plan, err := PlanTransfer(snapshots, deltas, "v3", 100)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindIXFR {
		t.Fatalf("kind = %s (reason %q), want IXFR", plan.Kind, plan.Reason)
	}
	if plan.Bytes != 7 {
		t.Fatalf("chain bytes = %d, want minimum 7 (v0->v2->v3)", plan.Bytes)
	}
	wantPath := []string{"v2", "v3"}
	if len(plan.Chain) != len(wantPath) {
		t.Fatalf("chain length = %d, want %d", len(plan.Chain), len(wantPath))
	}
	for i, d := range plan.Chain {
		if d.ToVersion != wantPath[i] {
			t.Fatalf("step %d lands at %s, want %s", i, d.ToVersion, wantPath[i])
		}
	}
}

func TestPlan_AlreadyUpToDate(t *testing.T) {
	v1 := snap("v1", 7, rec("a.example.", "A", "192.0.2.1"))
	plan, err := PlanTransfer([]Snapshot{v1}, nil, "v1", 7)
	if err != nil {
		t.Fatalf("PlanTransfer returned error: %v", err)
	}
	if plan.Kind != KindIXFR || len(plan.Chain) != 0 || plan.Bytes != 0 {
		t.Fatalf("up-to-date backup should get an empty IXFR, got %+v", plan)
	}
}

func TestPlan_InvalidInputs(t *testing.T) {
	v1 := snap("v1", 1)
	if _, err := PlanTransfer([]Snapshot{v1}, nil, "missing", 1); !errors.Is(err, ErrBadTarget) {
		t.Fatalf("missing target: err = %v, want ErrBadTarget", err)
	}
	if _, err := PlanTransfer([]Snapshot{v1, snap("v1", 2)}, nil, "v1", 1); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("duplicate version: err = %v, want ErrInvalidInput", err)
	}
	if _, err := PlanTransfer([]Snapshot{snap("", 1)}, nil, "", 1); !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("empty version: err = %v, want ErrInvalidInput", err)
	}
}

func TestValidateEdge_RejectsBackwardsSerial(t *testing.T) {
	r1 := rec("a.example.", "A", "192.0.2.1")
	v1 := snap("v1", 10, r1)
	v2 := snap("v2", 5, r1, rec("b.", "A", "192.0.2.2"))
	backward := delta("v1", "v2", 10, 5, nil, []Record{rec("b.", "A", "192.0.2.2")}, 1)
	if _, err := validateEdge(backward, map[string]Snapshot{"v1": v1, "v2": v2}); err == nil {
		t.Fatal("backwards serial edge must be rejected")
	}
}
