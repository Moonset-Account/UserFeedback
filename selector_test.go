package sourceselect

import (
	"errors"
	"testing"
	"time"
)

func TestSelectDualHomestedByRoute(t *testing.T) {
	now := testTime()
	s := newDualHostSelector(t, now)

	lan, err := s.SelectSource(mustIP(t, "10.20.30.40"), now)
	if err != nil {
		t.Fatal(err)
	}
	if lan.InterfaceID != "lan" {
		t.Fatalf("internal destination interface = %q, want lan", lan.InterfaceID)
	}
	assertIP(t, lan.Source, "10.0.0.2")
	if lan.RouteID != "lan-10/8" {
		t.Fatalf("route = %q, want lan-10/8", lan.RouteID)
	}
	if !lan.Preferred {
		t.Fatal("address should be preferred")
	}

	wan, err := s.SelectSource(mustIP(t, "198.51.100.8"), now)
	if err != nil {
		t.Fatal(err)
	}
	if wan.InterfaceID != "wan" {
		t.Fatalf("external destination interface = %q, want wan", wan.InterfaceID)
	}
	assertIP(t, wan.Source, "203.0.113.10")
	if wan.RouteID != "default-v4" {
		t.Fatalf("route = %q, want default-v4", wan.RouteID)
	}
}

func TestRouteLongestPrefixStateAndMetric(t *testing.T) {
	now := testTime()
	s := NewSelector()
	for _, id := range []string{"def", "one0", "two0", "m-low", "m-high", "tie-a", "tie-b"} {
		mustUpsertInterface(t, s, Interface{ID: id, State: InterfaceUp})
	}
	mustUpsertAddress(t, s, "def", Address{IP: mustIP(t, "198.51.100.1"), PrefixLength: 24})
	mustUpsertAddress(t, s, "one0", Address{IP: mustIP(t, "10.0.0.2"), PrefixLength: 8})
	mustUpsertAddress(t, s, "two0", Address{IP: mustIP(t, "10.20.0.2"), PrefixLength: 16})
	mustUpsertAddress(t, s, "m-low", Address{IP: mustIP(t, "192.0.2.2"), PrefixLength: 24})
	mustUpsertAddress(t, s, "m-high", Address{IP: mustIP(t, "192.0.2.3"), PrefixLength: 24})
	mustUpsertAddress(t, s, "tie-a", Address{IP: mustIP(t, "192.0.2.4"), PrefixLength: 24})
	mustUpsertAddress(t, s, "tie-b", Address{IP: mustIP(t, "192.0.2.5"), PrefixLength: 24})

	mustUpsertRoute(t, s, Route{ID: "default", Destination: mustCIDR(t, "0.0.0.0/0"), InterfaceID: "def"})
	mustUpsertRoute(t, s, Route{ID: "ten", Destination: mustCIDR(t, "10.0.0.0/8"), InterfaceID: "one0"})
	mustUpsertRoute(t, s, Route{ID: "twenty", Destination: mustCIDR(t, "10.20.0.0/16"), InterfaceID: "two0"})
	mustUpsertRoute(t, s, Route{ID: "metric-high", Destination: mustCIDR(t, "192.0.2.0/24"), InterfaceID: "m-high", Metric: 20})
	mustUpsertRoute(t, s, Route{ID: "metric-low", Destination: mustCIDR(t, "192.0.2.0/24"), InterfaceID: "m-low", Metric: 5})

	sel, err := s.SelectSource(mustIP(t, "10.20.1.1"), now)
	if err != nil {
		t.Fatal(err)
	}
	if sel.InterfaceID != "two0" || sel.RouteID != "twenty" {
		t.Fatalf("longest prefix selected %s/%s", sel.RouteID, sel.InterfaceID)
	}

	if err := s.SetRouteState("twenty", RouteInactive); err != nil {
		t.Fatal(err)
	}
	sel, err = s.SelectSource(mustIP(t, "10.20.1.1"), now)
	if err != nil {
		t.Fatal(err)
	}
	if sel.InterfaceID != "one0" || sel.RouteID != "ten" {
		t.Fatalf("inactive more-specific route should fall back, got %s/%s", sel.RouteID, sel.InterfaceID)
	}

	sel, err = s.SelectSource(mustIP(t, "192.0.2.77"), now)
	if err != nil {
		t.Fatal(err)
	}
	if sel.RouteID != "metric-low" || sel.InterfaceID != "m-low" {
		t.Fatalf("metric selection = %s/%s", sel.RouteID, sel.InterfaceID)
	}

	// Equal prefix and metric use the stable route ID tie-break.
	mustUpsertRoute(t, s, Route{ID: "tie-b", Destination: mustCIDR(t, "203.0.113.0/24"), InterfaceID: "tie-b", Metric: 7})
	mustUpsertRoute(t, s, Route{ID: "tie-a", Destination: mustCIDR(t, "203.0.113.0/24"), InterfaceID: "tie-a", Metric: 7})
	sel, err = s.SelectSource(mustIP(t, "203.0.113.9"), now)
	if err != nil {
		t.Fatal(err)
	}
	if sel.RouteID != "tie-a" {
		t.Fatalf("equal metric route = %q, want tie-a", sel.RouteID)
	}
}

func TestFamilyAndScopeFiltering(t *testing.T) {
	now := testTime()
	s := NewSelector()
	mustUpsertInterface(t, s, Interface{ID: "dual"})
	mustUpsertAddress(t, s, "dual", Address{IP: mustIP(t, "2001:db8::10"), PrefixLength: 64})
	mustUpsertAddress(t, s, "dual", Address{IP: mustIP(t, "fe80::1"), PrefixLength: 64})
	mustUpsertRoute(t, s, Route{ID: "v6-default", Destination: mustCIDR(t, "::/0"), InterfaceID: "dual"})

	global, err := s.SelectSource(mustIP(t, "2001:db8::1"), now)
	if err != nil {
		t.Fatal(err)
	}
	assertIP(t, global.Source, "2001:db8::10")
	if global.EffectiveScope != ScopeGlobal {
		t.Fatalf("scope = %s, want global", global.EffectiveScope)
	}

	link, err := s.SelectSource(mustIP(t, "fe80::1234"), now)
	if err != nil {
		t.Fatal(err)
	}
	assertIP(t, link.Source, "fe80::1")
	if link.EffectiveScope != ScopeLinkLocal {
		t.Fatalf("scope = %s, want link-local", link.EffectiveScope)
	}

	// A link-only IPv4 interface cannot cover a global destination.
	mustUpsertInterface(t, s, Interface{ID: "linkv4"})
	mustUpsertAddress(t, s, "linkv4", Address{IP: mustIP(t, "169.254.0.1"), PrefixLength: 16})
	mustUpsertRoute(t, s, Route{ID: "v4-default", Destination: mustCIDR(t, "0.0.0.0/0"), InterfaceID: "linkv4"})
	_, err = s.SelectSource(mustIP(t, "198.51.100.8"), now)
	if !errors.Is(err, ErrNoUsableAddress) {
		t.Fatalf("global destination with link-local source err = %v, want ErrNoUsableAddress", err)
	}
}

func TestCandidateOrder(t *testing.T) {
	now := testTime()

	newCandidateSelector := func(t *testing.T) *Selector {
		t.Helper()
		s := NewSelector()
		mustUpsertInterface(t, s, Interface{ID: "x"})
		mustUpsertRoute(t, s, Route{ID: "r", Destination: mustCIDR(t, "0.0.0.0/0"), InterfaceID: "x"})
		return s
	}

	t.Run("preferred status precedes policy", func(t *testing.T) {
		s := newCandidateSelector(t)
		mustUpsertAddress(t, s, "x", Address{
			IP: mustIP(t, "192.0.2.1"), PolicyLevel: 100,
			PreferredUntil: now.Add(-time.Minute), ValidUntil: now.Add(time.Hour),
		})
		mustUpsertAddress(t, s, "x", Address{
			IP: mustIP(t, "192.0.2.2"), PolicyLevel: 0,
			PreferredUntil: now.Add(time.Hour), ValidUntil: now.Add(time.Hour),
		})
		sel, err := s.SelectSource(mustIP(t, "192.0.2.200"), now)
		if err != nil {
			t.Fatal(err)
		}
		assertIP(t, sel.Source, "192.0.2.2")
		if !sel.Preferred {
			t.Fatal("selected source should be preferred")
		}
	})

	t.Run("policy precedes common prefix", func(t *testing.T) {
		s := newCandidateSelector(t)
		mustUpsertAddress(t, s, "x", Address{IP: mustIP(t, "192.0.2.128"), PolicyLevel: 1})
		mustUpsertAddress(t, s, "x", Address{IP: mustIP(t, "192.0.2.1"), PolicyLevel: 10})
		sel, err := s.SelectSource(mustIP(t, "192.0.2.200"), now)
		if err != nil {
			t.Fatal(err)
		}
		assertIP(t, sel.Source, "192.0.2.1")
		if sel.Candidates[0].CommonPrefixLength >= sel.Candidates[1].CommonPrefixLength {
			t.Fatalf("test setup expected policy winner to have shorter prefix: %+v", sel.Candidates)
		}
	})

	t.Run("common prefix breaks equal policy", func(t *testing.T) {
		s := newCandidateSelector(t)
		mustUpsertAddress(t, s, "x", Address{IP: mustIP(t, "192.0.2.1"), PolicyLevel: 5})
		mustUpsertAddress(t, s, "x", Address{IP: mustIP(t, "192.0.2.128"), PolicyLevel: 5})
		sel, err := s.SelectSource(mustIP(t, "192.0.2.200"), now)
		if err != nil {
			t.Fatal(err)
		}
		assertIP(t, sel.Source, "192.0.2.128")
	})

	t.Run("equal score uses numeric address order stably", func(t *testing.T) {
		s := newCandidateSelector(t)
		// Insert in descending order to prove the result is sorted rather
		// than accidentally following insertion order.
		mustUpsertAddress(t, s, "x", Address{IP: mustIP(t, "192.0.2.9"), PolicyLevel: 5})
		mustUpsertAddress(t, s, "x", Address{IP: mustIP(t, "192.0.2.2"), PolicyLevel: 5})
		sel, err := s.SelectSource(mustIP(t, "192.0.2.200"), now)
		if err != nil {
			t.Fatal(err)
		}
		assertIP(t, sel.Source, "192.0.2.2")
		if len(sel.Candidates) != 2 {
			t.Fatalf("candidates = %d, want 2", len(sel.Candidates))
		}
		assertIP(t, sel.Candidates[0].Address.IP, "192.0.2.2")
		assertIP(t, sel.Candidates[1].Address.IP, "192.0.2.9")
	})
}

func TestLifetimeBoundaries(t *testing.T) {
	base := testTime()
	preferredUntil := base
	validUntil := base.Add(2 * time.Hour)
	addr := Address{
		IP:             mustIP(t, "192.0.2.10"),
		PrefixLength:   24,
		PreferredUntil: preferredUntil,
		ValidUntil:     validUntil,
	}

	if got := addr.LifetimeAt(base.Add(-time.Nanosecond)); got != LifetimePreferred {
		t.Fatalf("before PreferredUntil: %s", got)
	}
	if got := addr.LifetimeAt(preferredUntil); got != LifetimeDeprecated {
		t.Fatalf("at PreferredUntil: %s, want deprecated", got)
	}
	if got := addr.LifetimeAt(validUntil.Add(-time.Nanosecond)); got != LifetimeDeprecated {
		t.Fatalf("just before ValidUntil: %s, want deprecated", got)
	}
	if got := addr.LifetimeAt(validUntil); got != LifetimeInvalid {
		t.Fatalf("at ValidUntil: %s, want invalid", got)
	}

	s := NewSelector()
	mustUpsertInterface(t, s, Interface{ID: "x"})
	mustUpsertAddress(t, s, "x", addr)
	mustUpsertRoute(t, s, Route{ID: "r", Destination: mustCIDR(t, "0.0.0.0/0"), InterfaceID: "x"})

	sel, err := s.SelectSource(mustIP(t, "192.0.2.99"), preferredUntil)
	if err != nil {
		t.Fatal(err)
	}
	if sel.Preferred || sel.Lifetime != LifetimeDeprecated {
		t.Fatalf("at preferred boundary selection = %+v", sel)
	}

	_, err = s.SelectSource(mustIP(t, "192.0.2.99"), validUntil)
	if !errors.Is(err, ErrNoUsableAddress) {
		t.Fatalf("at valid boundary err = %v, want ErrNoUsableAddress", err)
	}
}

func TestConnectionRetainsDeprecatedUntilValidExpiry(t *testing.T) {
	now := testTime()
	s := newDualHostSelector(t, now)

	conn, err := s.OpenConnection("c-lan", mustIP(t, "10.1.1.1"), now)
	if err != nil {
		t.Fatal(err)
	}
	assertIP(t, conn.Source, "10.0.0.2")

	deprecatedAt := now.Add(61 * time.Minute)
	if recs := s.Evaluate(deprecatedAt); len(recs) != 0 {
		t.Fatalf("deprecated-only event should not require rebuild: %+v", recs)
	}
	got, ok := s.Connection("c-lan")
	if !ok {
		t.Fatal("connection missing")
	}
	assertIP(t, got.Source, "10.0.0.2")
	if got.State != ConnectionEstablished {
		t.Fatalf("state after deprecation = %s", got.State)
	}

	expiredAt := now.Add(24 * time.Hour)
	all, newly := s.EvaluateChanges(expiredAt)
	rec, ok := findRecommendation(all, "c-lan")
	if !ok {
		t.Fatalf("expected c-lan rebuild in %+v", all)
	}
	if rec.Reason != RebuildSourceExpired || !rec.NewlyDetected {
		t.Fatalf("recommendation = %+v", rec)
	}
	if len(newly) != 1 || newly[0].ConnectionID != "c-lan" {
		t.Fatalf("new recommendations = %+v", newly)
	}

	// Re-evaluation keeps the pending recommendation but does not call it new.
	all, newly = s.EvaluateChanges(expiredAt.Add(time.Minute))
	rec, ok = findRecommendation(all, "c-lan")
	if !ok {
		t.Fatalf("pending recommendation disappeared: %+v", all)
	}
	if rec.NewlyDetected {
		t.Fatalf("repeat recommendation should not be newly detected: %+v", rec)
	}
	if len(newly) != 0 {
		t.Fatalf("repeat evaluation produced new recommendations: %+v", newly)
	}

	// The expired sole address cannot support a rebuild.
	_, err = s.RebuildConnection("c-lan", expiredAt)
	if !errors.Is(err, ErrRebuildUnsuccessful) || !errors.Is(err, ErrNoUsableAddress) {
		t.Fatalf("rebuild expired sole address err = %v", err)
	}

	// A fresh address makes explicit rebuild succeed.
	mustUpsertAddress(t, s, "lan", Address{
		IP:             mustIP(t, "10.0.0.9"),
		PolicyLevel:    50,
		PreferredUntil: expiredAt.Add(time.Hour),
		ValidUntil:     expiredAt.Add(2 * time.Hour),
	})
	rebuilt, err := s.RebuildConnection("c-lan", expiredAt.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	assertIP(t, rebuilt.Source, "10.0.0.9")
	if rebuilt.State != ConnectionEstablished || rebuilt.RebuildReason != "" {
		t.Fatalf("rebuilt connection = %+v", rebuilt)
	}
	if recs := s.Evaluate(expiredAt.Add(time.Minute)); len(recs) != 0 {
		t.Fatalf("rebuilt connection should be healthy: %+v", recs)
	}
}

func TestRouteEgressChangeDoesNotRebuildWhileInterfaceRemains(t *testing.T) {
	now := testTime()
	s := newDualHostSelector(t, now)
	mustUpsertInterface(t, s, Interface{ID: "backup"})
	mustUpsertAddress(t, s, "backup", Address{IP: mustIP(t, "198.51.100.20"), PrefixLength: 24})
	mustUpsertRoute(t, s, Route{
		ID: "special", Destination: mustCIDR(t, "198.51.100.0/24"), InterfaceID: "wan",
	})

	conn, err := s.OpenConnection("c", mustIP(t, "198.51.100.8"), now)
	if err != nil {
		t.Fatal(err)
	}
	if conn.InterfaceID != "wan" {
		t.Fatalf("initial interface = %s", conn.InterfaceID)
	}

	// The route changes egress, but wan and its source remain valid.
	mustUpsertRoute(t, s, Route{
		ID: "special", Destination: mustCIDR(t, "198.51.100.0/24"), InterfaceID: "backup",
	})
	at := now.Add(time.Minute)
	if recs := s.Evaluate(at); len(recs) != 0 {
		t.Fatalf("route change alone requested rebuild: %+v", recs)
	}
	drift := s.EgressDrift(at)
	if len(drift) != 1 || drift[0].ConnectionID != "c" {
		t.Fatalf("drift = %+v", drift)
	}
	if !drift[0].InterfaceChanged || drift[0].BestInterfaceID != "backup" {
		t.Fatalf("expected drift to backup: %+v", drift[0])
	}
	got, _ := s.Connection("c")
	if got.InterfaceID != "wan" {
		t.Fatalf("established connection was mutated to %s", got.InterfaceID)
	}

	// Actual departure of the original interface is a rebuild condition.
	if err := s.RemoveInterface("wan"); err != nil {
		t.Fatal(err)
	}
	recs := s.Evaluate(at.Add(time.Second))
	rec, ok := findRecommendation(recs, "c")
	if !ok || rec.Reason != RebuildInterfaceDeparted {
		t.Fatalf("after wan removal recs = %+v", recs)
	}
	rebuilt, err := s.RebuildConnection("c", at.Add(2*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if rebuilt.InterfaceID != "backup" {
		t.Fatalf("rebuilt interface = %s, want backup", rebuilt.InterfaceID)
	}
	assertIP(t, rebuilt.Source, "198.51.100.20")
}

func TestInterfaceDownIsAnUnavailableOriginalEgress(t *testing.T) {
	now := testTime()
	s := newDualHostSelector(t, now)
	if _, err := s.OpenConnection("c", mustIP(t, "198.51.100.8"), now); err != nil {
		t.Fatal(err)
	}

	if err := s.SetInterfaceState("wan", InterfaceDown); err != nil {
		t.Fatal(err)
	}
	all, newly := s.EvaluateChanges(now.Add(time.Second))
	rec, ok := findRecommendation(all, "c")
	if !ok || rec.Reason != RebuildInterfaceUnavailable {
		t.Fatalf("down interface recommendations = %+v", all)
	}
	if len(newly) != 1 {
		t.Fatalf("new recommendations = %+v", newly)
	}

	// Coming back up makes the retained source usable again, but the event
	// consumer still must explicitly rebuild/revalidate the broken flow.
	if err := s.SetInterfaceState("wan", InterfaceUp); err != nil {
		t.Fatal(err)
	}
	if all, _ := s.EvaluateChanges(now.Add(2 * time.Second)); len(all) != 1 {
		t.Fatalf("pending rebuild should latch after link recovery: %+v", all)
	}
	if _, err := s.RebuildConnection("c", now.Add(3*time.Second)); err != nil {
		t.Fatal(err)
	}
	if recs := s.Evaluate(now.Add(3 * time.Second)); len(recs) != 0 {
		t.Fatalf("explicit rebuild should clear pending state: %+v", recs)
	}
}

func TestInterleavedRouteInterfaceAddressAndReaddition(t *testing.T) {
	now := testTime()
	s := NewSelector()
	mustUpsertRoute(t, s, Route{ID: "default", Destination: mustCIDR(t, "0.0.0.0/0"), InterfaceID: "eth0"})

	_, err := s.SelectSource(mustIP(t, "198.51.100.8"), now)
	if !errors.Is(err, ErrInterfaceNotFound) {
		t.Fatalf("route-before-interface err = %v, want ErrInterfaceNotFound", err)
	}

	mustUpsertInterface(t, s, Interface{ID: "eth0", State: InterfaceUp})
	_, err = s.SelectSource(mustIP(t, "198.51.100.8"), now)
	if !errors.Is(err, ErrNoUsableAddress) {
		t.Fatalf("interface-before-address err = %v, want ErrNoUsableAddress", err)
	}

	mustUpsertAddress(t, s, "eth0", Address{IP: mustIP(t, "203.0.113.7"), PrefixLength: 24})
	if _, err := s.OpenConnection("c", mustIP(t, "198.51.100.8"), now); err != nil {
		t.Fatal(err)
	}
	if _, err := s.OpenConnection("c", mustIP(t, "198.51.100.9"), now); !errors.Is(err, ErrConnectionExists) {
		t.Fatalf("duplicate open err = %v, want ErrConnectionExists", err)
	}

	if err := s.RemoveAddress("eth0", mustIP(t, "203.0.113.7")); err != nil {
		t.Fatal(err)
	}
	all, newly := s.EvaluateChanges(now.Add(time.Second))
	if len(newly) != 1 || newly[0].Reason != RebuildSourceWithdrawn {
		t.Fatalf("after address removal all=%+v newly=%+v", all, newly)
	}

	// Re-adding the same address makes selection possible again, but does not
	// erase the explicit pending rebuild state.
	mustUpsertAddress(t, s, "eth0", Address{IP: mustIP(t, "203.0.113.7"), PrefixLength: 24})
	all, newly = s.EvaluateChanges(now.Add(2 * time.Second))
	if len(newly) != 0 {
		t.Fatalf("readdition should not create another new recommendation: %+v", newly)
	}
	if _, ok := findRecommendation(all, "c"); !ok {
		t.Fatalf("pending recommendation should latch: %+v", all)
	}
	if _, err := s.RebuildConnection("c", now.Add(3*time.Second)); err != nil {
		t.Fatal(err)
	}
	if recs := s.Evaluate(now.Add(3 * time.Second)); len(recs) != 0 {
		t.Fatalf("connection should recover after rebuild: %+v", recs)
	}
}

func TestCommonPrefixLength(t *testing.T) {
	cases := []struct {
		a, b string
		want int
	}{
		{"192.0.2.1", "192.0.2.1", 32},
		{"192.0.2.1", "192.0.2.127", 25},
		{"10.0.0.2", "10.20.30.40", 11},
		{"2001:db8::", "2001:db8::1", 127},
	}
	for _, tc := range cases {
		if got := CommonPrefixLength(mustIP(t, tc.a), mustIP(t, tc.b)); got != tc.want {
			t.Errorf("CommonPrefixLength(%s,%s)=%d want %d", tc.a, tc.b, got, tc.want)
		}
	}
	if got := CommonPrefixLength(mustIP(t, "192.0.2.1"), mustIP(t, "::1")); got != -1 {
		t.Fatalf("cross-family prefix = %d, want -1", got)
	}
}
