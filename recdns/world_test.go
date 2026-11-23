package recdns

import (
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

// ---- Synthetic zone layout -------------------------------------------------
//
//	root "."                 -> root-a, root-b
//	tld.                     -> tld-a
//	example.tld.             -> ns-a
//	alpha.example.tld. (zone 1)      -> alpha-ns
//	bravo.example.tld. (zone 2)      -> bravo-ns
//
// The two-zone alias chain:
//
//	alias.alpha.example.tld. CNAME host.bravo.example.tld.   (zone alpha.example.tld.)
//	host.bravo.example.tld.  A 203.0.113.7               (zone bravo.example.tld.)
const (
	srvRootA Server = "root-a."
	srvRootB Server = "root-b."
	srvTLD   Server = "tld-a."
	srvNS    Server = "ns-a."
	srvAlpha Server = "alpha-ns."
	srvBravo Server = "bravo-ns."
	srvOld   Server = "old-gamma-ns."
	srvNew   Server = "new-gamma-ns."
	srvFlaky Server = "flaky-ns."

	zoneTLD   Name = "tld."
	zoneEx    Name = "example.tld."
	zoneAlpha Name = "alpha.example.tld."
	zoneBravo Name = "bravo.example.tld."
	zoneGamma Name = "gamma.example.tld."
	zoneDelta Name = "delta.example.tld."

	qChain   Name = "alias.alpha.example.tld."
	cTarget  Name = "host.bravo.example.tld."
	cFinal   Name = "final.bravo.example.tld."
	qNoData  Name = "nodata.alpha.example.tld."
	qAbsent  Name = "missing.alpha.example.tld."
	qMixed   Name = "mixed.alpha.example.tld."
	qLoop    Name = "loop1.alpha.example.tld."
	qCut     Name = "cut.gamma.example.tld."
	qFlaky   Name = "flaky.delta.example.tld."
	qShort   Name = "short.bravo.example.tld."
	qStandby Name = "standby.bravo.example.tld."

	ttlRoot   = 24 * time.Hour
	ttlCut    = 10 * time.Minute
	ttlCNAME1 = 30 * time.Minute
	ttlCNAME2 = 10 * time.Minute
	ttlA      = 5 * time.Minute
	ttlNeg    = 90 * time.Second
)

type manualClock struct {
	mu sync.Mutex
	t  time.Time
}

func newManualClock() *manualClock {
	return &manualClock{t: time.Date(2026, 11, 23, 9, 0, 0, 0, time.UTC)}
}

func (c *manualClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *manualClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// world bundles everything a test needs.
type world struct {
	t          *testing.T
	clock      *manualClock
	fx         *Fixture
	res        *Resolver
	mu         sync.Mutex
	scripts    map[string]*Message    // exact (server, QNAME, type) answers
	referrals  map[string]*Delegation // (server, child-zone) referral table
	fails      map[string]int         // remaining transient transport failures per key
	sfails     map[string]int         // remaining SERVFAIL answers per key
	alwaysFail map[string]bool        // permanent transport failures
}

func key(s Server, n Name, typ Type) string {
	return string(s) + "|" + string(n) + "|" + string(typ)
}

func refKey(s Server, zone Name) string {
	return string(s) + "|" + string(zone)
}

func newWorld(t *testing.T) *world {
	t.Helper()
	w := &world{
		t:          t,
		clock:      newManualClock(),
		scripts:    make(map[string]*Message),
		referrals:  make(map[string]*Delegation),
		fails:      make(map[string]int),
		sfails:     make(map[string]int),
		alwaysFail: make(map[string]bool),
	}
	w.fx = NewFixture(w.handle, w.clock.now)
	w.res = NewResolver(w.fx, []Server{srvRootA, srvRootB}, ttlRoot, w.clock.now)
	w.seed()
	return w
}

func (w *world) handle(s Server, q Query) (*Message, error) {
	k := key(s, q.Name, q.Type)
	w.mu.Lock()
	defer w.mu.Unlock()

	if n := w.fails[k]; n > 0 {
		w.fails[k] = n - 1
		return nil, errors.New("simulated transport timeout")
	}
	if w.alwaysFail[k] {
		return nil, errors.New("permanent simulated transport timeout")
	}
	if n := w.sfails[k]; n > 0 {
		w.sfails[k] = n - 1
		return &Message{Query: q, Server: s, RCode: RCodeServFail}, nil
	}
	if m, ok := w.scripts[k]; ok {
		cp := *m
		cp.Query = q
		cp.Server = s
		return &cp, nil
	}
	// Referral fallback: walk the zone tree from the deepest child up to and
	// including the root; a server only holds referrals for its direct
	// children, so non-matching keys are simply skipped.
	for z := q.Name; ; z = parentZone(z) {
		if d, ok := w.referrals[refKey(s, z)]; ok {
			cp := *d
			return &Message{Query: q, Server: s, RCode: RCodeOK, Delegation: &cp}, nil
		}
		if z == "." {
			break
		}
	}
	w.t.Errorf("unscripted query: server=%s name=%s type=%s", s, q.Name, q.Type)
	return nil, errors.New("unscripted query")
}

func (w *world) on(s Server, n Name, typ Type, m *Message) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.scripts[key(s, n, typ)] = m
}

func (w *world) refer(s Server, d Delegation) {
	w.mu.Lock()
	defer w.mu.Unlock()
	cp := d
	w.referrals[refKey(s, d.Zone)] = &cp
}

func (w *world) failOnce(s Server, n Name, typ Type)  { w.fails[key(s, n, typ)] = 1 }
func (w *world) sfailOnce(s Server, n Name, typ Type) { w.sfails[key(s, n, typ)] = 1 }

// failAlways installs a permanent transport failure independent of the
// decrementing transient-failure counters.
func (w *world) failAlways(s Server, n Name, typ Type) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.alwaysFail[key(s, n, typ)] = true
}

func answer(q Name, typ Type, rrs ...RR) *Message {
	return &Message{Query: Query{Name: q, Type: typ}, RCode: RCodeOK, Answers: rrs}
}

func nxdomain(q Name, typ Type, zone Name, negTTL time.Duration) *Message {
	return &Message{
		Query:     Query{Name: q, Type: typ},
		RCode:     RCodeNXDomain,
		Authority: &SOA{Name: zone, Origin: zone, MinTTL: negTTL},
	}
}

func nodata(q Name, typ Type, zone Name, negTTL time.Duration) *Message {
	return &Message{
		Query:     Query{Name: q, Type: typ},
		RCode:     RCodeOK,
		Authority: &SOA{Name: zone, Origin: zone, MinTTL: negTTL},
	}
}

// seed installs the standard referral tree and all steady-state answers.
func (w *world) seed() {
	// Referral tree: every query arriving at a parent for a name in a child
	// zone produces that child's delegation regardless of full QNAME.
	for _, s := range []Server{srvRootA, srvRootB} {
		w.refer(s, Delegation{
			Zone: zoneTLD, Servers: []Server{srvTLD}, TTL: ttlCut, Origin: "."})
	}
	w.refer(srvTLD, Delegation{
		Zone: zoneEx, Servers: []Server{srvNS}, TTL: ttlCut, Origin: zoneTLD})
	w.refer(srvNS, Delegation{
		Zone: zoneAlpha, Servers: []Server{srvAlpha}, TTL: ttlCut, Origin: zoneEx})
	w.refer(srvNS, Delegation{
		Zone: zoneBravo, Servers: []Server{srvBravo}, TTL: ttlCut, Origin: zoneEx})
	w.refer(srvNS, Delegation{
		Zone: zoneGamma, Servers: []Server{srvOld}, TTL: ttlCut, Origin: zoneEx})
	w.refer(srvNS, Delegation{
		Zone: zoneDelta, Servers: []Server{srvFlaky}, TTL: ttlCut, Origin: zoneEx})

	// Two-zone CNAME chain:
	//   alias.alpha...  CNAME host.bravo...            (zone alpha, 30m)
	//   host.bravo...   CNAME final.bravo... + A(...)  (zone bravo, 10m / 60m)
	// The two CNAMEs originate in two different zones; the terminal A and the
	// bravo CNAME arrive in the same message.
	cname1 := RR{Name: qChain, Type: TypeCNAME, Value: string(cTarget), Origin: zoneAlpha, TTL: ttlCNAME1}
	for _, typ := range []Type{TypeA, TypeAAAA, TypeTXT} {
		w.on(srvAlpha, qChain, typ, answer(qChain, typ, cname1))
	}
	cname2 := RR{Name: cTarget, Type: TypeCNAME, Value: string(cFinal), Origin: zoneBravo, TTL: ttlCNAME2}
	// Long-TTL A record so the answer stays live across the 10-minute cutover
	// window used by TestDelegationCutover's unrelated-cache assertion.
	w.on(srvBravo, cTarget, TypeA, answer(cTarget, TypeA, cname2,
		RR{Name: cFinal, Type: TypeA, Value: "203.0.113.7", Origin: zoneBravo, TTL: time.Hour}))
	// Non-A queries for the intermediate bravo name carry the same forward
	// CNAME so the chain reaches the terminal name before the negative answer.
	w.on(srvBravo, cTarget, TypeAAAA, answer(cTarget, TypeAAAA, cname2))
	w.on(srvBravo, cTarget, TypeTXT, answer(cTarget, TypeTXT, cname2))
	// The terminal name also answers directly (used after the chain is cached
	// and a later hop re-queries at the terminal).
	w.on(srvBravo, cFinal, TypeA, answer(cFinal, TypeA,
		RR{Name: cFinal, Type: TypeA, Value: "203.0.113.7", Origin: zoneBravo, TTL: time.Hour}))
	w.on(srvBravo, cFinal, TypeAAAA, nodata(cFinal, TypeAAAA, zoneBravo, ttlNeg))
	w.on(srvBravo, cFinal, TypeTXT, nodata(cFinal, TypeTXT, zoneBravo, ttlNeg))
	// Dedicated short-TTL positive record for exact expiry boundary tests.
	w.on(srvBravo, qShort, TypeA, answer(qShort, TypeA,
		RR{Name: qShort, Type: TypeA, Value: "203.0.113.99", Origin: zoneBravo, TTL: ttlA}))

	// Long-TTL, alias-free record used to prove unrelated zone data survives a
	// delegation cutover in another zone (gamma) even after the clock advances.
	w.on(srvBravo, qStandby, TypeA, answer(qStandby, TypeA,
		RR{Name: qStandby, Type: TypeA, Value: "203.0.113.77", Origin: zoneBravo, TTL: time.Hour}))

	// Same-name different-type negative fixture inside alpha.example.tld.
	w.on(srvAlpha, qNoData, TypeA, nodata(qNoData, TypeA, zoneAlpha, ttlNeg))
	w.on(srvAlpha, qNoData, TypeAAAA, answer(qNoData, TypeAAAA,
		RR{Name: qNoData, Type: TypeAAAA, Value: "2001:db8::1", Origin: zoneAlpha, TTL: ttlA}))
	w.on(srvAlpha, qAbsent, TypeA, nxdomain(qAbsent, TypeA, zoneAlpha, ttlNeg))
	w.on(srvAlpha, qAbsent, TypeTXT, nxdomain(qAbsent, TypeTXT, zoneAlpha, ttlNeg))
	w.on(srvAlpha, qMixed, TypeA, nodata(qMixed, TypeA, zoneAlpha, ttlNeg))
	w.on(srvAlpha, qMixed, TypeTXT, nodata(qMixed, TypeTXT, zoneAlpha, ttlNeg))

	// CNAME loop entirely inside alpha.example.tld.
	loop1 := RR{Name: qLoop, Type: TypeCNAME, Value: "loop2.alpha.example.tld.", Origin: zoneAlpha, TTL: ttlCNAME1}
	loop2Name := Name("loop2.alpha.example.tld.")
	loop2 := RR{Name: loop2Name, Type: TypeCNAME, Value: string(qLoop), Origin: zoneAlpha, TTL: ttlCNAME1}
	w.on(srvAlpha, qLoop, TypeA, answer(qLoop, TypeA, loop1))
	w.on(srvAlpha, loop2Name, TypeA, answer(loop2Name, TypeA, loop2))

	// Cutover zone: two generations of gamma.example.tld.
	w.on(srvOld, qCut, TypeA, answer(qCut, TypeA,
		RR{Name: qCut, Type: TypeA, Value: "198.51.100.10", Origin: zoneGamma, TTL: ttlA}))
	w.on(srvNew, qCut, TypeA, answer(qCut, TypeA,
		RR{Name: qCut, Type: TypeA, Value: "198.51.100.20", Origin: zoneGamma, TTL: ttlA}))
	w.on(srvNew, qCut, TypeAAAA, answer(qCut, TypeAAAA,
		RR{Name: qCut, Type: TypeAAAA, Value: "2001:db8::20", Origin: zoneGamma, TTL: ttlA}))

	// Transient-failure zone with one multi-homed server group.
	w.on(srvFlaky, qFlaky, TypeA, answer(qFlaky, TypeA,
		RR{Name: qFlaky, Type: TypeA, Value: "192.0.2.55", Origin: zoneDelta, TTL: ttlA}))
}

// ---- Assertions ------------------------------------------------------------

func mustEqual(t *testing.T, got, want interface{}, msg string) {
	t.Helper()
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("%s: got %v, want %v", msg, got, want)
	}
}

func mustServers(t *testing.T, got []Server, want []Server, msg string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s: got %v, want %v", msg, got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Fatalf("%s: got %v, want %v", msg, got, want)
		}
	}
}

// mustTail asserts that the contacted-server sequence ends with want, ignoring
// the (already covered) delegation-walk prefix.
func mustTail(t *testing.T, got []Server, want []Server, msg string) {
	t.Helper()
	if len(got) < len(want) {
		t.Fatalf("%s: got %v, want tail %v", msg, got, want)
	}
	tail := got[len(got)-len(want):]
	for i := range tail {
		if tail[i] != want[i] {
			t.Fatalf("%s: got %v, want tail %v", msg, got, want)
		}
	}
}

func durEq(t *testing.T, got, want time.Duration, msg string) {
	t.Helper()
	if got != want {
		t.Fatalf("%s: got %v, want %v", msg, got, want)
	}
}

func noErr(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}
