package wipeverify_test

import (
	"reflect"
	"strings"
	"testing"

	"wipeverify"
)

const testVolume = "vol-7f3a"

// demoManifest is the volume's last sealing manifest: after a migration on
// host-a the key lives in host-a slot 5 (generation 1) and host-b slot 1
// (generation 2).
func demoManifest() wipeverify.Manifest {
	return wipeverify.Manifest{
		Volume: testVolume,
		Copies: []wipeverify.CopyRef{
			{Device: "host-a", Slot: 5, Generation: 1},
			{Device: "host-b", Slot: 1, Generation: 2},
		},
	}
}

// demoReceipts is the full synthetic receipt set for the volume lifetime.
func demoReceipts() []wipeverify.Receipt {
	return []wipeverify.Receipt{
		{Device: "host-a", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 3, Generation: 7},
		{Device: "host-b", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 1, Generation: 2},
		{Device: "host-a", Seq: 2, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 5, Generation: 1},
		{Device: "host-a", Seq: 3, Kind: wipeverify.KindMigrate, Volume: testVolume, Slot: 3, Generation: 7},
		{Device: "host-b", Seq: 2, Kind: wipeverify.KindDestroy, Volume: testVolume, Slot: 1, Generation: 2},
		{Device: "host-a", Seq: 4, Kind: wipeverify.KindDestroy, Volume: testVolume, Slot: 5, Generation: 1},
	}
}

func mustAdd(t *testing.T, v *wipeverify.Verifier, receipts ...wipeverify.Receipt) {
	t.Helper()
	for _, r := range receipts {
		if err := v.AddReceipt(r); err != nil {
			t.Fatalf("AddReceipt(%+v): %v", r, err)
		}
	}
}

func findCopy(t *testing.T, rep wipeverify.Report, device string, slot int, gen uint64) wipeverify.CopyStatus {
	t.Helper()
	for _, c := range rep.Copies {
		if c.Copy.Device == device && c.Copy.Slot == slot && c.Copy.Generation == gen {
			return c
		}
	}
	t.Fatalf("copy %s slot %d gen %d not in report: %+v", device, slot, gen, rep.Copies)
	return wipeverify.CopyStatus{}
}

// TestDecommissionFlow is the required demo: old slot cleared by migration
// while the migration target is still alive -> pending; the last valid
// receipt completes the erasure and the certificate can be issued.
func TestDecommissionFlow(t *testing.T) {
	v := wipeverify.NewVerifier(demoManifest())
	all := demoReceipts()
	// Everything except the final destroy of the migration target.
	mustAdd(t, v, all[:5]...)

	rep := v.Report()
	if rep.Complete {
		t.Fatalf("report must not be complete while host-a slot 5 gen 1 is alive")
	}
	if rep.Pending != 1 {
		t.Fatalf("want exactly 1 pending copy, got %d", rep.Pending)
	}

	old := findCopy(t, rep, "host-a", 3, 7)
	if old.State != wipeverify.StateDestroyed {
		t.Fatalf("old slot copy should be destroyed by migrate receipt, got %v", old.State)
	}
	if old.ClosedBy == nil || old.ClosedBy.Kind != wipeverify.KindMigrate || old.ClosedBy.Seq != 3 {
		t.Fatalf("old slot copy should be closed by host-a migrate receipt #3, got %+v", old.ClosedBy)
	}

	target := findCopy(t, rep, "host-a", 5, 1)
	if target.State != wipeverify.StatePending {
		t.Fatalf("migration target should still be pending, got %v", target.State)
	}

	dr := findCopy(t, rep, "host-b", 1, 2)
	if dr.State != wipeverify.StateDestroyed {
		t.Fatalf("DR copy should be destroyed, got %v", dr.State)
	}

	if _, err := v.Certificate(); err == nil {
		t.Fatal("certificate must be refused while a copy is pending")
	}

	// The last valid receipt arrives.
	mustAdd(t, v, all[5])
	rep = v.Report()
	if !rep.Complete || rep.Pending != 0 {
		t.Fatalf("report should be complete after final receipt: %+v", rep)
	}
	cert, err := v.Certificate()
	if err != nil {
		t.Fatalf("Certificate: %v", err)
	}
	if cert.Volume != testVolume || len(cert.Copies) != 3 {
		t.Fatalf("unexpected certificate: %+v", cert)
	}
	var zero [32]byte
	if cert.Digest == zero {
		t.Fatal("certificate digest must not be zero")
	}
}

// TestMigrationReceiptsReverseOrder: migration establishes the new copy
// first and destroys the old one second, but the two receipts may be
// delivered in reverse order. The verifier must not depend on arrival
// order.
func TestMigrationReceiptsReverseOrder(t *testing.T) {
	v := wipeverify.NewVerifier(demoManifest())
	// Reverse arrival: migrate-away of the old slot arrives BEFORE the
	// install of the new slot, and the destroy of the DR copy arrives
	// before its own install receipt.
	mustAdd(t, v,
		wipeverify.Receipt{Device: "host-a", Seq: 3, Kind: wipeverify.KindMigrate, Volume: testVolume, Slot: 3, Generation: 7},
		wipeverify.Receipt{Device: "host-b", Seq: 2, Kind: wipeverify.KindDestroy, Volume: testVolume, Slot: 1, Generation: 2},
		wipeverify.Receipt{Device: "host-a", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 3, Generation: 7},
		wipeverify.Receipt{Device: "host-a", Seq: 2, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 5, Generation: 1},
		wipeverify.Receipt{Device: "host-b", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 1, Generation: 2},
	)
	rep := v.Report()
	if got := findCopy(t, rep, "host-a", 3, 7).State; got != wipeverify.StateDestroyed {
		t.Fatalf("old slot copy should be destroyed regardless of arrival order, got %v", got)
	}
	if got := findCopy(t, rep, "host-b", 1, 2).State; got != wipeverify.StateDestroyed {
		t.Fatalf("DR copy should be destroyed regardless of arrival order, got %v", got)
	}
	if got := findCopy(t, rep, "host-a", 5, 1).State; got != wipeverify.StatePending {
		t.Fatalf("migration target should be pending, got %v", got)
	}
}

// TestSequenceGapKeepsPending: a missing intermediate sequence number on a
// device makes every later receipt of that device unusable, so the copy
// stays pending even though its destroy receipt has physically arrived.
func TestSequenceGapKeepsPending(t *testing.T) {
	v := wipeverify.NewVerifier(demoManifest())
	mustAdd(t, v,
		wipeverify.Receipt{Device: "host-a", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 3, Generation: 7},
		wipeverify.Receipt{Device: "host-a", Seq: 2, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 5, Generation: 1},
		// host-a #3 (migrate of the old slot) is missing.
		wipeverify.Receipt{Device: "host-a", Seq: 4, Kind: wipeverify.KindDestroy, Volume: testVolume, Slot: 5, Generation: 1},
		wipeverify.Receipt{Device: "host-b", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 1, Generation: 2},
		wipeverify.Receipt{Device: "host-b", Seq: 2, Kind: wipeverify.KindDestroy, Volume: testVolume, Slot: 1, Generation: 2},
	)
	rep := v.Report()
	if rep.Complete {
		t.Fatal("report must not be complete while host-a chain has a gap")
	}
	target := findCopy(t, rep, "host-a", 5, 1)
	if target.State != wipeverify.StatePending {
		t.Fatalf("copy behind a sequence gap must stay pending, got %v", target.State)
	}
	if !strings.Contains(target.Note, "sequence chain is incomplete") {
		t.Fatalf("pending note should mention the broken sequence chain, got %q", target.Note)
	}
	// The old slot's destroy receipt is the missing one, so that copy is
	// pending too.
	if got := findCopy(t, rep, "host-a", 3, 7).State; got != wipeverify.StatePending {
		t.Fatalf("old slot copy should be pending while its migrate receipt is missing, got %v", got)
	}
	if _, err := v.Certificate(); err == nil {
		t.Fatal("certificate must be refused while the chain has a gap")
	}

	// The missing intermediate receipt arrives later; the chain becomes
	// contiguous and everything behind it turns usable.
	mustAdd(t, v, wipeverify.Receipt{Device: "host-a", Seq: 3, Kind: wipeverify.KindMigrate, Volume: testVolume, Slot: 3, Generation: 7})
	rep = v.Report()
	if !rep.Complete {
		t.Fatalf("report should be complete once the gap is filled: %+v", rep)
	}
	if _, err := v.Certificate(); err != nil {
		t.Fatalf("Certificate after gap refill: %v", err)
	}
}

// TestSlotReuseRequiresOriginalGeneration: after the slot is reused for
// another volume at a newer generation, a destroy receipt for that newer
// generation must NOT close the decommissioned copy; only the original
// generation's destroy receipt can.
func TestSlotReuseRequiresOriginalGeneration(t *testing.T) {
	manifest := wipeverify.Manifest{
		Volume: testVolume,
		Copies: []wipeverify.CopyRef{{Device: "host-a", Slot: 5, Generation: 1}},
	}
	v := wipeverify.NewVerifier(manifest)
	mustAdd(t, v,
		wipeverify.Receipt{Device: "host-a", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 5, Generation: 1},
		// Slot reused for another volume at generation 2, then that
		// generation is destroyed.
		wipeverify.Receipt{Device: "host-a", Seq: 2, Kind: wipeverify.KindInstall, Volume: "vol-other", Slot: 5, Generation: 2},
		wipeverify.Receipt{Device: "host-a", Seq: 3, Kind: wipeverify.KindDestroy, Volume: "vol-other", Slot: 5, Generation: 2},
	)
	rep := v.Report()
	cs := findCopy(t, rep, "host-a", 5, 1)
	if cs.State != wipeverify.StatePending {
		t.Fatalf("destroy of the reused generation must not close the copy, got %v", cs.State)
	}
	if !strings.Contains(cs.Note, "original generation") {
		t.Fatalf("pending note should demand the original generation, got %q", cs.Note)
	}
	if rep.Complete {
		t.Fatal("report must not be complete")
	}

	// The original generation's destroy credential arrives late.
	mustAdd(t, v, wipeverify.Receipt{Device: "host-a", Seq: 4, Kind: wipeverify.KindDestroy, Volume: testVolume, Slot: 5, Generation: 1})
	rep = v.Report()
	if !rep.Complete {
		t.Fatalf("original-generation destroy receipt should close the copy: %+v", rep)
	}
	if _, err := v.Certificate(); err != nil {
		t.Fatalf("Certificate: %v", err)
	}
}

// TestConflictingReceiptPoisonsChain: two different receipts with the same
// (device, seq) break the chain at that point.
func TestConflictingReceiptPoisonsChain(t *testing.T) {
	manifest := wipeverify.Manifest{
		Volume: testVolume,
		Copies: []wipeverify.CopyRef{{Device: "host-a", Slot: 5, Generation: 1}},
	}
	v := wipeverify.NewVerifier(manifest)
	mustAdd(t, v,
		wipeverify.Receipt{Device: "host-a", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 5, Generation: 1},
		wipeverify.Receipt{Device: "host-a", Seq: 2, Kind: wipeverify.KindDestroy, Volume: testVolume, Slot: 5, Generation: 1},
	)
	// Conflicting second receipt for host-a #2.
	mustAdd(t, v, wipeverify.Receipt{Device: "host-a", Seq: 2, Kind: wipeverify.KindInstall, Volume: "vol-other", Slot: 9, Generation: 1})
	rep := v.Report()
	cs := findCopy(t, rep, "host-a", 5, 1)
	if cs.State != wipeverify.StatePending {
		t.Fatalf("conflicting sequence number must keep the copy pending, got %v", cs.State)
	}
	if !strings.Contains(cs.Note, "sequence chain is incomplete") {
		t.Fatalf("pending note should mention the broken chain, got %q", cs.Note)
	}
}

// TestDestroyMustMatchVolumeAndGeneration: destroy receipts for a
// different volume or a different generation never close a copy.
func TestDestroyMustMatchVolumeAndGeneration(t *testing.T) {
	manifest := wipeverify.Manifest{
		Volume: testVolume,
		Copies: []wipeverify.CopyRef{{Device: "host-a", Slot: 5, Generation: 1}},
	}
	v := wipeverify.NewVerifier(manifest)
	mustAdd(t, v,
		wipeverify.Receipt{Device: "host-a", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 5, Generation: 1},
		// Same slot+generation but a different volume tag.
		wipeverify.Receipt{Device: "host-a", Seq: 2, Kind: wipeverify.KindDestroy, Volume: "vol-other", Slot: 5, Generation: 1},
		// Same volume but a different generation.
		wipeverify.Receipt{Device: "host-a", Seq: 3, Kind: wipeverify.KindDestroy, Volume: testVolume, Slot: 5, Generation: 2},
	)
	rep := v.Report()
	if got := findCopy(t, rep, "host-a", 5, 1).State; got != wipeverify.StatePending {
		t.Fatalf("mismatched destroy receipts must not close the copy, got %v", got)
	}
}

// TestReceiptValidation covers AddReceipt input validation and idempotent
// redelivery.
func TestReceiptValidation(t *testing.T) {
	v := wipeverify.NewVerifier(demoManifest())
	cases := []struct {
		name string
		r    wipeverify.Receipt
	}{
		{"empty device", wipeverify.Receipt{Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume}},
		{"zero seq", wipeverify.Receipt{Device: "host-a", Kind: wipeverify.KindInstall, Volume: testVolume}},
		{"empty volume", wipeverify.Receipt{Device: "host-a", Seq: 1, Kind: wipeverify.KindInstall}},
		{"negative slot", wipeverify.Receipt{Device: "host-a", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: -1}},
		{"unknown kind", wipeverify.Receipt{Device: "host-a", Seq: 1, Kind: wipeverify.Kind(42), Volume: testVolume}},
	}
	for _, tc := range cases {
		if err := v.AddReceipt(tc.r); err == nil {
			t.Errorf("%s: expected error, got nil", tc.name)
		}
	}

	// Identical redelivery is a no-op and must not poison the chain.
	r := wipeverify.Receipt{Device: "host-a", Seq: 1, Kind: wipeverify.KindInstall, Volume: testVolume, Slot: 3, Generation: 7}
	mustAdd(t, v, r, r, r)
	rep := v.Report()
	if got := findCopy(t, rep, "host-a", 3, 7).State; got != wipeverify.StatePending {
		t.Fatalf("duplicate redelivery broke the chain, got %v", got)
	}
}

// TestDeterministicAcrossArrivalOrders: the report and the certificate
// digest depend only on the receipt set, not on arrival order.
func TestDeterministicAcrossArrivalOrders(t *testing.T) {
	receipts := demoReceipts()
	reversed := make([]wipeverify.Receipt, len(receipts))
	for i, r := range receipts {
		reversed[len(receipts)-1-i] = r
	}

	v1 := wipeverify.NewVerifier(demoManifest())
	mustAdd(t, v1, receipts...)
	v2 := wipeverify.NewVerifier(demoManifest())
	mustAdd(t, v2, reversed...)

	r1, r2 := v1.Report(), v2.Report()
	if !reflect.DeepEqual(r1, r2) {
		t.Fatalf("reports differ across arrival orders:\n%+v\n%+v", r1, r2)
	}
	c1, err := v1.Certificate()
	if err != nil {
		t.Fatalf("Certificate v1: %v", err)
	}
	c2, err := v2.Certificate()
	if err != nil {
		t.Fatalf("Certificate v2: %v", err)
	}
	if c1.Digest != c2.Digest {
		t.Fatal("certificate digests differ across arrival orders")
	}
}

// TestManifestOnlyCopyPending: a manifest copy with no receipts at all is
// pending.
func TestManifestOnlyCopyPending(t *testing.T) {
	v := wipeverify.NewVerifier(demoManifest())
	rep := v.Report()
	if rep.Complete || rep.Pending != 2 {
		t.Fatalf("want 2 pending copies and incomplete report, got %+v", rep)
	}
	for _, c := range rep.Copies {
		if c.State != wipeverify.StatePending || !strings.Contains(c.Note, "no destroy receipt") {
			t.Fatalf("unexpected copy status: %+v", c)
		}
	}
}
