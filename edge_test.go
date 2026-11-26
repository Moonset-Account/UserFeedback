package slotcheck

import (
	"encoding/json"
	"reflect"
	"testing"
)

func TestAuxiliaryCoordinatesAreNotTreatedAsObservedEmpty(t *testing.T) {
	origin := SlotRef{Generation: 1, Slot: 0, Direction: "constant"}
	reissue := SlotRef{Generation: 1, Slot: 1, Direction: "constant"}
	in := Input{
		Generations: []Epoch{{Generation: 1}},
		Template:    SlotTemplate{FirstSlot: 0, SlotCount: 2},
		Policies:    []DirectionPolicy{{Direction: "constant", AllowedLengths: []int{256}}},
		DelayedChunks: []DelayedChunk{{
			ChunkID: "c", Bytes: 10, Origin: origin, Reissue: &reissue,
		}},
	}

	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	for _, ref := range []SlotRef{origin, reissue} {
		s := getSlot(t, rep, ref.Generation, ref.Slot, ref.Direction)
		if len(s.LengthFindings) != 0 {
			t.Fatalf("auxiliary coordinate %s must not receive a fabricated zero-length verdict: %+v", ref, s.LengthFindings)
		}
		if s.Status != StatusInsufficientEvidence {
			t.Fatalf("coordinate %s should be insufficient, got %s: %+v", ref, s.Status, s)
		}
		if !hasFinding(s.EvidenceGaps, CodeMissingSlotRecord) {
			t.Fatalf("coordinate %s should locate missing slot record: %+v", ref, s.EvidenceGaps)
		}
	}
}

func TestCrossGenerationDelayAcrossResync(t *testing.T) {
	origin := SlotRef{Generation: 1, Slot: 2, Direction: "out"}
	reissue := SlotRef{Generation: 2, Slot: 0, Direction: "out"}
	in := Input{
		Generations: []Epoch{{Generation: 1}, {Generation: 2, Resync: true}},
		Template:    SlotTemplate{SlotIndexes: []int{0, 2}},
		Policies:    []DirectionPolicy{{Direction: "out", AllowedLengths: []int{0, 128}}},
		Records: []SlotRecord{
			{SlotRef: origin, ObservedVisibleBytes: Ptr(0), RealBytes: 0},
			{
				SlotRef: reissue, ObservedVisibleBytes: Ptr(128),
				RealBytes: 100, PaddingBytes: 28, ReissuedChunkIDs: []string{"cross-gen"},
				PaddingAllocations: []PaddingAllocation{{ID: "post-resync-pad"}},
			},
		},
		DelayedChunks: []DelayedChunk{{
			ChunkID: "cross-gen", Bytes: 100, Origin: origin, Reissue: &reissue,
		}},
		Receipts: []Receipt{{
			ID: "cross-gen-receipt", ChunkID: "cross-gen", Bytes: 100,
			Origin: origin, Reissue: &reissue,
		}},
	}

	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	for _, ref := range []SlotRef{origin, reissue} {
		s := getSlot(t, rep, ref.Generation, ref.Slot, ref.Direction)
		if s.Status != StatusProvenCompliant {
			t.Fatalf("cross-generation receipted delay should prove %s: %+v", ref, s)
		}
		for _, f := range s.LengthFindings {
			if f.Code == CodePaddingResyncDuplicate || f.Code == CodePaddingDuplicate {
				t.Fatalf("distinct padding was falsely duplicated at %s: %+v", ref, f)
			}
		}
	}
}

func TestArbitraryInputPermutationsGiveSameReport(t *testing.T) {
	origin := SlotRef{Generation: 1, Slot: 0, Direction: "out"}
	reissue := SlotRef{Generation: 1, Slot: 2, Direction: "out"}
	base := baseInput()
	base.Template.SlotCount = 3
	base.Records = []SlotRecord{
		{
			SlotRef: reissue, ObservedVisibleBytes: Ptr(128), RealBytes: 100, PaddingBytes: 28,
			ReissuedChunkIDs: []string{"c1"},
		},
		{SlotRef: origin, ObservedVisibleBytes: Ptr(0)},
	}
	base.DelayedChunks = []DelayedChunk{{ChunkID: "c1", Bytes: 100, Origin: origin, Reissue: &reissue}}
	base.Receipts = []Receipt{{ID: "r1", ChunkID: "c1", Bytes: 100, Origin: origin, Reissue: &reissue}}

	reversedRecords := base
	reversedRecords.Records = []SlotRecord{base.Records[1], base.Records[0]}

	reorderedEvidence := base
	reorderedEvidence.DelayedChunks = []DelayedChunk{base.DelayedChunks[0]}
	reorderedEvidence.Receipts = []Receipt{base.Receipts[0]}

	inputs := []Input{base, reversedRecords, reorderedEvidence}
	var expected []byte
	for i, in := range inputs {
		rep, err := Check(in)
		if err != nil {
			t.Fatalf("permutation %d: %v", i, err)
		}
		got, err := json.Marshal(rep)
		if err != nil {
			t.Fatal(err)
		}
		if expected == nil {
			expected = got
			continue
		}
		if !reflect.DeepEqual(expected, got) {
			t.Fatalf("permutation %d changed report:\nexpected %s\ngot %s", i, expected, got)
		}
	}
}

func TestObservedLengthOptionalAndStrictObservationPolicy(t *testing.T) {
	in := baseInput()
	in.Records = []SlotRecord{{
		SlotRef:   SlotRef{Generation: 1, Slot: 0, Direction: "out"},
		RealBytes: 100, PaddingBytes: 28,
	}}
	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	s := getSlot(t, rep, 1, 0, "out")
	if s.Status != StatusProvenCompliant || s.EffectiveVisibleBytes != 128 {
		t.Fatalf("accounted length should be used without independent observation: %+v", s)
	}

	in.Policies[0].RequireExternalObservation = true
	rep, err = Check(in)
	if err != nil {
		t.Fatal(err)
	}
	s = getSlot(t, rep, 1, 0, "out")
	if s.Status != StatusInsufficientEvidence || !hasFinding(s.EvidenceGaps, CodeMissingExternalObservation) {
		t.Fatalf("strict observation policy should require observed length: %+v", s)
	}
}

func TestUnknownClockGenerationIsEvidenceGap(t *testing.T) {
	in := baseInput()
	in.Records = []SlotRecord{{
		SlotRef:   SlotRef{Generation: 99, Slot: 0, Direction: "out"},
		RealBytes: 128,
	}}
	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	s := getSlot(t, rep, 99, 0, "out")
	if s.Status != StatusInsufficientEvidence || !hasFinding(s.EvidenceGaps, CodeUnknownGeneration) {
		t.Fatalf("unknown generation should be an evidence gap: %+v", s)
	}
}

func TestNamedTiersAndExactBoundaries(t *testing.T) {
	in := Input{
		Generations: []Epoch{{Generation: 1}},
		Template:    SlotTemplate{FirstSlot: 0, SlotCount: 3},
		Policies: []DirectionPolicy{{
			Direction:       "d",
			AllowedTiers:    []LengthTier{{Name: "small", Length: 128}, {Name: "large", Length: 256}},
			MaxVisibleBytes: Ptr(256),
		}},
		Records: []SlotRecord{
			{SlotRef: SlotRef{Generation: 1, Slot: 0, Direction: "d"}, ObservedVisibleBytes: Ptr(128), RealBytes: 128},
			{SlotRef: SlotRef{Generation: 1, Slot: 1, Direction: "d"}, ObservedVisibleBytes: Ptr(256), RealBytes: 256},
			{SlotRef: SlotRef{Generation: 1, Slot: 2, Direction: "d"}, ObservedVisibleBytes: Ptr(257), RealBytes: 257},
		},
	}
	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	s0 := getSlot(t, rep, 1, 0, "d")
	if s0.Status != StatusProvenCompliant || s0.SelectedTier == nil || s0.SelectedTier.Name != "small" {
		t.Fatalf("exact named 128 tier should pass: %+v", s0)
	}
	s1 := getSlot(t, rep, 1, 1, "d")
	if s1.Status != StatusProvenCompliant || s1.SelectedTier == nil || s1.SelectedTier.Name != "large" {
		t.Fatalf("exact named 256 tier should pass: %+v", s1)
	}
	s2 := getSlot(t, rep, 1, 2, "d")
	if s2.Status != StatusExceededVisibilityTier || !hasFinding(s2.LengthFindings, CodeAboveMaximumVisible) {
		t.Fatalf("257 should exceed maximum/tier: %+v", s2)
	}
}

func TestResyncDuplicateAndReceiptedDelayInterleave(t *testing.T) {
	duplicateOld := SlotRef{Generation: 1, Slot: 1, Direction: "out"}
	resyncFirst := SlotRef{Generation: 2, Slot: 0, Direction: "out"}
	reissue := SlotRef{Generation: 2, Slot: 1, Direction: "out"}
	in := Input{
		Generations: []Epoch{{Generation: 1}, {Generation: 2, Resync: true}},
		Template:    SlotTemplate{SlotIndexes: []int{0, 1}},
		Policies:    []DirectionPolicy{{Direction: "out", AllowedLengths: []int{0, 128}}},
		Records: []SlotRecord{
			{
				SlotRef: duplicateOld, ObservedVisibleBytes: Ptr(128), RealBytes: 100, PaddingBytes: 28,
				PaddingAllocations: []PaddingAllocation{{ID: "shared-resync-pad"}},
			},
			{
				SlotRef: resyncFirst, ObservedVisibleBytes: Ptr(128), RealBytes: 100, PaddingBytes: 28,
				PaddingAllocations: []PaddingAllocation{{ID: "shared-resync-pad"}},
			},
			{
				SlotRef: reissue, ObservedVisibleBytes: Ptr(128), RealBytes: 100, PaddingBytes: 28,
				ReissuedChunkIDs:   []string{"interleaved"},
				PaddingAllocations: []PaddingAllocation{{ID: "reissue-pad"}},
			},
		},
		DelayedChunks: []DelayedChunk{{
			ChunkID: "interleaved", Bytes: 100, Origin: duplicateOld, Reissue: &reissue,
		}},
		Receipts: []Receipt{{
			ID: "interleaved-receipt", ChunkID: "interleaved", Bytes: 100,
			Origin: duplicateOld, Reissue: &reissue,
		}},
	}

	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	old := getSlot(t, rep, 1, 1, "out")
	first := getSlot(t, rep, 2, 0, "out")
	later := getSlot(t, rep, 2, 1, "out")

	if !hasFinding(old.LengthFindings, CodePaddingResyncDuplicate) ||
		!hasFinding(first.LengthFindings, CodePaddingResyncDuplicate) {
		t.Fatalf("expected resync duplicate to remain visible in interleaved ledger: old=%+v first=%+v", old.LengthFindings, first.LengthFindings)
	}
	if len(old.ConfirmationGaps) != 0 || len(later.ConfirmationGaps) != 0 {
		t.Fatalf("valid receipt should still correlate despite padding failure: old=%+v later=%+v", old.ConfirmationGaps, later.ConfirmationGaps)
	}
	if later.Status != StatusProvenCompliant {
		t.Fatalf("unrelated reissue slot should remain compliant: %+v", later)
	}
}

func TestPaddingBudgetResetsPerGeneration(t *testing.T) {
	in := Input{
		Generations: []Epoch{{Generation: 1}, {Generation: 2, Resync: true}},
		Template:    SlotTemplate{FirstSlot: 0, SlotCount: 1},
		Policies: []DirectionPolicy{{
			Direction: "out", AllowedLengths: []int{0, 128},
			EpochPaddingBudgetBytes: Ptr(28),
		}},
		Records: []SlotRecord{
			{
				SlotRef: SlotRef{Generation: 1, Slot: 0, Direction: "out"}, RealBytes: 100, PaddingBytes: 28,
				PaddingAllocations: []PaddingAllocation{{ID: "budget-g1-pad"}},
			},
			{
				SlotRef: SlotRef{Generation: 2, Slot: 0, Direction: "out"}, RealBytes: 100, PaddingBytes: 28,
				PaddingAllocations: []PaddingAllocation{{ID: "budget-g2-pad"}},
			},
		},
	}
	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	for _, gen := range []int64{1, 2} {
		s := getSlot(t, rep, gen, 0, "out")
		if hasFinding(s.LengthFindings, CodePaddingEpochBudget) {
			t.Fatalf("generation %d budget should reset and accept exactly 28 bytes: %+v", gen, s.LengthFindings)
		}
		if s.Status != StatusProvenCompliant {
			t.Fatalf("generation %d slot should be compliant at exact budget/tier: %+v", gen, s)
		}
	}
}
