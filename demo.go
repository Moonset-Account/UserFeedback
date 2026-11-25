package sourceselect

import (
	"net"
	"time"
)

// DemoBaseTime is the fixed clock used by DemoEvents.
var DemoBaseTime = time.Date(2026, 11, 26, 0, 0, 0, 0, time.UTC)

// DemoEvents returns the synthetic internal/external replay used by tests and
// cmd/replay.
//
// The important contrast is:
//
//   - at "egress-route-change", selection for a new flow moves from wan to lan,
//     but the established connection is not rebuilt because wan remains;
//   - at "preferred-lifetime-only", the old address is deprecated but still
//     valid, so the established connection is again not rebuilt;
//   - valid-lifetime expiry and physical interface departure do force rebuild.
func DemoEvents() []Event {
	t0 := DemoBaseTime
	min := time.Minute
	parseIP := func(s string) net.IP { return mustParseIP(s) }
	parseCIDR := func(s string) *net.IPNet {
		n, err := ParseCIDR(s)
		if err != nil {
			panic(err)
		}
		return n
	}

	return []Event{
		{
			Time: t0, Kind: EventUpsertInterface, Label: "setup-lan",
			Interface: Interface{ID: "lan", State: InterfaceUp}, InterfaceID: "lan",
		},
		{
			Time: t0, Kind: EventUpsertInterface, Label: "setup-wan",
			Interface: Interface{ID: "wan", State: InterfaceUp}, InterfaceID: "wan",
		},
		{
			Time: t0, Kind: EventUpsertAddress, Label: "lan-address-2", InterfaceID: "lan",
			Address: Address{
				IP: parseIP("10.0.0.2"), PrefixLength: 24, PolicyLevel: 10,
				PreferredUntil: t0.Add(10 * min), ValidUntil: t0.Add(60 * min),
			},
		},
		{
			Time: t0, Kind: EventUpsertAddress, Label: "wan-address-10", InterfaceID: "wan",
			Address: Address{
				IP: parseIP("203.0.113.10"), PrefixLength: 24, PolicyLevel: 10,
				PreferredUntil: t0.Add(10 * min), ValidUntil: t0.Add(180 * min),
			},
		},
		{
			Time: t0, Kind: EventUpsertRoute, Label: "default-route-via-wan",
			Route: Route{
				ID: "default-v4", Destination: parseCIDR("0.0.0.0/0"),
				InterfaceID: "wan", State: RouteActive,
			},
		},
		{
			Time: t0.Add(min), Kind: EventOpenConnection, Label: "open-egress-conn",
			ConnectionID: "C-EGRESS", Destination: parseIP("10.20.0.8"),
		},
		{
			Time: t0.Add(2 * min), Kind: EventUpsertRoute, Label: "egress-route-change",
			Route: Route{
				ID: "internal-10/8", Destination: parseCIDR("10.0.0.0/8"),
				InterfaceID: "lan", State: RouteActive,
			},
		},
		{
			Time: t0.Add(3 * min), Kind: EventOpenConnection, Label: "open-lifetime-conn",
			ConnectionID: "C-LIFETIME", Destination: parseIP("10.20.0.9"),
		},
		{
			Time: t0.Add(5 * min), Kind: EventUpsertAddress, Label: "lan-address-3", InterfaceID: "lan",
			Address: Address{
				IP: parseIP("10.0.0.3"), PrefixLength: 24, PolicyLevel: 10,
				PreferredUntil: t0.Add(30 * min), ValidUntil: t0.Add(120 * min),
			},
		},
		{
			Time: t0.Add(11 * min), Kind: EventEvaluate, Label: "preferred-lifetime-only",
		},
		{
			Time: t0.Add(12 * min), Kind: EventOpenConnection, Label: "open-after-preferred",
			ConnectionID: "C-AFTER-PREFERRED", Destination: parseIP("10.20.0.10"),
		},
		{
			Time: t0.Add(60 * min), Kind: EventEvaluate, Label: "source-valid-expiry",
		},
		{
			Time: t0.Add(61 * min), Kind: EventRebuildConnection, Label: "rebuild-lifetime-conn",
			ConnectionID: "C-LIFETIME",
		},
		{
			Time: t0.Add(70 * min), Kind: EventRemoveInterface, Label: "egress-interface-departed",
			InterfaceID: "wan",
		},
		{
			Time: t0.Add(71 * min), Kind: EventRebuildConnection, Label: "rebuild-egress-conn",
			ConnectionID: "C-EGRESS",
		},
		{
			Time: t0.Add(72 * min), Kind: EventEvaluate, Label: "final-evaluate",
		},
	}
}

func mustParseIP(s string) net.IP {
	ip := net.ParseIP(s)
	if ip == nil {
		panic("invalid demo IP: " + s)
	}
	return ip
}
