package forwardguard

import (
	"errors"
	"net"
	"testing"
)

// ---------- fixtures and assertion helpers ----------

// parseIP parses a constant test fixture IP and panics if it is malformed.
func parseIP(s string) net.IP {
	ip := net.ParseIP(s)
	if ip == nil {
		panic("test fixture: cannot parse ip " + s)
	}
	return ip
}

// mustCIDR for constant test CIDRs is provided by forwardguard.go.

// loopbackPolicy is the temporary loopback approval used by most cases:
// loopback-only listening, one target resource, both directions.
func loopbackPolicy(revision int) SessionPolicy {
	return SessionPolicy{
		SessionID:         "sess-1",
		AllowedDirections: []Direction{ForwardRemote, ReverseRemote},
		Targets: []TargetRule{{
			Name:         "db",
			AllowedCIDRs: []*net.IPNet{mustCIDR("10.0.0.0/16")},
			AllowedPorts: []PortRange{{From: 8000, To: 8010}},
		}},
		ListenCIDRs:       []*net.IPNet{mustCIDR("127.0.0.0/8"), mustCIDR("::1/128")},
		TemporaryLoopback: true,
		Revision:          revision,
	}
}

func newEval(t *testing.T, p SessionPolicy) *Evaluator {
	t.Helper()
	e, err := NewEvaluator(p)
	if err != nil {
		t.Fatalf("NewEvaluator returned unexpected error: %v", err)
	}
	return e
}

func submit(t *testing.T, e *Evaluator, app Application) {
	t.Helper()
	if err := e.Submit(app); err != nil {
		t.Fatalf("Submit(%s) unexpected error: %v", app.ApplicationID, err)
	}
}

func bind(t *testing.T, e *Evaluator, id string, addrs ...BoundAddress) {
	t.Helper()
	if err := e.RegisterBinding(id, BindingResult{Addresses: addrs}); err != nil {
		t.Fatalf("RegisterBinding(%s) unexpected error: %v", id, err)
	}
}

func enabledIDs(d Decision) map[string]bool {
	out := map[string]bool{}
	for _, c := range d.Enabled {
		out[c.Channel.ApplicationID] = true
	}
	return out
}

func blockedMap(d Decision) map[string]BlockedChannel {
	out := map[string]BlockedChannel{}
	for _, b := range d.Blocked {
		out[b.Channel.ApplicationID] = b
	}
	return out
}

func hasCode(b BlockedChannel, code ReasonCode) bool {
	for _, r := range b.Reasons {
		if r.Code == code {
			return true
		}
	}
	return false
}

func assertBlocked(t *testing.T, d Decision, id string, code ReasonCode) BlockReason {
	t.Helper()
	bm := blockedMap(d)
	b, ok := bm[id]
	if !ok {
		t.Fatalf("channel %q should be blocked with %s, decision: %+v", id, code, d)
	}
	if !hasCode(b, code) {
		t.Fatalf("channel %q blocked without reason %s; reasons=%+v", id, code, b.Reasons)
	}
	if enabledIDs(d)[id] {
		t.Fatalf("channel %q is both enabled and blocked", id)
	}
	for _, r := range b.Reasons {
		if r.Detail == "" {
			t.Errorf("block reason %s on %q has empty detail", r.Code, id)
		}
	}
	var matched BlockReason
	for _, r := range b.Reasons {
		if r.Code == code {
			matched = r
		}
	}
	return matched
}

func assertEnabled(t *testing.T, d Decision, ids ...string) {
	t.Helper()
	got := enabledIDs(d)
	for _, id := range ids {
		if !got[id] {
			t.Errorf("channel %q should be enabled; blocked=%+v", id, blockedMap(d))
		}
	}
	if len(got) != len(ids) {
		t.Errorf("enabled count mismatch: want %v got %v", ids, got)
	}
}

func fwdApp(id string, dynamic bool, requested int) Application {
	return Application{
		ApplicationID: id,
		SessionID:     "sess-1",
		Direction:     ForwardRemote,
		TargetName:    "db",
		TargetIP:      parseIP("10.0.0.5"),
		TargetPort:    8000,
		Dynamic:       dynamic,
		RequestedPort: requested,
	}
}

// ---------- acceptable results ----------

func TestAcceptableDualStackLoopback(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))
	submit(t, e, fwdApp("ch-dual", false, 9000))
	// Both families stay on concrete loopback addresses: reachability is not
	// enlarged, so the dual-stack binding remains inside the approval.
	bind(t, e, "ch-dual",
		BoundAddress{IP: parseIP("127.0.0.1"), Port: 9000},
		BoundAddress{IP: parseIP("::1"), Port: 9000},
	)

	d := e.Confirm()
	assertEnabled(t, d, "ch-dual")
	if len(d.Enabled[0].ListenAddr) != 2 {
		t.Fatalf("expected both bound sockets to be reported, got %d", len(d.Enabled[0].ListenAddr))
	}
	if len(d.Blocked) != 0 {
		t.Fatalf("expected no blocked channels, got %+v", d.Blocked)
	}
}

func TestAcceptableStaticAndDynamic(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))

	submit(t, e, fwdApp("ch-static", false, 9100))
	bind(t, e, "ch-static", BoundAddress{IP: parseIP("127.0.0.1"), Port: 9100})

	dyn := fwdApp("ch-dyn", true, 0)
	dyn.TargetPort = 8001
	submit(t, e, dyn)
	bind(t, e, "ch-dyn", BoundAddress{IP: parseIP("127.0.0.2"), Port: 40321})

	d := e.Confirm()
	assertEnabled(t, d, "ch-static", "ch-dyn")
}

// ---------- results that must be blocked ----------

func TestBlockedDualStackWildcardExpansion(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))
	submit(t, e, fwdApp("ch-wild", false, 9000))
	// IPv6 wildcard enlarges reachability to every IPv6 interface while only
	// loopback is approved: the whole dual-stack channel must be refused.
	bind(t, e, "ch-wild",
		BoundAddress{IP: parseIP("127.0.0.1"), Port: 9000},
		BoundAddress{IP: parseIP("::"), Port: 9000},
	)

	d := e.Confirm()
	r := assertBlocked(t, d, "ch-wild", ReasonDualStackExpansion)
	if r.Boundary == "" || r.Actual == "" {
		t.Errorf("dual-stack block must name the crossed boundary and actual address: %+v", r)
	}
	if r.Actual != "127.0.0.1:9000,[::]:9000" {
		t.Errorf("unexpected actual binding label %q", r.Actual)
	}
}

func TestBlockedDualStackNonLoopbackFamily(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))
	submit(t, e, fwdApp("ch-lan", false, 9000))
	// IPv4 side is loopback, IPv6 side lands on a routable address.
	bind(t, e, "ch-lan",
		BoundAddress{IP: parseIP("127.0.0.1"), Port: 9000},
		BoundAddress{IP: parseIP("2001:db8::1"), Port: 9000},
	)

	d := e.Confirm()
	assertBlocked(t, d, "ch-lan", ReasonDualStackExpansion)
}

func TestBlockedSingleFamilyWildcardUnderLoopbackOnly(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))
	submit(t, e, fwdApp("ch-v4wild", false, 9000))
	// Single-family wildcard is not a dual-stack case; it is rejected through
	// the ordinary listen-segment and loopback-only boundaries instead.
	bind(t, e, "ch-v4wild", BoundAddress{IP: parseIP("0.0.0.0"), Port: 9000})

	d := e.Confirm()
	bm := blockedMap(d)
	b, ok := bm["ch-v4wild"]
	if !ok {
		t.Fatalf("wildcard binding must be blocked: %+v", d)
	}
	if !hasCode(b, ReasonListenerOutOfRange) || !hasCode(b, ReasonLoopbackOnlyViolation) {
		t.Fatalf("wildcard binding must cite both boundaries, got %+v", b.Reasons)
	}
	if hasCode(b, ReasonDualStackExpansion) {
		t.Fatalf("single-family binding must not be labelled dual-stack expansion")
	}
}

func TestBlockedDirectionDenied(t *testing.T) {
	p := loopbackPolicy(1)
	p.AllowedDirections = []Direction{ForwardRemote}
	e := newEval(t, p)

	rev := fwdApp("ch-rev", false, 9200)
	rev.Direction = ReverseRemote
	submit(t, e, rev)
	bind(t, e, "ch-rev", BoundAddress{IP: parseIP("127.0.0.1"), Port: 9200})

	d := e.Confirm()
	r := assertBlocked(t, d, "ch-rev", ReasonDirectionDenied)
	if r.Actual != string(ReverseRemote) {
		t.Errorf("direction block should name reverse_remote, got %q", r.Actual)
	}
}

func TestBlockedTargetAndPortBoundaries(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))

	unknown := fwdApp("ch-unknown", false, 9300)
	unknown.TargetName = "other-service"
	submit(t, e, unknown)
	bind(t, e, "ch-unknown", BoundAddress{IP: parseIP("127.0.0.1"), Port: 9300})

	outOfCIDR := fwdApp("ch-badip", false, 9301)
	outOfCIDR.TargetIP = parseIP("192.0.2.10")
	submit(t, e, outOfCIDR)
	bind(t, e, "ch-badip", BoundAddress{IP: parseIP("127.0.0.1"), Port: 9301})

	badPort := fwdApp("ch-badport", false, 9302)
	badPort.TargetPort = 9999
	submit(t, e, badPort)
	bind(t, e, "ch-badport", BoundAddress{IP: parseIP("127.0.0.1"), Port: 9302})

	d := e.Confirm()
	assertBlocked(t, d, "ch-unknown", ReasonTargetUnknown)
	assertBlocked(t, d, "ch-badip", ReasonTargetOutOfRange)
	r := assertBlocked(t, d, "ch-badport", ReasonPortNotApproved)
	if r.Boundary != "8000-8010" || r.Actual != "9999" {
		t.Errorf("port block boundary mismatch: %+v", r)
	}
}

func TestBlockedDynamicPortReuseAcrossApplications(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))

	a := fwdApp("dyn-a", true, 0)
	b := fwdApp("dyn-b", true, 0)
	submit(t, e, a)
	submit(t, e, b)
	// The ephemeral port is only valid for the application that obtained it;
	// the second application reusing the same port must be refused.
	bind(t, e, "dyn-a", BoundAddress{IP: parseIP("127.0.0.1"), Port: 40007})
	bind(t, e, "dyn-b", BoundAddress{IP: parseIP("127.0.0.2"), Port: 40007})

	d := e.Confirm()
	assertEnabled(t, d, "dyn-a")
	assertBlocked(t, d, "dyn-b", ReasonDynamicPortReused)
}

func TestBlockedStaticPortMismatch(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))
	submit(t, e, fwdApp("ch-static", false, 9100))
	bind(t, e, "ch-static", BoundAddress{IP: parseIP("127.0.0.1"), Port: 9101})

	d := e.Confirm()
	r := assertBlocked(t, d, "ch-static", ReasonStaticPortMismatch)
	if r.Boundary != "requested_port=9100" || r.Actual != "9101" {
		t.Errorf("static port mismatch labels wrong: %+v", r)
	}
}

func TestBlockedWithoutBinding(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))
	submit(t, e, fwdApp("ch-nobind", false, 9100))

	d := e.Confirm()
	assertBlocked(t, d, "ch-nobind", ReasonBindingMissing)
}

func TestBlockedListenerOutsideApprovedSegment(t *testing.T) {
	p := loopbackPolicy(1)
	// Non-loopback approval, but deliberately narrow: no wildcard relaxation.
	p.TemporaryLoopback = false
	p.ListenCIDRs = []*net.IPNet{mustCIDR("127.0.0.0/8")}
	e := newEval(t, p)

	submit(t, e, fwdApp("ch-lanlisten", false, 9100))
	bind(t, e, "ch-lanlisten", BoundAddress{IP: parseIP("10.1.2.3"), Port: 9100})

	d := e.Confirm()
	r := assertBlocked(t, d, "ch-lanlisten", ReasonListenerOutOfRange)
	if r.Family != "ipv4" {
		t.Errorf("listener block should identify address family, got %q", r.Family)
	}
}

// ---------- boundary value cases ----------

func TestCIDRAndPortBoundaries(t *testing.T) {
	p := SessionPolicy{
		SessionID:         "sess-b",
		AllowedDirections: []Direction{ForwardRemote},
		Targets: []TargetRule{{
			Name:         "db",
			AllowedCIDRs: []*net.IPNet{mustCIDR("10.0.0.0/24")},
			AllowedPorts: []PortRange{{From: 8000, To: 8010}},
		}},
		ListenCIDRs:       []*net.IPNet{mustCIDR("127.0.0.0/8")},
		TemporaryLoopback: false,
		Revision:          1,
	}
	e := newEval(t, p)

	// Last address inside /24, last port inside range, edge of loopback /8.
	inside := Application{
		ApplicationID: "inside", SessionID: "sess-b", Direction: ForwardRemote,
		TargetName: "db", TargetIP: parseIP("10.0.0.254"), TargetPort: 8010,
		Dynamic: false, RequestedPort: 9000,
	}
	submit(t, e, inside)
	bind(t, e, "inside", BoundAddress{IP: parseIP("127.255.255.255"), Port: 9000})

	// First address outside /24, first port outside range, first IP outside /8.
	outside := Application{
		ApplicationID: "outside", SessionID: "sess-b", Direction: ForwardRemote,
		TargetName: "db", TargetIP: parseIP("10.0.1.1"), TargetPort: 8011,
		Dynamic: false, RequestedPort: 9001,
	}
	submit(t, e, outside)
	bind(t, e, "outside", BoundAddress{IP: parseIP("128.0.0.1"), Port: 9001})

	d := e.Confirm()
	assertEnabled(t, d, "inside")
	bm := blockedMap(d)
	b, ok := bm["outside"]
	if !ok {
		t.Fatalf("outside-boundary channel must be blocked: %+v", d)
	}
	for _, want := range []ReasonCode{ReasonTargetOutOfRange, ReasonPortNotApproved, ReasonListenerOutOfRange} {
		if !hasCode(b, want) {
			t.Errorf("outside channel missing boundary reason %s; got %+v", want, b.Reasons)
		}
	}
}

// ---------- policy tightening interleave ----------

func tightenFixture(t *testing.T) *Evaluator {
	t.Helper()
	p := SessionPolicy{
		SessionID:         "sess-t",
		AllowedDirections: []Direction{ForwardRemote, ReverseRemote},
		Targets: []TargetRule{{
			Name:         "db",
			AllowedCIDRs: []*net.IPNet{mustCIDR("10.0.0.0/16")},
			AllowedPorts: []PortRange{{From: 8000, To: 8010}},
		}},
		ListenCIDRs:       []*net.IPNet{mustCIDR("127.0.0.0/8")},
		TemporaryLoopback: false,
		Revision:          1,
	}
	e := newEval(t, p)

	submit(t, e, Application{
		ApplicationID: "keep", SessionID: "sess-t", Direction: ForwardRemote,
		TargetName: "db", TargetIP: parseIP("10.0.0.5"), TargetPort: 8000,
		RequestedPort: 9000,
	})
	bind(t, e, "keep", BoundAddress{IP: parseIP("127.0.0.1"), Port: 9000})

	submit(t, e, Application{
		ApplicationID: "reverse", SessionID: "sess-t", Direction: ReverseRemote,
		TargetName: "db", TargetIP: parseIP("10.0.0.6"), TargetPort: 8001,
		RequestedPort: 9001,
	})
	bind(t, e, "reverse", BoundAddress{IP: parseIP("127.0.0.1"), Port: 9001})

	submit(t, e, Application{
		ApplicationID: "widened-listen", SessionID: "sess-t", Direction: ForwardRemote,
		TargetName: "db", TargetIP: parseIP("10.0.2.5"), TargetPort: 8002,
		RequestedPort: 9002,
	})
	bind(t, e, "widened-listen", BoundAddress{IP: parseIP("127.1.2.3"), Port: 9002})

	submit(t, e, Application{
		ApplicationID: "old-port", SessionID: "sess-t", Direction: ForwardRemote,
		TargetName: "db", TargetIP: parseIP("10.0.0.7"), TargetPort: 8010,
		RequestedPort: 9003,
	})
	bind(t, e, "old-port", BoundAddress{IP: parseIP("127.0.0.1"), Port: 9003})

	return e
}

func TestTightenRechecksEveryPendingChannel(t *testing.T) {
	e := tightenFixture(t)

	before := e.Confirm()
	assertEnabled(t, before, "keep", "reverse", "widened-listen", "old-port")

	tight := SessionPolicy{
		SessionID:         "sess-t",
		AllowedDirections: []Direction{ForwardRemote}, // reverse removed
		Targets: []TargetRule{{
			Name:         "db",
			AllowedCIDRs: []*net.IPNet{mustCIDR("10.0.0.0/24")}, // /16 shrunk to /24
			AllowedPorts: []PortRange{{From: 8000, To: 8005}},   // port 8010 cut off
		}},
		ListenCIDRs:       []*net.IPNet{mustCIDR("127.0.0.0/24")}, // /8 shrunk to /24
		TemporaryLoopback: false,
		Revision:          2,
	}
	if err := e.Tighten(tight); err != nil {
		t.Fatalf("legitimate tightening rejected: %v", err)
	}
	if e.PolicyRevision() != 2 {
		t.Fatalf("revision not updated: %d", e.PolicyRevision())
	}

	// Confirmation re-applies the tightened envelope to channels that were
	// previously enabled; no earlier decision is cached.
	after := e.Confirm()
	assertEnabled(t, after, "keep")
	assertBlocked(t, after, "reverse", ReasonDirectionDenied)
	assertBlocked(t, after, "widened-listen", ReasonListenerOutOfRange)
	assertBlocked(t, after, "widened-listen", ReasonTargetOutOfRange)
	assertBlocked(t, after, "old-port", ReasonPortNotApproved)
}

func TestTightenRejectsLoosening(t *testing.T) {
	e := tightenFixture(t)
	legit := SessionPolicy{
		SessionID:         "sess-t",
		AllowedDirections: []Direction{ForwardRemote},
		Targets: []TargetRule{{
			Name:         "db",
			AllowedCIDRs: []*net.IPNet{mustCIDR("10.0.0.0/24")},
			AllowedPorts: []PortRange{{From: 8000, To: 8005}},
		}},
		ListenCIDRs:       []*net.IPNet{mustCIDR("127.0.0.0/24")},
		TemporaryLoopback: false,
		Revision:          2,
	}
	if err := e.Tighten(legit); err != nil {
		t.Fatalf("setup tightening failed: %v", err)
	}

	// Same revision must be refused.
	sameRev := legit
	if err := e.Tighten(sameRev); !errors.Is(err, ErrTightening) {
		t.Errorf("same-revision tighten should fail with ErrTightening, got %v", err)
	}

	// Widening the listen segment must be refused.
	wider := legit
	wider.Revision = 3
	wider.ListenCIDRs = []*net.IPNet{mustCIDR("127.0.0.0/8")}
	if err := e.Tighten(wider); !errors.Is(err, ErrTightening) {
		t.Errorf("listen widening should fail with ErrTightening, got %v", err)
	}

	// Re-adding a direction must be refused.
	moreDirs := legit
	moreDirs.Revision = 3
	moreDirs.AllowedDirections = []Direction{ForwardRemote, ReverseRemote}
	if err := e.Tighten(moreDirs); !errors.Is(err, ErrTightening) {
		t.Errorf("direction widening should fail with ErrTightening, got %v", err)
	}

	// Session id change must be refused.
	otherSession := legit
	otherSession.Revision = 3
	otherSession.SessionID = "sess-other"
	if err := e.Tighten(otherSession); !errors.Is(err, ErrTightening) {
		t.Errorf("session change should fail with ErrTightening, got %v", err)
	}

	// Failed attempts leave the tight-but-valid revision 2 policy in force.
	if e.PolicyRevision() != 2 {
		t.Fatalf("rejected tightening mutated policy to revision %d", e.PolicyRevision())
	}
	d := e.Confirm()
	assertBlocked(t, d, "reverse", ReasonDirectionDenied)
}

func TestTightenCannotDropLoopbackRestriction(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))
	dropped := loopbackPolicy(2)
	dropped.TemporaryLoopback = false
	// Also give it a genuinely non-loopback segment so the loosening intent is
	// unambiguous.
	dropped.ListenCIDRs = []*net.IPNet{mustCIDR("10.0.0.0/8")}
	if err := e.Tighten(dropped); !errors.Is(err, ErrTightening) {
		t.Errorf("dropping temporary loopback must be rejected, got %v", err)
	}
	if e.PolicyRevision() != 1 {
		t.Fatalf("rejected tightening must not change revision, got %d", e.PolicyRevision())
	}
}

// ---------- constructor and input validation ----------

func TestNewEvaluatorRejectsInconsistentLoopbackPolicy(t *testing.T) {
	p := loopbackPolicy(1)
	p.ListenCIDRs = []*net.IPNet{
		mustCIDR("127.0.0.0/8"),
		mustCIDR("10.0.0.0/8"), // non-loopback segment under loopback-only approval
	}
	if _, err := NewEvaluator(p); err == nil {
		t.Fatal("non-loopback listen cidr under TemporaryLoopback must be rejected")
	}
}

func TestNewEvaluatorValidatesRequiredFields(t *testing.T) {
	base := loopbackPolicy(1)
	cases := map[string]func(SessionPolicy) SessionPolicy{
		"empty session id": func(p SessionPolicy) SessionPolicy { p.SessionID = ""; return p },
		"no directions":    func(p SessionPolicy) SessionPolicy { p.AllowedDirections = nil; return p },
		"no targets":       func(p SessionPolicy) SessionPolicy { p.Targets = nil; return p },
		"no listen cidrs":  func(p SessionPolicy) SessionPolicy { p.ListenCIDRs = nil; return p },
		"invalid target port range": func(p SessionPolicy) SessionPolicy {
			p.Targets[0].AllowedPorts = []PortRange{{From: 9000, To: 80}}
			return p
		},
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := NewEvaluator(mutate(base)); err == nil {
				t.Fatalf("%s: expected constructor error", name)
			}
		})
	}
}

func TestSubmitAndBindingValidation(t *testing.T) {
	e := newEval(t, loopbackPolicy(1))

	good := fwdApp("good", false, 9000)
	if err := e.Submit(good); err != nil {
		t.Fatalf("unexpected submit error: %v", err)
	}
	if err := e.Submit(good); err == nil {
		t.Error("duplicate application id must be rejected")
	}
	wrongSession := fwdApp("foreign", false, 9001)
	wrongSession.SessionID = "sess-other"
	if err := e.Submit(wrongSession); err == nil {
		t.Error("application from another session must be rejected")
	}
	staticNoPort := fwdApp("noport", false, 0)
	if err := e.Submit(staticNoPort); err == nil {
		t.Error("static application without requested port must be rejected")
	}
	if err := e.RegisterBinding("good", BindingResult{}); err == nil {
		t.Error("empty binding must be rejected")
	}
	mixedPorts := BindingResult{Addresses: []BoundAddress{
		{IP: parseIP("127.0.0.1"), Port: 9000},
		{IP: parseIP("::1"), Port: 9001},
	}}
	if err := e.RegisterBinding("good", mixedPorts); err == nil {
		t.Error("binding sockets with different ports must be rejected")
	}
	if err := e.RegisterBinding("missing", BindingResult{
		Addresses: []BoundAddress{{IP: parseIP("127.0.0.1"), Port: 9000}},
	}); err == nil {
		t.Error("binding for unknown application must be rejected")
	}
}
