package bootmeasure_test

import (
	"crypto/sha256"
	"fmt"
	"sync"
	"testing"

	"bootmeasure"
)

// Fixture baselines. Each firmware generation carries two approved complete
// sequences ("normal" and "recovery"); one sequence contains an explicitly
// optional step so the optional-step rule is exercised end to end.
func testBaselines() []bootmeasure.Baseline {
	return []bootmeasure.Baseline{
		{
			Version: "gen1-normal-v1.0", Generation: 1, SequenceType: "normal",
			Steps: []bootmeasure.Step{
				{Component: "srtm", ExpectedDigest: digest("srtm-v1")},
				{Component: "bootloader", ExpectedDigest: digest("bootloader-v1")},
				{Component: "option-rom", ExpectedDigest: digest("optionrom-v1"), Optional: true},
				{Component: "kernel", ExpectedDigest: digest("kernel-v1")},
			},
		},
		{
			Version: "gen1-recovery-v1.0", Generation: 1, SequenceType: "recovery",
			Steps: []bootmeasure.Step{
				{Component: "srtm", ExpectedDigest: digest("srtm-v1")},
				{Component: "recovery-loader", ExpectedDigest: digest("recovery-loader-v1")},
				{Component: "kernel", ExpectedDigest: digest("kernel-v1")},
			},
		},
		{
			Version: "gen2-normal-v2.0", Generation: 2, SequenceType: "normal",
			Steps: []bootmeasure.Step{
				{Component: "srtm", ExpectedDigest: digest("srtm-v2")},
				{Component: "bootloader", ExpectedDigest: digest("bootloader-v2")},
				{Component: "option-rom", ExpectedDigest: digest("optionrom-v2"), Optional: true},
				{Component: "kernel", ExpectedDigest: digest("kernel-v2")},
			},
		},
		{
			Version: "gen2-recovery-v2.0", Generation: 2, SequenceType: "recovery",
			Steps: []bootmeasure.Step{
				{Component: "srtm", ExpectedDigest: digest("srtm-v2")},
				{Component: "recovery-loader", ExpectedDigest: digest("recovery-loader-v2")},
				{Component: "kernel", ExpectedDigest: digest("kernel-v2")},
			},
		},
	}
}

func digest(seed string) []byte {
	sum := sha256.Sum256([]byte(seed))
	return sum[:]
}

func m(component, seed string) bootmeasure.Measurement {
	return bootmeasure.Measurement{Component: component, Digest: digest(seed)}
}

func newDecider(t *testing.T, baselines []bootmeasure.Baseline) (*bootmeasure.Decider, *bootmeasure.MemoryNodeStateStore) {
	t.Helper()
	store := bootmeasure.NewMemoryNodeStateStore()
	decider, err := bootmeasure.NewDecider(baselines, store)
	if err != nil {
		t.Fatalf("NewDecider returned unexpected error: %v", err)
	}
	return decider, store
}

// TestAcceptedFullSequence covers the acceptable path: a complete normal
// sequence at the claimed generation, and the same sequence with its
// optional step declared (and with it omitted).
func TestAcceptedFullSequence(t *testing.T) {
	decider, store := newDecider(t, testBaselines())

	cases := []struct {
		name         string
		measurements []bootmeasure.Measurement
		wantVersion  string
	}{
		{
			name: "normal sequence with optional step",
			measurements: []bootmeasure.Measurement{
				m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
				m("option-rom", "optionrom-v1"), m("kernel", "kernel-v1"),
			},
			wantVersion: "gen1-normal-v1.0",
		},
		{
			name: "normal sequence with optional step omitted",
			measurements: []bootmeasure.Measurement{
				m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
				m("kernel", "kernel-v1"),
			},
			wantVersion: "gen1-normal-v1.0",
		},
		{
			name: "recovery sequence",
			measurements: []bootmeasure.Measurement{
				m("srtm", "srtm-v1"), m("recovery-loader", "recovery-loader-v1"),
				m("kernel", "kernel-v1"),
			},
			wantVersion: "gen1-recovery-v1.0",
		},
	}

	for i, tc := range cases {
		node := fmt.Sprintf("node-accept-%d", i)
		decision := decider.Evaluate(bootmeasure.Record{
			NodeID: node, FirmwareGeneration: 1, Measurements: tc.measurements,
		})
		if decision.Status != bootmeasure.StatusAccepted {
			t.Fatalf("%s: status = %q (%s), want accepted", tc.name, decision.Status, decision.Reason)
		}
		if decision.BaselineVersion != tc.wantVersion {
			t.Fatalf("%s: baseline version = %q, want %q", tc.name, decision.BaselineVersion, tc.wantVersion)
		}
		gen, err := store.HighestGeneration(node)
		if err != nil {
			t.Fatalf("%s: store lookup failed: %v", tc.name, err)
		}
		if gen != 1 {
			t.Fatalf("%s: confirmed generation = %d, want 1", tc.name, gen)
		}
	}
}

// TestSplicedSequencesRejected is the core cross-constraint case: a stream
// made of a prefix from one approved sequence and a suffix from another
// must never be accepted, even though every prefix on its own matches a
// real baseline.
func TestSplicedSequencesRejected(t *testing.T) {
	decider, _ := newDecider(t, testBaselines())

	spliced := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"),
		m("bootloader", "bootloader-v1"),           // prefix of gen1 normal
		m("recovery-loader", "recovery-loader-v1"), // tail of gen1 recovery
		m("kernel", "kernel-v1"),
	}
	decision := decider.Evaluate(bootmeasure.Record{
		NodeID: "node-splice", FirmwareGeneration: 1, Measurements: spliced,
	})
	if decision.Status == bootmeasure.StatusAccepted {
		t.Fatalf("spliced normal/recovery sequence was accepted: %+v", decision)
	}
	if decision.BaselineVersion != "" {
		t.Fatalf("rejected splice must not carry a baseline version, got %q", decision.BaselineVersion)
	}

	// Splicing across generations (upgraded components followed by old
	// components) is likewise a splice, not a generation mismatch.
	crossGen := []bootmeasure.Measurement{
		m("srtm", "srtm-v2"),
		m("bootloader", "bootloader-v2"), // gen2 prefix
		m("kernel", "kernel-v1"),         // gen1 suffix
	}
	decision = decider.Evaluate(bootmeasure.Record{
		NodeID: "node-splice-gen", FirmwareGeneration: 2, Measurements: crossGen,
	})
	if decision.Status == bootmeasure.StatusAccepted {
		t.Fatalf("cross-generation splice was accepted: %+v", decision)
	}
}

// TestRollbackBlockedAfterNewGeneration: once a node has been confirmed at
// generation 2, presenting the still-approved generation 1 baseline must be
// blocked for that node, while another node still on generation 1 remains
// accepted — rollback protection is per node, not global.
func TestRollbackBlockedAfterNewGeneration(t *testing.T) {
	decider, store := newDecider(t, testBaselines())
	upgradedNode, otherNode := "node-upgraded", "node-other"

	// Upgraded node boots generation 2 successfully.
	gen2 := []bootmeasure.Measurement{
		m("srtm", "srtm-v2"), m("bootloader", "bootloader-v2"),
		m("option-rom", "optionrom-v2"), m("kernel", "kernel-v2"),
	}
	if d := decider.Evaluate(bootmeasure.Record{
		NodeID: upgradedNode, FirmwareGeneration: 2, Measurements: gen2,
	}); d.Status != bootmeasure.StatusAccepted {
		t.Fatalf("generation 2 boot: %q (%s)", d.Status, d.Reason)
	}

	// The old generation 1 sequence is still fully approved for other
	// nodes, so the other node must still be accepted.
	gen1 := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
		m("kernel", "kernel-v1"),
	}
	if d := decider.Evaluate(bootmeasure.Record{
		NodeID: otherNode, FirmwareGeneration: 1, Measurements: gen1,
	}); d.Status != bootmeasure.StatusAccepted {
		t.Fatalf("other node's generation 1 boot: %q (%s)", d.Status, d.Reason)
	}

	// The upgraded node presenting that same valid generation 1 sequence
	// must be blocked as a rollback, and the decision must still name the
	// baseline it rolled back to.
	rollback := decider.Evaluate(bootmeasure.Record{
		NodeID: upgradedNode, FirmwareGeneration: 1, Measurements: gen1,
	})
	if rollback.Status != bootmeasure.StatusRollbackBlocked {
		t.Fatalf("rollback status = %q (%s), want %q",
			rollback.Status, rollback.Reason, bootmeasure.StatusRollbackBlocked)
	}
	if rollback.BaselineVersion != "gen1-normal-v1.0" {
		t.Fatalf("rollback decision baseline = %q, want gen1-normal-v1.0", rollback.BaselineVersion)
	}

	// The rejected rollback must not move the confirmed generation
	// backwards.
	highest, err := store.HighestGeneration(upgradedNode)
	if err != nil {
		t.Fatalf("store lookup: %v", err)
	}
	if highest != 2 {
		t.Fatalf("highest confirmed generation after rollback attempt = %d, want 2", highest)
	}

	// Re-accepting generation 2 works and keeps the same high-water mark.
	if d := decider.Evaluate(bootmeasure.Record{
		NodeID: upgradedNode, FirmwareGeneration: 2, Measurements: gen2,
	}); d.Status != bootmeasure.StatusAccepted {
		t.Fatalf("repeated generation 2 boot: %q (%s)", d.Status, d.Reason)
	}
	if highest, _ = store.HighestGeneration(upgradedNode); highest != 2 {
		t.Fatalf("highest generation = %d, want 2", highest)
	}
}

// TestMissingVsOrderMismatch pins the two failure modes to distinct statuses.
func TestMissingVsOrderMismatch(t *testing.T) {
	decider, _ := newDecider(t, testBaselines())

	// A required component is simply absent (stream ends early): missing.
	missingTail := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
	}
	d := decider.Evaluate(bootmeasure.Record{
		NodeID: "node-missing", FirmwareGeneration: 1, Measurements: missingTail,
	})
	if d.Status != bootmeasure.StatusMissingMeasurement {
		t.Fatalf("truncated stream: status = %q (%s), want %q",
			d.Status, d.Reason, bootmeasure.StatusMissingMeasurement)
	}

	// Required component dropped while a later component is still measured:
	// the later component arrives out of order, not as a missing record.
	outOfOrder := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"),
		m("kernel", "kernel-v1"), // bootloader skipped, kernel early
	}
	d = decider.Evaluate(bootmeasure.Record{
		NodeID: "node-order", FirmwareGeneration: 1, Measurements: outOfOrder,
	})
	if d.Status != bootmeasure.StatusOrderMismatch {
		t.Fatalf("out-of-order stream: status = %q (%s), want %q",
			d.Status, d.Reason, bootmeasure.StatusOrderMismatch)
	}

	// Unknown component that no baseline contains at all.
	unknown := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"),
		m("evil-loader", "evil"),
		m("kernel", "kernel-v1"),
	}
	d = decider.Evaluate(bootmeasure.Record{
		NodeID: "node-unknown", FirmwareGeneration: 1, Measurements: unknown,
	})
	if d.Status != bootmeasure.StatusNoBaselineMatch {
		t.Fatalf("unknown component: status = %q (%s), want %q",
			d.Status, d.Reason, bootmeasure.StatusNoBaselineMatch)
	}
}

// TestDigestMismatch covers a present component whose measured value does
// not match the baseline, including a mismatch on the optional step.
func TestDigestMismatch(t *testing.T) {
	decider, _ := newDecider(t, testBaselines())

	cases := []struct {
		name         string
		measurements []bootmeasure.Measurement
	}{
		{
			name: "required component wrong digest",
			measurements: []bootmeasure.Measurement{
				m("srtm", "srtm-v1"), m("bootloader", "tampered"),
				m("kernel", "kernel-v1"),
			},
		},
		{
			name: "optional component wrong digest",
			measurements: []bootmeasure.Measurement{
				m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
				m("option-rom", "tampered-option"), m("kernel", "kernel-v1"),
			},
		},
	}
	for _, tc := range cases {
		d := decider.Evaluate(bootmeasure.Record{
			NodeID: "node-digest", FirmwareGeneration: 1, Measurements: tc.measurements,
		})
		if d.Status != bootmeasure.StatusDigestMismatch {
			t.Fatalf("%s: status = %q (%s), want %q",
				tc.name, d.Status, d.Reason, bootmeasure.StatusDigestMismatch)
		}
	}
}

// TestOptionalStepInterleaving is the boundary/interleaving case: an
// optional step may be present or absent, but it cannot be used to mask a
// skipped required step or be measured after a later required component.
func TestOptionalStepInterleaving(t *testing.T) {
	decider, _ := newDecider(t, testBaselines())

	// Optional step measured in its declared position, between required
	// steps: accepted.
	goodInterleave := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
		m("option-rom", "optionrom-v1"), m("kernel", "kernel-v1"),
	}
	if d := decider.Evaluate(bootmeasure.Record{
		NodeID: "node-opt-ok", FirmwareGeneration: 1, Measurements: goodInterleave,
	}); d.Status != bootmeasure.StatusAccepted {
		t.Fatalf("optional step in position: %q (%s)", d.Status, d.Reason)
	}

	// Optional step measured after the component that follows it: order
	// mismatch. Optional does not mean free placement.
	lateOptional := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
		m("kernel", "kernel-v1"), m("option-rom", "optionrom-v1"),
	}
	if d := decider.Evaluate(bootmeasure.Record{
		NodeID: "node-opt-late", FirmwareGeneration: 1, Measurements: lateOptional,
	}); d.Status != bootmeasure.StatusOrderMismatch {
		t.Fatalf("late optional step: status = %q (%s), want %q",
			d.Status, d.Reason, bootmeasure.StatusOrderMismatch)
	}

	// Duplicate declaration of the optional step: the second copy has no
	// baseline position and must not be accepted.
	duplicateOptional := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
		m("option-rom", "optionrom-v1"), m("option-rom", "optionrom-v1"),
		m("kernel", "kernel-v1"),
	}
	if d := decider.Evaluate(bootmeasure.Record{
		NodeID: "node-opt-dup", FirmwareGeneration: 1, Measurements: duplicateOptional,
	}); d.Status == bootmeasure.StatusAccepted {
		t.Fatalf("duplicate optional step was accepted: %+v", d)
	}
}

// TestGenerationMismatch verifies that a fully matching sequence whose
// baseline belongs to another generation is rejected with the baseline
// version attached, and does not confirm any generation for the node.
func TestGenerationMismatch(t *testing.T) {
	decider, store := newDecider(t, testBaselines())
	gen1Normal := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
		m("kernel", "kernel-v1"),
	}
	d := decider.Evaluate(bootmeasure.Record{
		NodeID: "node-gen", FirmwareGeneration: 2, Measurements: gen1Normal,
	})
	if d.Status != bootmeasure.StatusGenerationMismatch {
		t.Fatalf("generation mismatch: status = %q (%s), want %q",
			d.Status, d.Reason, bootmeasure.StatusGenerationMismatch)
	}
	if d.BaselineVersion != "gen1-normal-v1.0" {
		t.Fatalf("generation mismatch baseline = %q, want gen1-normal-v1.0", d.BaselineVersion)
	}
	highest, err := store.HighestGeneration("node-gen")
	if err != nil {
		t.Fatalf("store lookup: %v", err)
	}
	if highest != 0 {
		t.Fatalf("mismatched record confirmed generation %d, want no state change (0)", highest)
	}
}

// TestInvalidInputs guards the record-validation boundary.
func TestInvalidInputs(t *testing.T) {
	decider, _ := newDecider(t, testBaselines())

	valid := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
		m("kernel", "kernel-v1"),
	}
	cases := []struct {
		name   string
		record bootmeasure.Record
	}{
		{"empty node id", bootmeasure.Record{FirmwareGeneration: 1, Measurements: valid}},
		{"no measurements", bootmeasure.Record{NodeID: "node-empty", FirmwareGeneration: 1}},
		{"empty component name", bootmeasure.Record{
			NodeID: "node-bad", FirmwareGeneration: 1,
			Measurements: []bootmeasure.Measurement{{Component: "", Digest: digest("x")}},
		}},
		{"empty digest", bootmeasure.Record{
			NodeID: "node-bad", FirmwareGeneration: 1,
			Measurements: []bootmeasure.Measurement{{Component: "srtm", Digest: nil}},
		}},
	}
	for _, tc := range cases {
		if d := decider.Evaluate(tc.record); d.Status != bootmeasure.StatusInvalidInput {
			t.Fatalf("%s: status = %q (%s), want %q",
				tc.name, d.Status, d.Reason, bootmeasure.StatusInvalidInput)
		}
	}
}

// TestAmbiguousBaseline is a defensive case: if the approved set contains
// two baselines that both match a record completely, the module must refuse
// to pick one rather than attest an arbitrary baseline version.
func TestAmbiguousBaseline(t *testing.T) {
	baselines := []bootmeasure.Baseline{
		{
			Version: "normal-a", Generation: 1, SequenceType: "normal",
			Steps: []bootmeasure.Step{
				{Component: "srtm", ExpectedDigest: digest("same")},
				{Component: "kernel", ExpectedDigest: digest("same-k")},
			},
		},
		{
			Version: "normal-b", Generation: 1, SequenceType: "normal",
			Steps: []bootmeasure.Step{
				{Component: "srtm", ExpectedDigest: digest("same")},
				{Component: "kernel", ExpectedDigest: digest("same-k")},
			},
		},
	}
	decider, _ := newDecider(t, baselines)
	d := decider.Evaluate(bootmeasure.Record{
		NodeID: "node-amb", FirmwareGeneration: 1,
		Measurements: []bootmeasure.Measurement{m("srtm", "same"), m("kernel", "same-k")},
	})
	if d.Status != bootmeasure.StatusAmbiguousBaseline {
		t.Fatalf("ambiguous baselines: status = %q (%s), want %q",
			d.Status, d.Reason, bootmeasure.StatusAmbiguousBaseline)
	}
	if d.BaselineVersion != "" {
		t.Fatalf("ambiguous decision must not name a baseline version, got %q", d.BaselineVersion)
	}
}

// TestStoreMonotonicAndFreshNode pins the state-store contract and the
// fresh-node boundary (no confirmed generation means nothing to roll back).
func TestStoreMonotonicAndFreshNode(t *testing.T) {
	decider, store := newDecider(t, testBaselines())
	fresh := "node-fresh"

	highest, err := store.HighestGeneration(fresh)
	if err != nil {
		t.Fatalf("fresh node lookup: %v", err)
	}
	if highest != 0 {
		t.Fatalf("fresh node highest generation = %d, want 0", highest)
	}

	gen1 := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"), m("recovery-loader", "recovery-loader-v1"),
		m("kernel", "kernel-v1"),
	}
	if d := decider.Evaluate(bootmeasure.Record{
		NodeID: fresh, FirmwareGeneration: 1, Measurements: gen1,
	}); d.Status != bootmeasure.StatusAccepted {
		t.Fatalf("fresh node generation 1 boot: %q (%s)", d.Status, d.Reason)
	}

	// Direct monotonicity enforcement on the store.
	if err := store.CommitGeneration(fresh, 1); err != nil {
		t.Fatalf("re-committing same generation failed: %v", err)
	}
	if err := store.CommitGeneration(fresh, 0); err != bootmeasure.ErrRollbackAttempt {
		t.Fatalf("backward commit error = %v, want ErrRollbackAttempt", err)
	}
	if highest, _ = store.HighestGeneration(fresh); highest != 1 {
		t.Fatalf("highest generation after rejected commit = %d, want 1", highest)
	}
}

// TestConcurrentGenerationInterleaving is the interleaving case across
// goroutines: generation-1 and generation-2 records for the same node race
// each other. Once any generation-2 record is accepted, later generation-1
// attempts must be blocked as rollbacks; every decision must be one of the
// two coherent outcomes and the stored high-water mark must stay monotonic.
func TestConcurrentGenerationInterleaving(t *testing.T) {
	decider, store := newDecider(t, testBaselines())
	node := "node-race"

	gen1 := []bootmeasure.Measurement{
		m("srtm", "srtm-v1"), m("bootloader", "bootloader-v1"),
		m("kernel", "kernel-v1"),
	}
	gen2 := []bootmeasure.Measurement{
		m("srtm", "srtm-v2"), m("bootloader", "bootloader-v2"),
		m("kernel", "kernel-v2"),
	}

	const goroutines = 32
	var wg sync.WaitGroup
	results := make(chan bootmeasure.Status, goroutines*2)
	start := make(chan struct{})

	for i := 0; i < goroutines; i++ {
		wg.Add(2)
		go func() {
			defer wg.Done()
			<-start
			d := decider.Evaluate(bootmeasure.Record{
				NodeID: node, FirmwareGeneration: 1, Measurements: gen1,
			})
			results <- d.Status
		}()
		go func() {
			defer wg.Done()
			<-start
			d := decider.Evaluate(bootmeasure.Record{
				NodeID: node, FirmwareGeneration: 2, Measurements: gen2,
			})
			results <- d.Status
		}()
	}
	close(start)
	wg.Wait()
	close(results)

	var accepted, rollbackBlocked, other int
	for status := range results {
		switch status {
		case bootmeasure.StatusAccepted:
			accepted++
		case bootmeasure.StatusRollbackBlocked:
			rollbackBlocked++
		default:
			other++
			t.Errorf("unexpected interleaved status %q", status)
		}
	}
	if accepted == 0 {
		t.Fatal("at least one record must have been accepted")
	}
	if other != 0 {
		t.Fatalf("interleaving produced %d incoherent decisions", other)
	}

	highest, err := store.HighestGeneration(node)
	if err != nil {
		t.Fatalf("store lookup after race: %v", err)
	}
	if highest != 2 {
		t.Fatalf("highest generation after interleaving = %d, want 2", highest)
	}

	// After the race settles, generation 1 is deterministically blocked.
	d := decider.Evaluate(bootmeasure.Record{
		NodeID: node, FirmwareGeneration: 1, Measurements: gen1,
	})
	if d.Status != bootmeasure.StatusRollbackBlocked {
		t.Fatalf("post-race gen1 status = %q, want %q", d.Status, bootmeasure.StatusRollbackBlocked)
	}
	if d.BaselineVersion != "gen1-normal-v1.0" {
		t.Fatalf("post-race rollback baseline = %q, want gen1-normal-v1.0", d.BaselineVersion)
	}
}

// TestNewDeciderValidation ensures misconfiguration is rejected at
// construction time rather than producing misleading decisions.
func TestNewDeciderValidation(t *testing.T) {
	good := testBaselines()

	if _, err := bootmeasure.NewDecider(good, nil); err == nil {
		t.Fatal("nil store must be rejected")
	}
	if _, err := bootmeasure.NewDecider(nil, bootmeasure.NewMemoryNodeStateStore()); err == nil {
		t.Fatal("empty baseline set must be rejected")
	}

	dup := append([]bootmeasure.Baseline{}, good...)
	dup[1] = dup[0]
	if _, err := bootmeasure.NewDecider(dup, bootmeasure.NewMemoryNodeStateStore()); err == nil {
		t.Fatal("duplicate baseline versions must be rejected")
	}

	badStep := append([]bootmeasure.Baseline{}, good...)
	badStep[0].Steps[0].ExpectedDigest = nil
	if _, err := bootmeasure.NewDecider(badStep, bootmeasure.NewMemoryNodeStateStore()); err == nil {
		t.Fatal("baseline step without digest must be rejected")
	}
}
