package pathmtu_test

import (
	"fmt"
	"time"

	"example.com/pathmtu"
)

// ExampleDiscoverer walks a small path-MTU timeline and prints, for every
// event, the ordinary send size, the next probe and why the bounds moved. The
// scenario exercises growth, a size limit and re-verification.
func ExampleDiscoverer() {
	fake := time.Date(2026, 11, 23, 12, 0, 0, 0, time.UTC)
	now := func() time.Time { return fake }

	d, err := pathmtu.New(pathmtu.Config{
		MinPayload:  1200,
		MaxPayload:  1500,
		EvidenceTTL: 30 * time.Second,
		Now:         now,
	})
	if err != nil {
		panic(err)
	}

	step := func(label string, s pathmtu.Snapshot) {
		fmt.Printf("%-14s %s\n", label, s)
	}

	step("migrate", d.MigrateTo(1, 1200).Snapshot)

	p, _ := d.SendProbe()
	step("probe ok", d.OnProbeSuccess(p.ID).Snapshot)

	p, _ = d.SendProbe()
	step("too big", d.OnProbeTooBig(p.ID, 1400).Snapshot)

	p, _ = d.SendProbe()
	step("probe ok", d.OnProbeSuccess(p.ID).Snapshot)

	p, _ = d.SendProbe()
	step("too big", d.OnProbeTooBig(p.ID, 1360).Snapshot)

	p, _ = d.SendProbe() // confirmation of the new limit
	step("confirmed", d.OnProbeSuccess(p.ID).Snapshot)

	fake = fake.Add(10 * time.Second)
	step("migrate", d.MigrateTo(2, 1200).Snapshot)
	step("back", d.MigrateTo(1, 1200).Snapshot)

	// Output:
	// migrate        gen=1 ordinary=1200 nextProbe=1350 floor=1200 verified=1200 ceiling=1500 kind=migrated_fresh | new generation 1: fresh bounds at floor 1200, ceiling 1500
	// probe ok       gen=1 ordinary=1350 nextProbe=1425 floor=1200 verified=1350 ceiling=1500 kind=verified_raised | probe 1 (1350 bytes) succeeded: verified lower bound raised to 1350
	// too big        gen=1 ordinary=1350 nextProbe=1375 floor=1200 verified=1350 ceiling=1400 kind=ceiling_lowered | probe 2 too big: ceiling lowered 1500 -> 1400 by reported limit 1400
	// probe ok       gen=1 ordinary=1375 nextProbe=1387 floor=1200 verified=1375 ceiling=1400 kind=verified_raised | probe 3 (1375 bytes) succeeded: verified lower bound raised to 1375
	// too big        gen=1 ordinary=1360 nextProbe=1360 floor=1200 verified=1360 ceiling=1360 revalidating kind=revalidating | probe 4 too big: limit 1360 is below verified 1375, immediately dropped to 1360 and scheduling re-verification
	// confirmed      gen=1 ordinary=1360 nextProbe=none floor=1200 verified=1360 ceiling=1360 kind=revalidated | probe 5 (1360 bytes) succeeded: limit 1360 re-verified, ordinary send size confirmed
	// migrate        gen=2 ordinary=1200 nextProbe=1350 floor=1200 verified=1200 ceiling=1500 kind=migrated_fresh | new generation 2: fresh bounds at floor 1200, ceiling 1500
	// back           gen=1 ordinary=1360 nextProbe=none floor=1200 verified=1360 ceiling=1360 kind=migrated_reuse | reused unexpired evidence for generation 1 (age 10s <= ttl 30s): floor=1200 verified=1360 ceiling=1360
}
