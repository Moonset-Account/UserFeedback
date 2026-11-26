package slotcheck

import (
	"encoding/json"
	"errors"
	"testing"
)

func baseInput() Input {
	return Input{
		Generations: []Epoch{{Generation: 1}},
		Template:    SlotTemplate{FirstSlot: 0, SlotCount: 2},
		Policies: []DirectionPolicy{
			{
				Direction:               "out",
				AllowedLengths:          []int{0, 128, 256},
				MaxPaddingBytes:         Ptr(64),
				EpochPaddingBudgetBytes: Ptr(96),
			},
			{
				Direction:       "in",
				AllowedLengths:  []int{0, 64, 512},
				MaxPaddingBytes: Ptr(32),
			},
		},
	}
}

func getSlot(t *testing.T, rep *Report, gen int64, slot int, direction string) SlotReport {
	t.Helper()
	want := SlotRef{Generation: gen, Slot: slot, Direction: direction}
	for _, s := range rep.Slots {
		if s.Slot == want {
			return s
		}
	}
	t.Fatalf("slot %s not found in %d reports", want, len(rep.Slots))
	return SlotReport{}
}

func hasFinding(fs []Finding, code string) bool {
	for _, f := range fs {
		if f.Code == code {
			return true
		}
	}
	return false
}

func findFinding(fs []Finding, code string) (Finding, bool) {
	for _, f := range fs {
		if f.Code == code {
			return f, true
		}
	}
	return Finding{}, false
}

func TestCompliantSlotsAndIndependentEmptySlots(t *testing.T) {
	in := baseInput()
	in.Records = []SlotRecord{
		{
			SlotRef:              SlotRef{Generation: 1, Slot: 0, Direction: "out"},
			ObservedVisibleBytes: Ptr(128),
			RealBytes:            100,
			PaddingBytes:         28,
			PaddingAllocations:   []PaddingAllocation{{ID: "pad-out-0", Bytes: Ptr(28)}},
		},
		{
			SlotRef:              SlotRef{Generation: 1, Slot: 0, Direction: "in"},
			ObservedVisibleBytes: Ptr(64),
			RealBytes:            40,
			PaddingBytes:         24,
		},
	}

	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Summary.Slots != 4 {
		t.Fatalf("expected four direction-slot pairs, got %d", rep.Summary.Slots)
	}
	if rep.Summary.ProvenCompliant != 4 || rep.Summary.ExceededVisibilityTier != 0 || rep.Summary.InsufficientEvidence != 0 {
		t.Fatalf("unexpected summary: %+v", rep.Summary)
	}
	out0 := getSlot(t, rep, 1, 0, "out")
	if out0.Status != StatusProvenCompliant || out0.SelectedTier == nil || out0.SelectedTier.Length != 128 {
		t.Fatalf("out/0 not proven tier 128: %+v", out0)
	}
	if out0.StatusLabelZH != "已证明符合" {
		t.Fatalf("unexpected Chinese status label: %q", out0.StatusLabelZH)
	}
	out1 := getSlot(t, rep, 1, 1, "out")
	if !out1.Empty || out1.EffectiveVisibleBytes != 0 || out1.Status != StatusProvenCompliant {
		t.Fatalf("empty out/1 should be proven zero-compliant: %+v", out1)
	}
	in1 := getSlot(t, rep, 1, 1, "in")
	if in1.Status != StatusProvenCompliant || in1.EffectiveVisibleBytes != 0 {
		t.Fatalf("empty in/1 should be independently allowed zero: %+v", in1)
	}
}

func TestEmptySlotDirectionPoliciesDiffer(t *testing.T) {
	in := baseInput()
	in.Template = SlotTemplate{FirstSlot: 0, SlotCount: 1}
	in.Policies = []DirectionPolicy{
		{Direction: "silence-allowed", AllowedLengths: []int{0, 128}},
		{Direction: "constant-shape", AllowedLengths: []int{128}},
		{Direction: "explicit-evidence", AllowedLengths: []int{0, 128}, EmptyPolicy: EmptyRequireRecord},
	}

	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	if got := getSlot(t, rep, 1, 0, "silence-allowed"); got.Status != StatusProvenCompliant {
		t.Fatalf("zero tier should allow empty slot: %+v", got)
	}
	constant := getSlot(t, rep, 1, 0, "constant-shape")
	if constant.Status != StatusExceededVisibilityTier ||
		!hasFinding(constant.LengthFindings, CodeEmptyLengthNotAllowed) {
		t.Fatalf("empty constant-shape slot should be a located length failure: %+v", constant)
	}
	explicit := getSlot(t, rep, 1, 0, "explicit-evidence")
	if explicit.Status != StatusInsufficientEvidence ||
		!hasFinding(explicit.EvidenceGaps, CodeMissingSlotRecord) {
		t.Fatalf("require_record policy should report missing evidence: %+v", explicit)
	}
}

func TestTierViolationAndVisibleLengthMismatchAreLocated(t *testing.T) {
	in := baseInput()
	in.Template.SlotCount = 2
	in.Records = []SlotRecord{
		{
			SlotRef:              SlotRef{Generation: 1, Slot: 0, Direction: "out"},
			ObservedVisibleBytes: Ptr(300),
			RealBytes:            250,
			PaddingBytes:         50,
		},
		{
			SlotRef:              SlotRef{Generation: 1, Slot: 1, Direction: "out"},
			ObservedVisibleBytes: Ptr(256),
			RealBytes:            200,
			PaddingBytes:         40,
		},
	}

	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	s0 := getSlot(t, rep, 1, 0, "out")
	if s0.Status != StatusExceededVisibilityTier || !hasFinding(s0.LengthFindings, CodeTierNotAllowed) {
		t.Fatalf("expected tier violation at out/0: %+v", s0)
	}
	s1 := getSlot(t, rep, 1, 1, "out")
	if s1.Status != StatusExceededVisibilityTier {
		t.Fatalf("accounting mismatch must not be called compliant: %+v", s1)
	}
	f, ok := findFinding(s1.LengthFindings, CodeVisibleLengthMismatch)
	if !ok || f.Expected != 240 || f.Actual != 256 {
		t.Fatalf("expected mismatch 240 vs 256, got %+v", s1.LengthFindings)
	}
}

func TestPaddingBudgets(t *testing.T) {
	in := baseInput()
	in.Template.SlotCount = 2
	in.Policies[0].MaxPaddingBytes = Ptr(32)
	in.Policies[0].EpochPaddingBudgetBytes = Ptr(50)
	in.Records = []SlotRecord{
		{SlotRef: SlotRef{Generation: 1, Slot: 0, Direction: "out"}, ObservedVisibleBytes: Ptr(128), RealBytes: 98, PaddingBytes: 30},
		{SlotRef: SlotRef{Generation: 1, Slot: 1, Direction: "out"}, ObservedVisibleBytes: Ptr(128), RealBytes: 98, PaddingBytes: 30},
	}

	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	// Per-slot 30 <= 32, but generation total 60 > 50.
	s0 := getSlot(t, rep, 1, 0, "out")
	if !hasFinding(s0.LengthFindings, CodePaddingEpochBudget) {
		t.Fatalf("expected epoch budget finding at first slot: %+v", s0.LengthFindings)
	}

	in.Policies[0].MaxPaddingBytes = Ptr(16)
	rep, err = Check(in)
	if err != nil {
		t.Fatal(err)
	}
	s0 = getSlot(t, rep, 1, 0, "out")
	if !hasFinding(s0.LengthFindings, CodePaddingSlotBudget) {
		t.Fatalf("expected per-slot padding budget finding: %+v", s0.LengthFindings)
	}
}

func TestDuplicatePaddingAcrossSlotsIsLocatedOnBothSlots(t *testing.T) {
	in := baseInput()
	in.Template.SlotCount = 2
	in.Policies[0].EpochPaddingBudgetBytes = nil
	in.Records = []SlotRecord{
		{
			SlotRef:              SlotRef{Generation: 1, Slot: 0, Direction: "out"},
			ObservedVisibleBytes: Ptr(128), RealBytes: 100, PaddingBytes: 28,
			PaddingAllocations: []PaddingAllocation{{ID: "same-grant", Bytes: Ptr(28)}},
		},
		{
			SlotRef:              SlotRef{Generation: 1, Slot: 1, Direction: "out"},
			ObservedVisibleBytes: Ptr(128), RealBytes: 100, PaddingBytes: 28,
			PaddingAllocations: []PaddingAllocation{{ID: "same-grant", Bytes: Ptr(28)}},
		},
	}

	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	for _, slot := range []int{0, 1} {
		s := getSlot(t, rep, 1, slot, "out")
		f, ok := findFinding(s.LengthFindings, CodePaddingDuplicate)
		if !ok || f.PaddingID != "same-grant" || f.OtherSlot == nil {
			t.Fatalf("slot %d missing located duplicate padding: %+v", slot, s.LengthFindings)
		}
		if s.Status != StatusExceededVisibilityTier {
			t.Fatalf("duplicate padding must be definitive length failure")
		}
	}
}

func TestClockResyncPaddingAndReusedSlotIndexes(t *testing.T) {
	in := Input{
		Generations: []Epoch{{Generation: 1}, {Generation: 2, Resync: true}},
		Template:    SlotTemplate{SlotIndexes: []int{0}},
		Policies: []DirectionPolicy{{
			Direction:      "out",
			AllowedLengths: []int{0, 128},
		}},
		Records: []SlotRecord{
			{
				SlotRef: SlotRef{Generation: 1, Slot: 0, Direction: "out"}, ObservedVisibleBytes: Ptr(128),
				RealBytes: 100, PaddingBytes: 28,
				PaddingAllocations: []PaddingAllocation{{ID: "resync-grant"}},
			},
			{
				SlotRef: SlotRef{Generation: 2, Slot: 0, Direction: "out"}, ObservedVisibleBytes: Ptr(128),
				RealBytes: 100, PaddingBytes: 28,
				PaddingAllocations: []PaddingAllocation{{ID: "resync-grant"}},
			},
		},
	}
	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	g1 := getSlot(t, rep, 1, 0, "out")
	g2 := getSlot(t, rep, 2, 0, "out")
	if !hasFinding(g1.LengthFindings, CodePaddingResyncDuplicate) ||
		!hasFinding(g2.LengthFindings, CodePaddingResyncDuplicate) {
		t.Fatalf("expected resync-specific double-count on both generations: %+v / %+v", g1.LengthFindings, g2.LengthFindings)
	}

	// Reusing slot index 0 after resync is not itself a collision when the
	// generation-qualified coordinates have distinct allocation IDs.
	in.Records[0].PaddingAllocations[0].ID = "old-grant"
	in.Records[1].PaddingAllocations[0].ID = "new-grant"
	rep, err = Check(in)
	if err != nil {
		t.Fatal(err)
	}
	if got := getSlot(t, rep, 1, 0, "out"); got.Status != StatusProvenCompliant {
		t.Fatalf("old generation slot should be compliant: %+v", got)
	}
	if got := getSlot(t, rep, 2, 0, "out"); got.Status != StatusProvenCompliant {
		t.Fatalf("new generation slot should be independently compliant: %+v", got)
	}

	// Positive padding without an identity at the resync boundary cannot be
	// proved unique.
	in.Records[1].PaddingAllocations = nil
	rep, err = Check(in)
	if err != nil {
		t.Fatal(err)
	}
	g2 = getSlot(t, rep, 2, 0, "out")
	if g2.Status != StatusInsufficientEvidence || !hasFinding(g2.EvidenceGaps, CodePaddingIdentityMissing) {
		t.Fatalf("expected padding identity evidence gap: %+v", g2)
	}
}

func TestDelayedChunkMatchingReceiptAndShuffledInterleaving(t *testing.T) {
	origin := SlotRef{Generation: 1, Slot: 0, Direction: "out"}
	reissue := SlotRef{Generation: 1, Slot: 2, Direction: "out"}
	in := baseInput()
	in.Template = SlotTemplate{FirstSlot: 0, SlotCount: 3}
	in.Records = []SlotRecord{
		{
			// Intentionally supplied out of chronological order.
			SlotRef:              reissue,
			ObservedVisibleBytes: Ptr(128),
			RealBytes:            100,
			PaddingBytes:         28,
			ReissuedChunkIDs:     []string{"chunk-1"},
		},
		{
			SlotRef:              origin,
			ObservedVisibleBytes: Ptr(0),
			RealBytes:            0,
			PaddingBytes:         0,
		},
	}
	in.DelayedChunks = []DelayedChunk{{
		ChunkID: "chunk-1", Bytes: 100, Origin: origin, Reissue: &reissue,
	}}
	in.Receipts = []Receipt{{
		ID: "receipt-1", ChunkID: "chunk-1", Bytes: 100,
		Origin: origin, Reissue: &reissue,
	}}

	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Summary.InsufficientEvidence != 0 || rep.Summary.ExceededVisibilityTier != 0 {
		t.Fatalf("expected all slots proven, summary=%+v reports=%+v", rep.Summary, rep.Slots)
	}
	o := getSlot(t, rep, 1, 0, "out")
	r := getSlot(t, rep, 1, 2, "out")
	if o.Status != StatusProvenCompliant || r.Status != StatusProvenCompliant {
		t.Fatalf("delay/receipt pair should prove both slots: origin=%+v reissue=%+v", o, r)
	}
}

func TestMissingAndMismatchedReceiptsAreConfirmationGaps(t *testing.T) {
	origin := SlotRef{Generation: 1, Slot: 0, Direction: "out"}
	reissue := SlotRef{Generation: 1, Slot: 2, Direction: "out"}

	t.Run("missing", func(t *testing.T) {
		in := baseInput()
		in.Template.SlotCount = 3
		in.DelayedChunks = []DelayedChunk{{ChunkID: "c", Bytes: 10, Origin: origin, Reissue: &reissue}}
		rep, err := Check(in)
		if err != nil {
			t.Fatal(err)
		}
		o := getSlot(t, rep, 1, 0, "out")
		r := getSlot(t, rep, 1, 2, "out")
		if o.Status != StatusInsufficientEvidence || !hasFinding(o.ConfirmationGaps, CodeReceiptMissing) {
			t.Fatalf("origin should have missing receipt gap: %+v", o)
		}
		if r.Status != StatusInsufficientEvidence || !hasFinding(r.ConfirmationGaps, CodeReceiptMissing) {
			t.Fatalf("known reissue slot should also locate the gap: %+v", r)
		}
	})

	mismatchTests := []struct {
		name string
		rcpt Receipt
		code string
	}{
		{
			name: "bytes",
			code: CodeReceiptBytesMismatch,
			rcpt: Receipt{ID: "r", ChunkID: "c", Bytes: 11, Origin: origin, Reissue: &reissue},
		},
		{
			name: "origin",
			code: CodeReceiptOriginMismatch,
			rcpt: Receipt{
				ID: "r", ChunkID: "c", Bytes: 10,
				Origin:  SlotRef{Generation: 1, Slot: 1, Direction: "out"},
				Reissue: &reissue,
			},
		},
		{
			name: "reissue",
			code: CodeReceiptReissueMismatch,
			rcpt: Receipt{
				ID: "r", ChunkID: "c", Bytes: 10, Origin: origin,
				Reissue: Ptr(SlotRef{Generation: 1, Slot: 1, Direction: "out"}),
			},
		},
		{
			name: "missing reissue coordinate",
			code: CodeReceiptReissueMissing,
			rcpt: Receipt{ID: "r", ChunkID: "c", Bytes: 10, Origin: origin},
		},
	}
	for _, tc := range mismatchTests {
		t.Run(tc.name, func(t *testing.T) {
			in := baseInput()
			in.Template.SlotCount = 3
			in.DelayedChunks = []DelayedChunk{{ChunkID: "c", Bytes: 10, Origin: origin, Reissue: &reissue}}
			in.Receipts = []Receipt{tc.rcpt}
			rep, err := Check(in)
			if err != nil {
				t.Fatal(err)
			}
			o := getSlot(t, rep, 1, 0, "out")
			if !hasFinding(o.ConfirmationGaps, tc.code) {
				t.Fatalf("expected confirmation code %s in %+v", tc.code, o.ConfirmationGaps)
			}
			if o.Status != StatusInsufficientEvidence || len(o.LengthFindings) != 0 {
				t.Fatalf("receipt mismatch is confirmation gap, not length error: %+v", o)
			}
		})
	}
}

func TestBacklogEquationAndAggregateReceipt(t *testing.T) {
	t.Run("equation compliant", func(t *testing.T) {
		in := baseInput()
		in.Records = []SlotRecord{{
			SlotRef:              SlotRef{Generation: 1, Slot: 0, Direction: "out"},
			ObservedVisibleBytes: Ptr(128),
			RealBytes:            120,
			PaddingBytes:         8,
			PriorBacklogBytes:    0,
			NewRealBytes:         Ptr(120),
			NextBacklogBytes:     Ptr(0),
		}}
		rep, err := Check(in)
		if err != nil {
			t.Fatal(err)
		}
		if got := getSlot(t, rep, 1, 0, "out"); got.Status != StatusProvenCompliant {
			t.Fatalf("valid backlog equation should be compliant: %+v", got)
		}
	})

	t.Run("equation mismatch is length error", func(t *testing.T) {
		in := baseInput()
		in.Records = []SlotRecord{{
			SlotRef:              SlotRef{Generation: 1, Slot: 0, Direction: "out"},
			ObservedVisibleBytes: Ptr(128),
			RealBytes:            110,
			PaddingBytes:         18,
			PriorBacklogBytes:    20,
			NewRealBytes:         Ptr(100),
			NextBacklogBytes:     Ptr(0),
		}}
		rep, err := Check(in)
		if err != nil {
			t.Fatal(err)
		}
		got := getSlot(t, rep, 1, 0, "out")
		if got.Status != StatusExceededVisibilityTier || !hasFinding(got.LengthFindings, CodeBacklogMismatch) {
			t.Fatalf("expected located backlog mismatch: %+v", got)
		}
	})

	t.Run("aggregate backlog needs receipt coverage", func(t *testing.T) {
		origin := SlotRef{Generation: 1, Slot: 0, Direction: "out"}
		reissue := SlotRef{Generation: 1, Slot: 1, Direction: "out"}
		in := baseInput()
		in.Records = []SlotRecord{{
			SlotRef: reissue, ObservedVisibleBytes: Ptr(128),
			RealBytes: 100, PaddingBytes: 28, PriorBacklogBytes: 100,
			BacklogReceiptIDs: []string{"backlog-receipt"},
		}}
		in.Receipts = []Receipt{{
			ID: "backlog-receipt", ChunkID: "aggregate-backlog", Bytes: 100,
			Origin: origin, Reissue: &reissue,
		}}
		rep, err := Check(in)
		if err != nil {
			t.Fatal(err)
		}
		got := getSlot(t, rep, 1, 1, "out")
		if got.Status != StatusProvenCompliant {
			t.Fatalf("receipt-covered backlog should pass: %+v", got)
		}

		in.Receipts[0].Bytes = 90
		rep, err = Check(in)
		if err != nil {
			t.Fatal(err)
		}
		got = getSlot(t, rep, 1, 1, "out")
		if got.Status != StatusInsufficientEvidence || !hasFinding(got.ConfirmationGaps, CodeBacklogReceiptMismatch) {
			t.Fatalf("expected aggregate receipt mismatch: %+v", got)
		}
	})
}

func TestLengthFailureAndConfirmationGapCoexist(t *testing.T) {
	origin := SlotRef{Generation: 1, Slot: 0, Direction: "out"}
	reissue := SlotRef{Generation: 1, Slot: 1, Direction: "out"}
	in := baseInput()
	in.Records = []SlotRecord{{
		SlotRef: origin, ObservedVisibleBytes: Ptr(999),
		RealBytes: 950, PaddingBytes: 49,
	}}
	in.DelayedChunks = []DelayedChunk{{ChunkID: "c", Bytes: 5, Origin: origin, Reissue: &reissue}}

	rep, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	got := getSlot(t, rep, 1, 0, "out")
	if got.Status != StatusExceededVisibilityTier {
		t.Fatalf("definitive length failure should take verdict precedence: %+v", got)
	}
	if !hasFinding(got.LengthFindings, CodeTierNotAllowed) {
		t.Fatalf("missing tier finding: %+v", got.LengthFindings)
	}
	if !hasFinding(got.ConfirmationGaps, CodeReceiptMissing) {
		t.Fatalf("confirmation gap should still be located separately: %+v", got.ConfirmationGaps)
	}
}

func TestDeterministicSlotOrderAndJSONCoordinates(t *testing.T) {
	in := baseInput()
	in.Records = []SlotRecord{
		{SlotRef: SlotRef{Generation: 1, Slot: 1, Direction: "in"}, ObservedVisibleBytes: Ptr(0)},
		{SlotRef: SlotRef{Generation: 1, Slot: 0, Direction: "out"}, ObservedVisibleBytes: Ptr(128), RealBytes: 128},
	}
	first, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	second, err := Check(in)
	if err != nil {
		t.Fatal(err)
	}
	aj, _ := json.Marshal(first.ReportOrPanic())
	bj, _ := json.Marshal(second.ReportOrPanic())
	if string(aj) != string(bj) {
		t.Fatalf("reports are not deterministic:\n%s\n%s", aj, bj)
	}
	if len(first.Slots) < 2 || first.Slots[0].Slot.Direction > first.Slots[len(first.Slots)-1].Slot.Direction {
		t.Fatalf("slots not sorted by direction/generation/index: %+v", first.Slots)
	}

	var rec SlotRecord
	data, err := json.Marshal(SlotRecord{SlotRef: SlotRef{Generation: 7, Slot: 2, Direction: "out"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &rec); err != nil {
		t.Fatal(err)
	}
	if rec.Generation != 7 || rec.Slot != 2 || rec.Direction != "out" {
		t.Fatalf("promoted SlotRef JSON did not round-trip: %s -> %+v", data, rec)
	}
}

// ReportOrPanic exists only to make the determinism assertion read naturally;
// Check already returned no error above.
func (r *Report) ReportOrPanic() *Report { return r }

func TestInvalidRequests(t *testing.T) {
	tests := []struct {
		name string
		mut  func(*Input)
		want error
	}{
		{"no policies", func(in *Input) { in.Policies = nil }, nil},
		{"unknown direction", func(in *Input) {
			in.Records = []SlotRecord{{SlotRef: SlotRef{Generation: 1, Slot: 0, Direction: "sideways"}}}
		}, nil},
		{"duplicate slot record", func(in *Input) {
			ref := SlotRef{Generation: 1, Slot: 0, Direction: "out"}
			in.Records = []SlotRecord{{SlotRef: ref}, {SlotRef: ref}}
		}, nil},
		{"negative bytes", func(in *Input) {
			in.Records = []SlotRecord{{SlotRef: SlotRef{Generation: 1, Slot: 0, Direction: "out"}, RealBytes: -1}}
		}, nil},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			in := baseInput()
			tc.mut(&in)
			_, err := Check(in)
			if err == nil {
				t.Fatalf("expected invalid request error")
			}
			if tc.want != nil && !errors.Is(err, tc.want) {
				t.Fatalf("error %v does not match %v", err, tc.want)
			}
		})
	}
}
