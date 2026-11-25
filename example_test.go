package sourceselect_test

import (
	"fmt"

	sourceselect "sourceselect"
)

// ExampleReplay runs the synthetic internal/external event trace and prints
// the distinction between route egress change, preferred-lifetime deprecation,
// valid-lifetime expiry and interface departure.
func ExampleReplay() {
	report, err := sourceselect.Replay(sourceselect.DemoEvents())
	if err != nil {
		panic(err)
	}

	step := func(label string) sourceselect.EventResult {
		for _, st := range report.Steps {
			if st.Label == label {
				return st
			}
		}
		panic("missing step " + label)
	}
	conn := func(label, id string) sourceselect.Connection {
		for _, c := range step(label).Connections {
			if c.ID == id {
				return c
			}
		}
		panic("missing connection " + id)
	}

	route := step("egress-route-change")
	fmt.Printf("egress-route-change: new-rebuilds=%d", len(route.NewRebuilds))
	for _, d := range route.EgressDrift {
		if d.ConnectionID == "C-EGRESS" {
			fmt.Printf(" C-EGRESS %s/%s -> %s/%s\n",
				d.CurrentInterfaceID, d.CurrentSource, d.BestInterfaceID, d.BestSource)
		}
	}

	preferred := step("preferred-lifetime-only")
	c := conn("preferred-lifetime-only", "C-LIFETIME")
	fmt.Printf("preferred-lifetime-only: new-rebuilds=%d retained=C-LIFETIME=%s/%s",
		len(preferred.NewRebuilds), c.InterfaceID, c.Source)
	for _, d := range preferred.EgressDrift {
		if d.ConnectionID == "C-LIFETIME" {
			fmt.Printf(" best-now=%s/%s\n", d.BestInterfaceID, d.BestSource)
		}
	}

	after := step("open-after-preferred")
	fmt.Printf("open-after-preferred: C-AFTER-PREFERRED=%s/%s preferred=%v\n",
		after.Connection.InterfaceID, after.Connection.Source, after.Selection.Preferred)

	expiry := step("source-valid-expiry")
	for _, rec := range expiry.NewRebuilds {
		fmt.Printf("source-valid-expiry: new-rebuild=%s/%s\n", rec.ConnectionID, rec.Reason)
	}

	departed := step("egress-interface-departed")
	for _, rec := range departed.NewRebuilds {
		fmt.Printf("egress-interface-departed: new-rebuild=%s/%s\n", rec.ConnectionID, rec.Reason)
	}

	final := step("final-evaluate")
	fmt.Printf("final: pending=%d", len(final.Rebuilds))
	for _, id := range []string{"C-EGRESS", "C-LIFETIME", "C-AFTER-PREFERRED"} {
		c := conn("final-evaluate", id)
		fmt.Printf(" %s=%s/%s", id, c.InterfaceID, c.Source)
	}
	fmt.Println()

	// Output:
	// egress-route-change: new-rebuilds=0 C-EGRESS wan/203.0.113.10 -> lan/10.0.0.2
	// preferred-lifetime-only: new-rebuilds=0 retained=C-LIFETIME=lan/10.0.0.2 best-now=lan/10.0.0.3
	// open-after-preferred: C-AFTER-PREFERRED=lan/10.0.0.3 preferred=true
	// source-valid-expiry: new-rebuild=C-LIFETIME/source-valid-lifetime-expired
	// egress-interface-departed: new-rebuild=C-EGRESS/interface-departed
	// final: pending=0 C-EGRESS=lan/10.0.0.3 C-LIFETIME=lan/10.0.0.3 C-AFTER-PREFERRED=lan/10.0.0.3
}
