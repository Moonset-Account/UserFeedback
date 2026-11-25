package sourceselect

import (
	"net"
	"testing"
	"time"
)

func testTime() time.Time {
	return time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
}

func mustIP(t *testing.T, s string) net.IP {
	t.Helper()
	ip := net.ParseIP(s)
	if ip == nil {
		t.Fatalf("parse IP %q", s)
	}
	return ip
}

func mustCIDR(t *testing.T, s string) *net.IPNet {
	t.Helper()
	n, err := ParseCIDR(s)
	if err != nil {
		t.Fatalf("parse CIDR %q: %v", s, err)
	}
	return n
}

func newDualHostSelector(t *testing.T, now time.Time) *Selector {
	t.Helper()
	s := NewSelector()

	mustUpsertInterface(t, s, Interface{ID: "lan", State: InterfaceUp})
	mustUpsertInterface(t, s, Interface{ID: "wan", State: InterfaceUp})

	mustUpsertAddress(t, s, "lan", Address{
		IP:             mustIP(t, "10.0.0.2"),
		PrefixLength:   24,
		PolicyLevel:    10,
		PreferredUntil: now.Add(time.Hour),
		ValidUntil:     now.Add(24 * time.Hour),
	})
	mustUpsertAddress(t, s, "wan", Address{
		IP:             mustIP(t, "203.0.113.10"),
		PrefixLength:   24,
		PolicyLevel:    10,
		PreferredUntil: now.Add(time.Hour),
		ValidUntil:     now.Add(24 * time.Hour),
	})

	mustUpsertRoute(t, s, Route{
		ID:          "lan-10/8",
		Destination: mustCIDR(t, "10.0.0.0/8"),
		InterfaceID: "lan",
		State:       RouteActive,
	})
	mustUpsertRoute(t, s, Route{
		ID:          "default-v4",
		Destination: mustCIDR(t, "0.0.0.0/0"),
		InterfaceID: "wan",
		State:       RouteActive,
	})
	return s
}

func mustUpsertInterface(t *testing.T, s *Selector, iface Interface) {
	t.Helper()
	if err := s.UpsertInterface(iface); err != nil {
		t.Fatalf("UpsertInterface(%+v): %v", iface, err)
	}
}

func mustUpsertAddress(t *testing.T, s *Selector, id string, addr Address) {
	t.Helper()
	if err := s.UpsertAddress(id, addr); err != nil {
		t.Fatalf("UpsertAddress(%s, %s): %v", id, addr.IP, err)
	}
}

func mustUpsertRoute(t *testing.T, s *Selector, route Route) {
	t.Helper()
	if err := s.UpsertRoute(route); err != nil {
		t.Fatalf("UpsertRoute(%s): %v", route.ID, err)
	}
}

func assertIP(t *testing.T, got net.IP, want string) {
	t.Helper()
	wantIP := mustIP(t, want)
	if compareIP(got, wantIP) != 0 {
		t.Fatalf("IP = %v, want %v", got, wantIP)
	}
}

func findRecommendation(recs []RebuildRecommendation, connID string) (RebuildRecommendation, bool) {
	for _, rec := range recs {
		if rec.ConnectionID == connID {
			return rec, true
		}
	}
	return RebuildRecommendation{}, false
}

func recommendationIDs(recs []RebuildRecommendation) []string {
	out := make([]string, 0, len(recs))
	for _, rec := range recs {
		out = append(out, rec.ConnectionID)
	}
	return out
}
