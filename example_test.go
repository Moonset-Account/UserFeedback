package wipeverify_test

import (
	"fmt"

	"wipeverify"
)

// Small synthetic record demonstrating the decommission flow: the old slot
// is cleared by a migration while the migration target is still alive, so
// the volume stays pending; only after the last valid receipt arrives can
// the erasure certificate be issued.
func ExampleVerifier() {
	manifest := wipeverify.Manifest{
		Volume: "vol-7f3a",
		Copies: []wipeverify.CopyRef{
			{Device: "host-a", Slot: 5, Generation: 1}, // migration target
			{Device: "host-b", Slot: 1, Generation: 2}, // DR copy
		},
	}
	v := wipeverify.NewVerifier(manifest)

	// Receipts whose device signatures were already verified. Note that
	// the migration's two receipts arrive in reverse order: the migrate
	// receipt for the old slot (host-a #3) shows up before the install
	// receipt of the new slot (host-a #2).
	receipts := []wipeverify.Receipt{
		{Device: "host-a", Seq: 1, Kind: wipeverify.KindInstall, Volume: "vol-7f3a", Slot: 3, Generation: 7},
		{Device: "host-b", Seq: 1, Kind: wipeverify.KindInstall, Volume: "vol-7f3a", Slot: 1, Generation: 2},
		{Device: "host-a", Seq: 3, Kind: wipeverify.KindMigrate, Volume: "vol-7f3a", Slot: 3, Generation: 7},
		{Device: "host-a", Seq: 2, Kind: wipeverify.KindInstall, Volume: "vol-7f3a", Slot: 5, Generation: 1},
		{Device: "host-b", Seq: 2, Kind: wipeverify.KindDestroy, Volume: "vol-7f3a", Slot: 1, Generation: 2},
	}
	for _, r := range receipts {
		if err := v.AddReceipt(r); err != nil {
			panic(err)
		}
	}

	rep := v.Report()
	fmt.Println("complete:", rep.Complete)
	for _, c := range rep.Copies {
		line := fmt.Sprintf("%s slot %d gen %d -> %s", c.Copy.Device, c.Copy.Slot, c.Copy.Generation, c.State)
		if c.Note != "" {
			line += " (" + c.Note + ")"
		}
		fmt.Println(line)
	}
	if _, err := v.Certificate(); err != nil {
		fmt.Println("certificate:", err)
	}

	// The last valid receipt arrives: destroy of the original generation
	// on the migration target slot.
	if err := v.AddReceipt(wipeverify.Receipt{
		Device: "host-a", Seq: 4, Kind: wipeverify.KindDestroy,
		Volume: "vol-7f3a", Slot: 5, Generation: 1,
	}); err != nil {
		panic(err)
	}
	rep = v.Report()
	fmt.Println("complete:", rep.Complete)
	cert, err := v.Certificate()
	if err != nil {
		panic(err)
	}
	fmt.Println("certificate issued for", cert.Volume, "covering", len(cert.Copies), "copies")

	// Output:
	// complete: false
	// host-a slot 3 gen 7 -> destroyed
	// host-a slot 5 gen 1 -> pending (no destroy receipt for the original generation yet)
	// host-b slot 1 gen 2 -> destroyed
	// certificate: wipeverify: cannot issue erasure certificate for volume "vol-7f3a": 1 of 3 copies still pending
	// complete: true
	// certificate issued for vol-7f3a covering 3 copies
}
