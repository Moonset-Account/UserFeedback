package sourceselect

import (
	"testing"
)

func TestDemoEventReplayDistinguishesEgressChangeAndPreferredExpiry(t *testing.T) {
	report, err := Replay(DemoEvents())
	if err != nil {
		t.Fatalf("replay demo events: %v", err)
	}

	step := func(label string) EventResult {
		t.Helper()
		for _, st := range report.Steps {
			if st.Label == label {
				return st
			}
		}
		t.Fatalf("missing replay step %q", label)
		return EventResult{}
	}
	stepConnection := func(st EventResult, id string) Connection {
		t.Helper()
		for _, conn := range st.Connections {
			if conn.ID == id {
				return conn
			}
		}
		t.Fatalf("missing connection %s in step %s", id, st.Label)
		return Connection{}
	}

	// Initial flow follows the default route to wan.
	openEgress := step("open-egress-conn")
	if openEgress.Connection.InterfaceID != "wan" {
		t.Fatalf("initial egress interface = %q, want wan", openEgress.Connection.InterfaceID)
	}
	assertIP(t, openEgress.Connection.Source, "203.0.113.10")

	// Adding the internal route changes the best egress for a new flow, but
	// does not invalidate the retained wan source.
	routeChange := step("egress-route-change")
	if len(routeChange.Rebuilds) != 0 || len(routeChange.NewRebuilds) != 0 {
		t.Fatalf("route egress change should not request rebuild: all=%+v new=%+v",
			routeChange.Rebuilds, routeChange.NewRebuilds)
	}
	var egressDrift *EgressDrift
	for i := range routeChange.EgressDrift {
		if routeChange.EgressDrift[i].ConnectionID == "C-EGRESS" {
			egressDrift = &routeChange.EgressDrift[i]
		}
	}
	if egressDrift == nil || !egressDrift.InterfaceChanged || egressDrift.BestInterfaceID != "lan" {
		t.Fatalf("route-change drift = %+v, expected C-EGRESS drift to lan", routeChange.EgressDrift)
	}
	conn := stepConnection(routeChange, "C-EGRESS")
	if conn.InterfaceID != "wan" {
		t.Fatalf("existing conn mutated on route change to %s", conn.InterfaceID)
	}

	// The internal flow initially takes .2 while it is preferred and wins the
	// numeric tie over .3.
	openLifetime := step("open-lifetime-conn")
	assertIP(t, openLifetime.Connection.Source, "10.0.0.2")

	// Passing only PreferredUntil deprecates .2. It must not rebuild the
	// established connection, although selection now prefers .3.
	preferredOnly := step("preferred-lifetime-only")
	if len(preferredOnly.Rebuilds) != 0 || len(preferredOnly.NewRebuilds) != 0 {
		t.Fatalf("preferred-lifetime expiry should not request rebuild: %+v", preferredOnly.Rebuilds)
	}
	conn = stepConnection(preferredOnly, "C-LIFETIME")
	if conn.State != ConnectionEstablished {
		t.Fatalf("state after preferred-only expiry = %s", conn.State)
	}
	assertIP(t, conn.Source, "10.0.0.2")

	var lifetimeDrift *EgressDrift
	for i := range preferredOnly.EgressDrift {
		if preferredOnly.EgressDrift[i].ConnectionID == "C-LIFETIME" {
			lifetimeDrift = &preferredOnly.EgressDrift[i]
		}
	}
	if lifetimeDrift == nil || !lifetimeDrift.SourceChanged {
		t.Fatalf("preferred expiry drift = %+v, expected C-LIFETIME source drift", preferredOnly.EgressDrift)
	}
	assertIP(t, lifetimeDrift.BestSource, "10.0.0.3")

	afterPreferred := step("open-after-preferred")
	if !afterPreferred.Selection.Preferred {
		t.Fatal("new connection should choose a still-preferred address")
	}
	assertIP(t, afterPreferred.Connection.Source, "10.0.0.3")

	// Reaching ValidUntil is the first lifetime condition that rebuilds.
	validExpiry := step("source-valid-expiry")
	rec, ok := findRecommendation(validExpiry.NewRebuilds, "C-LIFETIME")
	if !ok || rec.Reason != RebuildSourceExpired {
		t.Fatalf("valid expiry new rebuilds = %+v", validExpiry.NewRebuilds)
	}
	rebuiltLifetime := step("rebuild-lifetime-conn")
	assertIP(t, rebuiltLifetime.Connection.Source, "10.0.0.3")

	// Actual original-interface departure also forces rebuild.
	departed := step("egress-interface-departed")
	rec, ok = findRecommendation(departed.NewRebuilds, "C-EGRESS")
	if !ok || rec.Reason != RebuildInterfaceDeparted {
		t.Fatalf("interface departure new rebuilds = %+v", departed.NewRebuilds)
	}
	rebuiltEgress := step("rebuild-egress-conn")
	if rebuiltEgress.Connection.InterfaceID != "lan" {
		t.Fatalf("rebuilt egress iface = %s, want lan", rebuiltEgress.Connection.InterfaceID)
	}
	assertIP(t, rebuiltEgress.Connection.Source, "10.0.0.3")

	final := step("final-evaluate")
	if len(final.Rebuilds) != 0 {
		t.Fatalf("final replay should have no pending rebuilds: %+v", final.Rebuilds)
	}
	if len(report.Rebuilds) != 0 {
		t.Fatalf("report rebuilds = %+v, want empty", report.Rebuilds)
	}
}
