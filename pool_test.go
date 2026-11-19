package connpool_test

import (
	"errors"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"

	"example.com/connpool"
)

const (
	gatewayIP = "203.0.113.10:443"
	zoneMain  = "shared-gateway"
)

// fakeResolver is a mutable, concurrency-safe stand-in for DNS.
type fakeResolver struct {
	mu    sync.Mutex
	addrs map[string][]string
	err   error
	calls map[string]int
}

func newResolver(entries map[string][]string) *fakeResolver {
	r := &fakeResolver{addrs: make(map[string][]string), calls: make(map[string]int)}
	for domain, addrs := range entries {
		r.addrs[domain] = append([]string(nil), addrs...)
	}
	return r
}

func (r *fakeResolver) Resolve(domain string) ([]string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls[domain]++
	if r.err != nil {
		return nil, r.err
	}
	addrs, ok := r.addrs[domain]
	if !ok {
		return nil, errors.New("no such host " + domain)
	}
	return append([]string(nil), addrs...), nil
}

func (r *fakeResolver) set(domain string, addrs ...string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.addrs[domain] = append([]string(nil), addrs...)
}

func (r *fakeResolver) callCount(domain string) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.calls[domain]
}

type dialCall struct {
	domain     string
	zone       string
	candidates []string
}

// fakeDialer records dials and delegates record construction to fn.
type fakeDialer struct {
	mu    sync.Mutex
	calls []dialCall
	fn    func(domain, zone string, candidates []string) (connpool.HandshakeRecord, error)
}

func (d *fakeDialer) Dial(domain, zone string, candidates []string) (connpool.HandshakeRecord, error) {
	d.mu.Lock()
	d.calls = append(d.calls, dialCall{
		domain:     domain,
		zone:       zone,
		candidates: append([]string(nil), candidates...),
	})
	d.mu.Unlock()
	return d.fn(domain, zone, candidates)
}

func (d *fakeDialer) callCount() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.calls)
}

func (d *fakeDialer) lastCall(t *testing.T) dialCall {
	t.Helper()
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(d.calls) == 0 {
		t.Fatal("dialer was not called")
	}
	return d.calls[len(d.calls)-1]
}

func h2Record(id string, verified []string, remote, zone string) connpool.HandshakeRecord {
	return connpool.HandshakeRecord{
		ConnectionID:    id,
		Protocol:        connpool.ProtocolHTTP2,
		VerifiedDomains: verified,
		RemoteAddr:      remote,
		Zone:            zone,
	}
}

func mustNew(t *testing.T, opts connpool.Options) *connpool.Pool {
	t.Helper()
	pool, err := connpool.New(opts)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return pool
}

func mustAdd(t *testing.T, pool *connpool.Pool, record connpool.HandshakeRecord) {
	t.Helper()
	if err := pool.AddConnection(record); err != nil {
		t.Fatalf("AddConnection(%s): %v", record.ConnectionID, err)
	}
}

func mustAcquire(t *testing.T, pool *connpool.Pool, domain string) (*connpool.Stream, connpool.Decision) {
	t.Helper()
	stream, decision, err := pool.Acquire(domain)
	if err != nil {
		t.Fatalf("Acquire(%s): %v (decision %s)", domain, err, decision)
	}
	if stream == nil {
		t.Fatalf("Acquire(%s): nil stream", domain)
	}
	return stream, decision
}

func skipReasons(decision connpool.Decision) map[string]string {
	out := make(map[string]string, len(decision.Skipped))
	for _, skip := range decision.Skipped {
		out[skip.ConnectionID] = skip.Reason
	}
	return out
}

func infosByID(pool *connpool.Pool) map[string]connpool.ConnectionInfo {
	out := make(map[string]connpool.ConnectionInfo)
	for _, info := range pool.ConnectionInfo() {
		out[info.ID] = info
	}
	return out
}

// TestThreeDomainsShareCertificateThenPerDomain421 is the headline scenario:
// three domains covered by one certificate reusing one HTTP/2 connection, then
// a 421 for one of them splitting only that (domain, connection) pair.
func TestThreeDomainsShareCertificateThenPerDomain421(t *testing.T) {
	domains := []string{"a.example.com", "b.example.com", "c.example.com"}
	resolver := newResolver(map[string][]string{
		"a.example.com": {gatewayIP},
		"b.example.com": {gatewayIP},
		"c.example.com": {gatewayIP},
	})
	zones := connpool.ZoneMap{
		"a.example.com": zoneMain,
		"b.example.com": zoneMain,
		"c.example.com": zoneMain,
	}
	var (
		dialMu sync.Mutex
		dialNo int
	)
	dialer := &fakeDialer{fn: func(domain, zone string, candidates []string) (connpool.HandshakeRecord, error) {
		dialMu.Lock()
		defer dialMu.Unlock()
		dialNo++
		return h2Record("conn-b"+strconv.Itoa(dialNo), []string{"b.example.com"}, candidates[0], zone), nil
	}}
	pool := mustNew(t, connpool.Options{Resolver: resolver, Zones: zones, Dialer: dialer})

	mustAdd(t, pool, h2Record("conn-1", []string{"*.example.com"}, gatewayIP, zoneMain))

	// All three domains must reuse the single shared connection.
	streams := make(map[string]*connpool.Stream, len(domains))
	for _, domain := range domains {
		stream, decision := mustAcquire(t, pool, domain)
		if decision.Action != connpool.ActionReuse || decision.ConnectionID != "conn-1" {
			t.Fatalf("%s: got %s, want reuse on conn-1", domain, decision)
		}
		streams[domain] = stream
	}
	if got := dialer.callCount(); got != 0 {
		t.Fatalf("dialer called %d times before any failure, want 0", got)
	}
	if got := infosByID(pool)["conn-1"].ActiveStreams; got != 3 {
		t.Fatalf("conn-1 active streams = %d, want 3", got)
	}

	// A 421 for b on conn-1 revokes exactly that pair.
	if err := pool.Handle421("conn-1", "b.example.com"); err != nil {
		t.Fatalf("Handle421: %v", err)
	}

	streamB2, decisionB := mustAcquire(t, pool, "b.example.com")
	if decisionB.Action != connpool.ActionDial || decisionB.ConnectionID != "conn-b1" {
		t.Fatalf("b after 421: got %s, want dial on conn-b1", decisionB)
	}
	if reason := skipReasons(decisionB)["conn-1"]; reason != connpool.ReasonRevokedPair {
		t.Fatalf("b did not skip conn-1 for 421: %v", decisionB.Skipped)
	}
	call := dialer.lastCall(t)
	if call.domain != "b.example.com" || call.zone != zoneMain {
		t.Fatalf("dial call = %+v", call)
	}
	if !reflect.DeepEqual(call.candidates, []string{gatewayIP}) {
		t.Fatalf("dial candidates = %v, want [%s]", call.candidates, gatewayIP)
	}

	// a and c keep reusing conn-1; the new conn-b must not be selected for them
	// because its certificate only covers b.
	for _, domain := range []string{"a.example.com", "c.example.com"} {
		_, decision := mustAcquire(t, pool, domain)
		if decision.Action != connpool.ActionReuse || decision.ConnectionID != "conn-1" {
			t.Fatalf("%s after b's 421: got %s, want reuse on conn-1", domain, decision)
		}
	}

	// Streams that were already running keep running and can end cleanly.
	if streams["a.example.com"].ConnectionID != "conn-1" || streams["b.example.com"].ConnectionID != "conn-1" {
		t.Fatal("pre-421 streams were moved off conn-1")
	}
	if err := streams["b.example.com"].End(); err != nil {
		t.Fatalf("End pre-421 b stream: %v", err)
	}
	if err := streams["b.example.com"].End(); !errors.Is(err, connpool.ErrStreamEnded) {
		t.Fatalf("double End = %v, want ErrStreamEnded", err)
	}
	if err := streams["a.example.com"].End(); err != nil {
		t.Fatalf("End a stream: %v", err)
	}

	// b now reuses its own connection; no extra dial is needed.
	if _, decision := mustAcquire(t, pool, "b.example.com"); decision.Action != connpool.ActionReuse || decision.ConnectionID != "conn-b1" {
		t.Fatalf("b second acquisition: got %s, want reuse on conn-b1", decision)
	}
	if got := dialer.callCount(); got != 1 {
		t.Fatalf("dialer calls = %d after b recovered, want 1", got)
	}

	// A 421 on conn-b1 for b does not touch conn-1 or the other domains.
	if err := pool.Handle421("conn-b1", "b.example.com"); err != nil {
		t.Fatalf("Handle421 conn-b1: %v", err)
	}
	streamB3, decisionB3 := mustAcquire(t, pool, "b.example.com")
	if decisionB3.Action != connpool.ActionDial {
		t.Fatalf("b after second 421: got %s, want dial", decisionB3)
	}
	if reason := skipReasons(decisionB3)["conn-b1"]; reason != connpool.ReasonRevokedPair {
		t.Fatalf("b did not skip conn-b1 for 421: %v", decisionB3.Skipped)
	}
	if streamB3.ConnectionID == streamB2.ConnectionID {
		t.Fatal("421 on conn-b1 still selected conn-b1")
	}
	if err := streamB2.End(); err != nil {
		t.Fatalf("End b stream on revoked conn-b1: %v", err)
	}

	_, decisionA := mustAcquire(t, pool, "a.example.com")
	if decisionA.Action != connpool.ActionReuse || decisionA.ConnectionID != "conn-1" {
		t.Fatalf("a after conn-b1 421: got %s, want reuse on conn-1", decisionA)
	}

	infos := infosByID(pool)
	if got := infos["conn-1"].RevokedDomains; !reflect.DeepEqual(got, []string{"b.example.com"}) {
		t.Fatalf("conn-1 revoked domains = %v, want [b.example.com]", got)
	}
	if got := infos["conn-b1"].RevokedDomains; !reflect.DeepEqual(got, []string{"b.example.com"}) {
		t.Fatalf("conn-b1 revoked domains = %v, want [b.example.com]", got)
	}
	if got := infos["conn-b2"].RevokedDomains; len(got) != 0 {
		t.Fatalf("conn-b2 revoked domains = %v, want none", got)
	}
}

// TestReuseRequiresAllThreeConstraints checks certificate, address and zone
// individually: failing any one forces a dial, never a reuse.
func TestReuseRequiresAllThreeConstraints(t *testing.T) {
	const domain = "host.example.com"

	tests := []struct {
		name         string
		record       connpool.HandshakeRecord
		resolved     []string
		zone         string
		wantSkip     string
		wantDialZone string
	}{
		{
			name:         "baseline reusable",
			record:       h2Record("base", []string{"*.example.com"}, gatewayIP, zoneMain),
			resolved:     []string{gatewayIP},
			zone:         zoneMain,
			wantDialZone: zoneMain,
		},
		{
			name:         "certificate does not cover",
			record:       h2Record("bad-cert", []string{"*.other.example"}, gatewayIP, zoneMain),
			resolved:     []string{gatewayIP},
			zone:         zoneMain,
			wantSkip:     connpool.ReasonCertificateMismatch,
			wantDialZone: zoneMain,
		},
		{
			name:         "address not in current resolution",
			record:       h2Record("bad-addr", []string{"*.example.com"}, "203.0.113.99:443", zoneMain),
			resolved:     []string{gatewayIP},
			zone:         zoneMain,
			wantSkip:     connpool.ReasonAddressMismatch,
			wantDialZone: zoneMain,
		},
		{
			name:         "different isolation zone",
			record:       h2Record("bad-zone", []string{"*.example.com"}, gatewayIP, "zone-b"),
			resolved:     []string{gatewayIP},
			zone:         zoneMain,
			wantSkip:     connpool.ReasonZoneMismatch,
			wantDialZone: zoneMain,
		},
		{
			name:         "not HTTP/2",
			record:       connpool.HandshakeRecord{ConnectionID: "h1", Protocol: "http/1.1", VerifiedDomains: []string{"*.example.com"}, RemoteAddr: gatewayIP, Zone: zoneMain},
			resolved:     []string{gatewayIP},
			zone:         zoneMain,
			wantSkip:     connpool.ReasonProtocolMismatch,
			wantDialZone: zoneMain,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			resolver := newResolver(map[string][]string{domain: tc.resolved})
			zones := connpool.ZoneMap{domain: tc.zone}
			dialer := &fakeDialer{fn: func(d, zone string, candidates []string) (connpool.HandshakeRecord, error) {
				return h2Record("fresh", []string{"*.example.com"}, candidates[0], zone), nil
			}}
			pool := mustNew(t, connpool.Options{Resolver: resolver, Zones: zones, Dialer: dialer})
			mustAdd(t, pool, tc.record)

			stream, decision := mustAcquire(t, pool, domain)
			stream.End()

			if tc.wantSkip == "" {
				if decision.Action != connpool.ActionReuse || decision.ConnectionID != tc.record.ConnectionID {
					t.Fatalf("got %s, want reuse on %s", decision, tc.record.ConnectionID)
				}
				if dialer.callCount() != 0 {
					t.Fatal("dialer used although connection was reusable")
				}
				return
			}

			if decision.Action != connpool.ActionDial {
				t.Fatalf("got %s, want dial", decision)
			}
			if reason := skipReasons(decision)[tc.record.ConnectionID]; reason != tc.wantSkip {
				t.Fatalf("skip reason = %q, want %q (all skips %v)", reason, tc.wantSkip, decision.Skipped)
			}
			call := dialer.lastCall(t)
			if call.zone != tc.wantDialZone {
				t.Fatalf("dial zone = %q, want %q", call.zone, tc.wantDialZone)
			}
			if len(pool.ConnectionInfo()) != 2 {
				t.Fatalf("pool has %d connections, want 2", len(pool.ConnectionInfo()))
			}
		})
	}
}

// TestAddressRotationIsEvaluatedPerDomain covers a partial rotation: one domain
// moves to a new address and must re-dial, while the other keeps reusing the
// old connection until its own resolution changes.
func TestAddressRotationIsEvaluatedPerDomain(t *testing.T) {
	const (
		oldIP = "192.0.2.10:443"
		newIP = "192.0.2.20:443"
	)
	resolver := newResolver(map[string][]string{
		"a.svc.example": {oldIP},
		"b.svc.example": {oldIP},
	})
	zones := connpool.ZoneMap{"a.svc.example": "svc", "b.svc.example": "svc"}
	dialer := &fakeDialer{fn: func(domain, zone string, candidates []string) (connpool.HandshakeRecord, error) {
		return h2Record("rotated-"+domain, []string{"*.svc.example"}, candidates[0], zone), nil
	}}
	pool := mustNew(t, connpool.Options{Resolver: resolver, Zones: zones, Dialer: dialer})
	mustAdd(t, pool, h2Record("original", []string{"*.svc.example"}, oldIP, "svc"))

	a1, _ := mustAcquire(t, pool, "a.svc.example")
	b1, d := mustAcquire(t, pool, "b.svc.example")
	if d.ConnectionID != "original" {
		t.Fatalf("b initial connection = %s, want original", d.ConnectionID)
	}

	// Only a rotates. Its next request must be judged against the new value.
	resolver.set("a.svc.example", newIP)
	a2, decisionA := mustAcquire(t, pool, "a.svc.example")
	if decisionA.Action != connpool.ActionDial {
		t.Fatalf("a after rotation: got %s, want dial", decisionA)
	}
	if reason := skipReasons(decisionA)["original"]; reason != connpool.ReasonAddressMismatch {
		t.Fatalf("a skip reason = %q, want %q", reason, connpool.ReasonAddressMismatch)
	}
	if !reflect.DeepEqual(decisionA.Resolved, []string{newIP}) {
		t.Fatalf("decision resolved = %v, want [%s]", decisionA.Resolved, newIP)
	}
	call := dialer.lastCall(t)
	if !reflect.DeepEqual(call.candidates, []string{newIP}) {
		t.Fatalf("dial candidates = %v, want [%s]", call.candidates, newIP)
	}

	// b still resolves to the old address, so it keeps the original connection.
	b2, decisionB := mustAcquire(t, pool, "b.svc.example")
	if decisionB.Action != connpool.ActionReuse || decisionB.ConnectionID != "original" {
		t.Fatalf("b before its rotation: got %s, want reuse on original", decisionB)
	}
	if dialer.callCount() != 1 {
		t.Fatalf("dialer calls = %d, want 1", dialer.callCount())
	}

	// Streams opened before the rotation remain valid on the original conn.
	if err := a1.End(); err != nil {
		t.Fatal(err)
	}
	if err := b1.End(); err != nil {
		t.Fatal(err)
	}

	// b now rotates too and should reuse a's new connection instead of dialing.
	resolver.set("b.svc.example", newIP)
	b3, decisionB3 := mustAcquire(t, pool, "b.svc.example")
	if decisionB3.Action != connpool.ActionReuse || decisionB3.ConnectionID != a2.ConnectionID {
		t.Fatalf("b after rotation: got %s, want reuse on %s", decisionB3, a2.ConnectionID)
	}
	if dialer.callCount() != 1 {
		t.Fatalf("dialer calls = %d after b followed rotation, want 1", dialer.callCount())
	}

	// A 421 on the rotated connection still revokes only (b, that connection).
	if err := pool.Handle421(a2.ConnectionID, "b.svc.example"); err != nil {
		t.Fatal(err)
	}
	b4, decisionB4 := mustAcquire(t, pool, "b.svc.example")
	if decisionB4.Action != connpool.ActionDial {
		t.Fatalf("b after 421 on rotated conn: got %s, want dial", decisionB4)
	}
	if reason := skipReasons(decisionB4)[a2.ConnectionID]; reason != connpool.ReasonRevokedPair {
		t.Fatalf("b skip reason = %q, want %q", reason, connpool.ReasonRevokedPair)
	}
	if b4.ConnectionID == a2.ConnectionID {
		t.Fatal("revoked connection reused")
	}
	// a's (domain, connection) pair was never revoked: the 421 for b must not
	// make a's own connection ineligible. Least-loaded spreading may still move
	// a to the freshly dialed connection, so assert on eligibility, not choice.
	_, decisionA2 := mustAcquire(t, pool, "a.svc.example")
	if reason, blocked := skipReasons(decisionA2)[a2.ConnectionID]; blocked {
		t.Fatalf("a was blocked from its own connection by b's 421: %s", reason)
	}
	if decisionA2.ConnectionID != a2.ConnectionID && decisionA2.ConnectionID != b4.ConnectionID {
		t.Fatalf("a picked an unexpected connection: %s", decisionA2)
	}
	if err := b4.End(); err != nil {
		t.Fatal(err)
	}
	if err := b2.End(); err != nil {
		t.Fatal(err)
	}
	if err := b3.End(); err != nil {
		t.Fatal(err)
	}
}

// TestWildcardCoverageBoundary pins the exact wildcard semantics that decide
// reuse: one extra label only.
func TestWildcardCoverageBoundary(t *testing.T) {
	const addr = "198.51.100.7:443"
	domains := []string{"a.example.com", "example.com", "a.b.example.com"}
	resolver := newResolver(map[string][]string{})
	zones := connpool.ZoneMap{}
	for _, domain := range domains {
		resolver.set(domain, addr)
		zones[domain] = zoneMain
	}
	dialer := &fakeDialer{fn: func(domain, zone string, candidates []string) (connpool.HandshakeRecord, error) {
		return h2Record("for-"+domain, []string{domain}, candidates[0], zone), nil
	}}
	pool := mustNew(t, connpool.Options{Resolver: resolver, Zones: zones, Dialer: dialer})
	mustAdd(t, pool, h2Record("wild", []string{"*.example.com"}, addr, zoneMain))

	_, decision := mustAcquire(t, pool, "a.example.com")
	if decision.Action != connpool.ActionReuse || decision.ConnectionID != "wild" {
		t.Fatalf("one-label subdomain: got %s, want reuse on wild", decision)
	}

	for _, domain := range []string{"example.com", "a.b.example.com"} {
		_, decision := mustAcquire(t, pool, domain)
		if decision.Action != connpool.ActionDial {
			t.Fatalf("%s: got %s, want dial (wildcard must not cover it)", domain, decision)
		}
		if reason := skipReasons(decision)["wild"]; reason != connpool.ReasonCertificateMismatch {
			t.Fatalf("%s skip reason = %q, want %q", domain, reason, connpool.ReasonCertificateMismatch)
		}
	}
}

// TestDialResultIsValidated ensures a dial cannot smuggle in a connection that
// violates the same reuse constraints, and that rejected connections stay out
// of the pool.
func TestDialResultIsValidated(t *testing.T) {
	const domain = "edge.example.com"

	tests := []struct {
		name   string
		record connpool.HandshakeRecord
		want   error
	}{
		{
			name:   "wrong certificate",
			record: h2Record("c", []string{"other.example"}, gatewayIP, zoneMain),
			want:   connpool.ErrCertificateMismatch,
		},
		{
			name:   "wrong address",
			record: h2Record("c", []string{"edge.example.com"}, "203.0.113.77:443", zoneMain),
			want:   connpool.ErrAddressMismatch,
		},
		{
			name:   "wrong zone",
			record: h2Record("c", []string{"edge.example.com"}, gatewayIP, "other-zone"),
			want:   connpool.ErrZoneMismatch,
		},
		{
			name:   "wrong protocol",
			record: connpool.HandshakeRecord{ConnectionID: "c", Protocol: "h3", VerifiedDomains: []string{"edge.example.com"}, RemoteAddr: gatewayIP, Zone: zoneMain},
			want:   connpool.ErrProtocolUnsupported,
		},
		{
			name:   "empty connection id",
			record: h2Record("", []string{"edge.example.com"}, gatewayIP, zoneMain),
			want:   connpool.ErrInvalidRecord,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			resolver := newResolver(map[string][]string{domain: {gatewayIP}})
			pool := mustNew(t, connpool.Options{
				Resolver: resolver,
				Zones:    connpool.ZoneMap{domain: zoneMain},
				Dialer:   &fakeDialer{fn: func(string, string, []string) (connpool.HandshakeRecord, error) { return tc.record, nil }},
			})

			stream, decision, err := pool.Acquire(domain)
			if stream != nil {
				t.Fatal("Acquire returned a stream for a rejected dial")
			}
			if !errors.Is(err, tc.want) {
				t.Fatalf("Acquire error = %v, want %v", err, tc.want)
			}
			if decision.Action != connpool.ActionDial {
				t.Fatalf("decision = %s, want dial attempt", decision)
			}
			if got := pool.ConnectionInfo(); len(got) != 0 {
				t.Fatalf("rejected dial registered %d connections", len(got))
			}
		})
	}
}

// TestAddressPortBoundary confirms that port differences count when both sides
// carry one, but a bare-IP resolution still matches a port-qualified peer.
func TestAddressPortBoundary(t *testing.T) {
	const domain = "port.example.com"
	resolver := newResolver(map[string][]string{domain: {"203.0.113.10:8443"}})
	dialer := &fakeDialer{fn: func(d, zone string, candidates []string) (connpool.HandshakeRecord, error) {
		return h2Record("fresh", []string{domain}, candidates[0], zone), nil
	}}
	pool := mustNew(t, connpool.Options{
		Resolver: resolver,
		Zones:    connpool.ZoneMap{domain: zoneMain},
		Dialer:   dialer,
	})
	mustAdd(t, pool, h2Record("port-443", []string{domain}, "203.0.113.10:443", zoneMain))

	stream, decision := mustAcquire(t, pool, domain)
	stream.End()
	if decision.Action != connpool.ActionDial {
		t.Fatalf("port mismatch: got %s, want dial", decision)
	}
	if reason := skipReasons(decision)["port-443"]; reason != connpool.ReasonAddressMismatch {
		t.Fatalf("skip reason = %q, want %q", reason, connpool.ReasonAddressMismatch)
	}

	// A bare IP in the resolution set matches any port on the same IP.
	bareResolver := newResolver(map[string][]string{domain: {"203.0.113.10"}})
	second := mustNew(t, connpool.Options{
		Resolver: bareResolver,
		Zones:    connpool.ZoneMap{domain: zoneMain},
		Dialer:   dialer,
	})
	mustAdd(t, second, h2Record("any-port", []string{domain}, "203.0.113.10:443", zoneMain))
	stream2, decision2 := mustAcquire(t, second, domain)
	stream2.End()
	if decision2.Action != connpool.ActionReuse || decision2.ConnectionID != "any-port" {
		t.Fatalf("bare IP: got %s, want reuse on any-port", decision2)
	}
}

// TestBookkeepingAndInvalidInputs covers the refusals around the happy path.
func TestBookkeepingAndInvalidInputs(t *testing.T) {
	if _, err := connpool.New(connpool.Options{}); !errors.Is(err, connpool.ErrNoResolver) {
		t.Fatalf("New without resolver = %v, want ErrNoResolver", err)
	}
	if _, err := connpool.New(connpool.Options{Resolver: newResolver(nil)}); !errors.Is(err, connpool.ErrNoZonePolicy) {
		t.Fatalf("New without zones = %v, want ErrNoZonePolicy", err)
	}

	resolver := newResolver(map[string][]string{"ok.example.com": {gatewayIP}})
	pool := mustNew(t, connpool.Options{
		Resolver: resolver,
		Zones:    connpool.ZoneMap{"ok.example.com": zoneMain},
	})

	if err := pool.AddConnection(h2Record("dup", []string{"ok.example.com"}, gatewayIP, zoneMain)); err != nil {
		t.Fatal(err)
	}
	if err := pool.AddConnection(h2Record("dup", []string{"ok.example.com"}, gatewayIP, zoneMain)); !errors.Is(err, connpool.ErrDuplicateConnection) {
		t.Fatalf("duplicate AddConnection = %v, want ErrDuplicateConnection", err)
	}
	if err := pool.AddConnection(h2Record("bad-addr", []string{"ok.example.com"}, "not-an-ip", zoneMain)); !errors.Is(err, connpool.ErrInvalidRecord) {
		t.Fatalf("invalid remote = %v, want ErrInvalidRecord", err)
	}
	if err := pool.AddConnection(connpool.HandshakeRecord{ConnectionID: "no-cert", Protocol: "h2", RemoteAddr: gatewayIP, Zone: zoneMain}); !errors.Is(err, connpool.ErrInvalidRecord) {
		t.Fatalf("missing verified domains = %v, want ErrInvalidRecord", err)
	}
	if err := pool.Handle421("missing", "ok.example.com"); !errors.Is(err, connpool.ErrUnknownConnection) {
		t.Fatalf("Handle421 unknown conn = %v, want ErrUnknownConnection", err)
	}
	if err := pool.Handle421("dup", ""); !errors.Is(err, connpool.ErrEmptyDomain) {
		t.Fatalf("Handle421 empty domain = %v, want ErrEmptyDomain", err)
	}
	if _, _, err := pool.Acquire(""); !errors.Is(err, connpool.ErrEmptyDomain) {
		t.Fatalf("Acquire empty = %v, want ErrEmptyDomain", err)
	}
	if _, _, err := pool.Acquire("unknown.example.com"); !errors.Is(err, connpool.ErrUnknownZone) {
		t.Fatalf("Acquire unknown zone = %v, want ErrUnknownZone", err)
	}

	resolver.set("empty.example.com")
	poolNoDial := mustNew(t, connpool.Options{
		Resolver: resolver,
		Zones:    connpool.ZoneMap{"empty.example.com": zoneMain},
	})
	if _, _, err := poolNoDial.Acquire("empty.example.com"); !errors.Is(err, connpool.ErrNoAddresses) {
		t.Fatalf("Acquire with empty resolution = %v, want ErrNoAddresses", err)
	}

	resolver.set("empty.example.com", gatewayIP)
	_, decision, err := poolNoDial.Acquire("empty.example.com")
	if !errors.Is(err, connpool.ErrNoDialer) {
		t.Fatalf("Acquire without dialer = %v, want ErrNoDialer", err)
	}
	if decision.Action != connpool.ActionDial || decision.Reason != "no_dialer" {
		t.Fatalf("no-dialer decision = %+v", decision)
	}
}

// TestConcurrentAcquireIsConsistent checks that concurrent acquisitions and
// stream endings keep bookkeeping exact.
func TestConcurrentAcquireIsConsistent(t *testing.T) {
	const domain = "concurrent.example.com"
	resolver := newResolver(map[string][]string{domain: {gatewayIP}})
	pool := mustNew(t, connpool.Options{
		Resolver: resolver,
		Zones:    connpool.ZoneMap{domain: zoneMain},
		Dialer: &fakeDialer{fn: func(d, zone string, candidates []string) (connpool.HandshakeRecord, error) {
			return h2Record("shared", []string{domain}, candidates[0], zone), nil
		}},
	})
	mustAdd(t, pool, h2Record("shared", []string{domain}, gatewayIP, zoneMain))

	const workers = 32
	streams := make([]*connpool.Stream, 0, workers)
	var (
		mu  sync.Mutex
		wg  sync.WaitGroup
		bad int
	)
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			stream, decision, err := pool.Acquire(domain)
			if err != nil || decision.Action != connpool.ActionReuse || decision.ConnectionID != "shared" {
				mu.Lock()
				bad++
				mu.Unlock()
				return
			}
			mu.Lock()
			streams = append(streams, stream)
			mu.Unlock()
		}()
	}
	wg.Wait()
	if bad != 0 {
		t.Fatalf("%d concurrent acquisitions did not reuse the shared connection", bad)
	}
	if got := infosByID(pool)["shared"].ActiveStreams; got != workers {
		t.Fatalf("active streams = %d, want %d", got, workers)
	}
	for _, stream := range streams {
		if err := stream.End(); err != nil {
			t.Fatalf("End: %v", err)
		}
	}
	if got := infosByID(pool)["shared"].ActiveStreams; got != 0 {
		t.Fatalf("active streams after End = %d, want 0", got)
	}
}

// TestDecisionStringIsStable keeps the human-readable audit line meaningful:
// it must name the domain, action, connection and skip reasons.
func TestDecisionStringIsStable(t *testing.T) {
	resolver := newResolver(map[string][]string{"x.example.com": {gatewayIP}})
	pool := mustNew(t, connpool.Options{
		Resolver: resolver,
		Zones:    connpool.ZoneMap{"x.example.com": zoneMain},
		Dialer: &fakeDialer{fn: func(d, zone string, candidates []string) (connpool.HandshakeRecord, error) {
			return h2Record("new", []string{"x.example.com"}, candidates[0], zone), nil
		}},
	})
	mustAdd(t, pool, h2Record("old", []string{"x.example.com"}, "203.0.113.50:443", zoneMain))
	_, decision := mustAcquire(t, pool, "x.example.com")
	line := decision.String()
	for _, want := range []string{
		"x.example.com", "zone=shared-gateway", "action=dial", "conn=new",
		"resolved=" + gatewayIP, "skip(old=address_mismatch)",
	} {
		if !strings.Contains(line, want) {
			t.Fatalf("decision string %q missing %q", line, want)
		}
	}
}
