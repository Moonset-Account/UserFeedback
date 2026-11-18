package relaydecision

import (
	"testing"
	"time"
)

// ---- local test fixtures ---------------------------------------------------

type fakeClock struct {
	t time.Time
}

func newFakeClock() *fakeClock {
	return &fakeClock{t: time.Date(2026, 11, 19, 8, 0, 0, 0, time.UTC)}
}

func (f *fakeClock) now() time.Time          { return f.t }
func (f *fakeClock) advance(d time.Duration) { f.t = f.t.Add(d) }

func testEngine(c *fakeClock) *Engine {
	return NewEngineWithClock(c.now, 30*time.Second, 10*time.Minute)
}

func relaySet() []Relay {
	return []Relay{
		{Name: "relay-a", Priority: 10, SupportsTLS: false},
		{Name: "relay-b", Priority: 20, SupportsTLS: true},
		{Name: "relay-c", Priority: 30, SupportsTLS: true},
	}
}

func mustEnqueue(t *testing.T, e *Engine, id, domain string, relays []Relay, deadline time.Time) {
	t.Helper()
	if err := e.Enqueue(EnqueueRequest{ID: id, Domain: domain, Relays: relays, Deadline: deadline}); err != nil {
		t.Fatalf("Enqueue(%s) returned unexpected error: %v", id, err)
	}
}

func mustNext(t *testing.T, d Decision, wantName string) {
	t.Helper()
	if d.Kind != KindNext {
		t.Fatalf("got Kind=%v Reason=%s, want KindNext(%s)", d.Kind, d.Reason, wantName)
	}
	if d.Relay == nil || d.Relay.Name != wantName {
		t.Fatalf("got relay %+v, want relay %q", d.Relay, wantName)
	}
}

func mustTerminate(t *testing.T, d Decision, want Reason) {
	t.Helper()
	if d.Kind != KindTerminate || d.Reason != want {
		t.Fatalf("got Kind=%v Reason=%s, want KindTerminate/%s", d.Kind, d.Reason, want)
	}
}

func mustDefer(t *testing.T, d Decision, want Reason) {
	t.Helper()
	if d.Kind != KindDefer || d.Reason != want {
		t.Fatalf("got Kind=%v Reason=%s, want KindDefer/%s", d.Kind, d.Reason, want)
	}
}

func findAttempt(t *testing.T, d Decision, relay string) Attempt {
	t.Helper()
	for _, a := range d.Attempts {
		if a.RelayName == relay {
			return a
		}
	}
	t.Fatalf("no attempt recorded for relay %q; attempts: %+v", relay, d.Attempts)
	return Attempt{}
}

// ---- 1. acceptable path under a TLS-required domain ------------------------

func TestRequiredDomainAcceptablePathCarriesRequirementToEveryCandidate(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "example.com", TLS: TLSRequired, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	mustEnqueue(t, e, "tx-1", "example.com", relaySet(), clock.t.Add(5*time.Minute))

	// First candidate cannot provide TLS: it must be rejected, and the next
	// candidate must still satisfy the same TLS requirement.
	d1, err := e.Evaluate("tx-1")
	if err != nil {
		t.Fatalf("Evaluate: %v", err)
	}
	mustNext(t, d1, "relay-b")
	if d1.Reason != ReasonFallbackCandidate {
		t.Fatalf("got reason %s, want %s", d1.Reason, ReasonFallbackCandidate)
	}
	a := findAttempt(t, d1, "relay-a")
	if a.Reason != ReasonRelayNoTLS {
		t.Fatalf("relay-a attempt reason = %s, want %s", a.Reason, ReasonRelayNoTLS)
	}
	if !d1.Relay.SupportsTLS {
		t.Fatal("fallback candidate relay-b must still satisfy the required-TLS policy")
	}

	// relay-b offers TLS but identity verification fails: the reason is
	// recorded and the requirement is not lowered on this candidate.
	if err := e.ReportHandshake("tx-1", HandshakeIdentityFailure, "certificate subject mismatch"); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}
	d2, err := e.Evaluate("tx-1")
	if err != nil {
		t.Fatalf("Evaluate: %v", err)
	}
	mustNext(t, d2, "relay-c")
	a = findAttempt(t, d2, "relay-b")
	if a.Reason != ReasonIdentityVerificationFailed || a.Detail != "certificate subject mismatch" {
		t.Fatalf("identity failure attempt = %+v, want reason %s with preserved detail", a, ReasonIdentityVerificationFailed)
	}
	if !d2.Relay.SupportsTLS {
		t.Fatal("relay-c must be selected under the unchanged TLS requirement")
	}

	// Success terminates the transmission; no plaintext fallback ever appears.
	if err := e.ReportHandshake("tx-1", HandshakeSuccess, "tls session established"); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}
	snap, err := e.Snapshot("tx-1")
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if !snap.Established {
		t.Fatal("transmission should be marked established after a successful handshake")
	}
	d3, err := e.Evaluate("tx-1")
	if err != nil {
		t.Fatalf("Evaluate: %v", err)
	}
	mustTerminate(t, d3, ReasonEstablished)
	for _, a := range d3.Attempts {
		if a.Reason == ReasonPlaintextFallback {
			t.Fatalf("required domain must never record a plaintext fallback: %+v", a)
		}
	}
}

// ---- 2. blocked / rejected results -----------------------------------------

func TestRequiredDomainAllCandidatesRejectedTerminates(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "strict.example", TLS: TLSRequired, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	noTLS := []Relay{
		{Name: "r1", Priority: 1, SupportsTLS: false},
		{Name: "r2", Priority: 2, SupportsTLS: false},
	}
	mustEnqueue(t, e, "tx-block", "strict.example", noTLS, clock.t.Add(time.Minute))

	d, err := e.Evaluate("tx-block")
	if err != nil {
		t.Fatalf("Evaluate: %v", err)
	}
	mustTerminate(t, d, ReasonNoEligibleCandidate)
	if len(d.Attempts) != 2 {
		t.Fatalf("recorded %d attempts, want 2 (one per rejected relay)", len(d.Attempts))
	}
	for _, a := range d.Attempts {
		if a.Reason != ReasonRelayNoTLS {
			t.Fatalf("attempt %+v must be rejected with %s", a, ReasonRelayNoTLS)
		}
	}

	// A terminal transmission stays terminal on later evaluations.
	d2, err := e.Evaluate("tx-block")
	if err != nil {
		t.Fatalf("Evaluate: %v", err)
	}
	mustTerminate(t, d2, ReasonNoEligibleCandidate)
}

func TestTLSUnsupportedAtHandshakeIsRejectedWithoutDowngrade(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "strict.example", TLS: TLSRequired, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	relays := []Relay{
		{Name: "tls-a", Priority: 1, SupportsTLS: true},
		{Name: "tls-b", Priority: 2, SupportsTLS: true},
	}
	mustEnqueue(t, e, "tx", "strict.example", relays, clock.t.Add(10*time.Minute))

	d, err := e.Evaluate("tx")
	if err != nil {
		t.Fatalf("Evaluate: %v", err)
	}
	mustNext(t, d, "tls-a")
	if err := e.ReportHandshake("tx", HandshakeTLSUnsupported, "no common cipher suite"); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}
	d, err = e.Evaluate("tx")
	if err != nil {
		t.Fatalf("Evaluate: %v", err)
	}
	mustNext(t, d, "tls-b") // requirement still TLS; plaintext was not allowed
	a := findAttempt(t, d, "tls-a")
	if a.Reason != ReasonRelayNoTLS {
		t.Fatalf("got reason %s, want %s", a.Reason, ReasonRelayNoTLS)
	}
}

// ---- 3. identity failure does not downgrade, fresh round may retry ----------

func TestIdentityFailureRetriesOnlyInFreshRound(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "mail.example", TLS: TLSRequired, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	relays := []Relay{
		{Name: "tls-a", Priority: 1, SupportsTLS: true},
		{Name: "tls-b", Priority: 2, SupportsTLS: true},
	}
	mustEnqueue(t, e, "tx", "mail.example", relays, clock.t.Add(time.Hour))

	d, _ := e.Evaluate("tx")
	mustNext(t, d, "tls-a")
	if err := e.ReportHandshake("tx", HandshakeIdentityFailure, "bad cert"); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}
	d, _ = e.Evaluate("tx")
	mustNext(t, d, "tls-b")
	if err := e.ReportHandshake("tx", HandshakeIdentityFailure, "bad cert"); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}

	// Round exhausted -> defer with backoff; the failed relays are not retried
	// inside the same round.
	d, _ = e.Evaluate("tx")
	mustDefer(t, d, ReasonRoundExhausted)
	if !d.NotBefore.Equal(clock.t.Add(30 * time.Second)) {
		t.Fatalf("NotBefore = %v, want %v", d.NotBefore, clock.t.Add(30*time.Second))
	}

	clock.advance(30 * time.Second)
	d, _ = e.Evaluate("tx")
	mustNext(t, d, "tls-a")
	if d.Reason != ReasonRetryRound || d.Round != 1 {
		t.Fatalf("got reason %s round %d, want %s round 1", d.Reason, d.Round, ReasonRetryRound)
	}
}

// ---- 4. policy tightening re-checks queued, unestablished transmissions ----

func TestPolicyTighteningRestartsSelectionFromTop(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "example.org", TLS: TLSOpportunistic, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	relays := []Relay{
		{Name: "plain-a", Priority: 1, SupportsTLS: false},
		{Name: "tls-b", Priority: 2, SupportsTLS: true},
		{Name: "plain-c", Priority: 3, SupportsTLS: false},
	}
	mustEnqueue(t, e, "tx", "example.org", relays, clock.t.Add(time.Hour))

	d, _ := e.Evaluate("tx")
	mustNext(t, d, "plain-a")
	if err := e.ReportHandshake("tx", HandshakeTLSUnsupported, ""); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}
	d, _ = e.Evaluate("tx")
	a := findAttempt(t, d, "plain-a")
	if a.Reason != ReasonPlaintextFallback {
		t.Fatalf("opportunistic TLS failure should record %s, got %s", ReasonPlaintextFallback, a.Reason)
	}
	mustNext(t, d, "tls-b")
	// The selected candidate finishes its handshake; the transmission remains
	// queued and unestablished when the policy change lands.
	if err := e.ReportHandshake("tx", HandshakeConnectFailure, "transient failure"); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}

	// Tighten the domain policy while the transmission is still queued.
	if err := e.ApplyPolicy(DomainPolicy{Domain: "example.org", TLS: TLSRequired, Version: 2}); err != nil {
		t.Fatalf("ApplyPolicy tightening: %v", err)
	}
	if pending := e.PendingRechecks(); len(pending) != 1 || pending[0] != "tx" {
		t.Fatalf("PendingRechecks = %v, want [tx]", pending)
	}

	d, _ = e.Evaluate("tx")
	mustNext(t, d, "tls-b") // plain-a and plain-c cannot be selected under required TLS
	if d.Reason != ReasonPolicyRecheck {
		t.Fatalf("got reason %s, want %s", d.Reason, ReasonPolicyRecheck)
	}
	// Recheck restarts from round 0 and clears pre-recheck history. During
	// this same evaluation plain-a is re-walked and rejected under the new
	// requirement, so a fresh relay_lacks_required_tls record is expected:
	// that record is evidence the tightened policy was actually applied.
	if d.Round != 0 {
		t.Fatalf("recheck must restart at round 0; got round=%d", d.Round)
	}
	if len(d.Attempts) != 1 {
		t.Fatalf("want exactly one fresh rejection after recheck, got %+v", d.Attempts)
	}
	if d.Attempts[0].RelayName != "plain-a" || d.Attempts[0].Reason != ReasonRelayNoTLS {
		t.Fatalf("recheck rejection = %+v, want plain-a/%s", d.Attempts[0], ReasonRelayNoTLS)
	}
	snap, _ := e.Snapshot("tx")
	if snap.PolicyVersion != 2 || snap.TLSAtEnqueue != TLSOpportunistic {
		t.Fatalf("snapshot after recheck = %+v", snap)
	}
	if len(snap.Attempts) != 1 {
		t.Fatalf("pre-recheck attempt history must be cleared; got %+v", snap.Attempts)
	}

	// Version regressions must be refused.
	if err := e.ApplyPolicy(DomainPolicy{Domain: "example.org", TLS: TLSRequired, Version: 1}); err != ErrPolicyVersionRegressed {
		t.Fatalf("regressed policy version returned %v, want ErrPolicyVersionRegressed", err)
	}

	// Established transmissions are excluded from recheck bookkeeping.
	if err := e.ReportHandshake("tx", HandshakeSuccess, ""); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}
	if pending := e.PendingRechecks(); len(pending) != 0 {
		t.Fatalf("PendingRechecks after establish = %v, want empty", pending)
	}
}

func TestPolicyChangeDuringInFlightHandshakeWaitsThenRechecks(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "example.net", TLS: TLSOpportunistic, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	relays := []Relay{
		{Name: "plain-a", Priority: 1, SupportsTLS: false},
		{Name: "tls-b", Priority: 2, SupportsTLS: true},
	}
	mustEnqueue(t, e, "tx", "example.net", relays, clock.t.Add(time.Hour))

	d, _ := e.Evaluate("tx")
	mustNext(t, d, "plain-a")

	// Tightening lands while the adapter is handshaking the selected relay.
	if err := e.ApplyPolicy(DomainPolicy{Domain: "example.net", TLS: TLSRequired, Version: 2}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	d, _ = e.Evaluate("tx")
	mustDefer(t, d, ReasonHandshakeInFlight)
	if pending := e.PendingRechecks(); len(pending) != 1 {
		t.Fatalf("PendingRechecks = %v, want the in-flight transmission listed", pending)
	}

	// Once the adapter reports back, the next evaluation applies the recheck.
	if err := e.ReportHandshake("tx", HandshakeTLSUnsupported, ""); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}
	d, _ = e.Evaluate("tx")
	mustNext(t, d, "tls-b")
	if d.Reason != ReasonPolicyRecheck {
		t.Fatalf("got reason %s, want %s after in-flight recheck", d.Reason, ReasonPolicyRecheck)
	}
}

// ---- 5. deadline boundaries and backoff ------------------------------------

func TestBackoffAndDeadlineBoundary(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "d.example", TLS: TLSNone, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	relays := []Relay{
		{Name: "r1", Priority: 1, SupportsTLS: false},
		{Name: "r2", Priority: 2, SupportsTLS: false},
	}
	mustEnqueue(t, e, "tx", "d.example", relays, clock.t.Add(60*time.Second))

	d, _ := e.Evaluate("tx")
	mustNext(t, d, "r1")
	if err := e.ReportHandshake("tx", HandshakeConnectFailure, "conn refused"); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}
	d, _ = e.Evaluate("tx")
	mustNext(t, d, "r2")
	if err := e.ReportHandshake("tx", HandshakeConnectFailure, "timeout"); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}

	// Round 0 exhausted: defer until t+30s, still before the t+60s deadline.
	d, _ = e.Evaluate("tx")
	mustDefer(t, d, ReasonRoundExhausted)
	if !d.NotBefore.Equal(clock.t.Add(30 * time.Second)) {
		t.Fatalf("NotBefore = %v, want t+30s", d.NotBefore)
	}

	// Inside the backoff window the same deferral is returned.
	clock.advance(10 * time.Second)
	d, _ = e.Evaluate("tx")
	mustDefer(t, d, ReasonBackoffNotElapsed)

	// Window opens: a fresh round starts.
	clock.advance(20 * time.Second)
	d, _ = e.Evaluate("tx")
	mustNext(t, d, "r1")
	if d.Round != 1 {
		t.Fatalf("round = %d, want 1", d.Round)
	}
	if err := e.ReportHandshake("tx", HandshakeConnectFailure, ""); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}
	d, _ = e.Evaluate("tx")
	mustNext(t, d, "r2")
	if err := e.ReportHandshake("tx", HandshakeConnectFailure, ""); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}

	// The next backoff (60s) would start at t+90s, past the t+60s deadline:
	// terminate immediately instead of scheduling an unreachable retry.
	d, _ = e.Evaluate("tx")
	mustTerminate(t, d, ReasonDeadlineExceeded)
}

func TestDeadlineEqualityIsTermination(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "d.example", TLS: TLSNone, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	mustEnqueue(t, e, "tx", "d.example",
		[]Relay{{Name: "r1", Priority: 1, SupportsTLS: false}}, clock.t.Add(time.Minute))

	clock.advance(time.Minute) // now == deadline: the boundary is exclusive
	d, err := e.Evaluate("tx")
	if err != nil {
		t.Fatalf("Evaluate: %v", err)
	}
	mustTerminate(t, d, ReasonDeadlineExceeded)
}

func TestEnqueueRejectsDeadlineAtOrBeforeNow(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "d.example", TLS: TLSNone, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	err := e.Enqueue(EnqueueRequest{
		ID: "tx", Domain: "d.example", Deadline: clock.t,
		Relays: []Relay{{Name: "r1", Priority: 1}},
	})
	if err != ErrInvalidDeadline {
		t.Fatalf("got %v, want ErrInvalidDeadline", err)
	}
}

// ---- 6. opportunistic / none accept plaintext ------------------------------

func TestOpportunisticAndNoneSelectPlaintextRelays(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "opp.example", TLS: TLSOpportunistic, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	relays := []Relay{{Name: "plain", Priority: 1, SupportsTLS: false}}
	mustEnqueue(t, e, "opp", "opp.example", relays, clock.t.Add(time.Minute))
	d, _ := e.Evaluate("opp")
	mustNext(t, d, "plain")
	if d.Reason != ReasonFirstCandidate {
		t.Fatalf("reason = %s, want %s", d.Reason, ReasonFirstCandidate)
	}
	if err := e.ReportHandshake("opp", HandshakeSuccess, "plaintext accepted"); err != nil {
		t.Fatalf("ReportHandshake: %v", err)
	}
	d, _ = e.Evaluate("opp")
	mustTerminate(t, d, ReasonEstablished)

	if err := e.ApplyPolicy(DomainPolicy{Domain: "none.example", TLS: TLSNone, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	mustEnqueue(t, e, "none", "none.example", relays, clock.t.Add(time.Minute))
	d, _ = e.Evaluate("none")
	mustNext(t, d, "plain")
}

// ---- 7. validation and state-machine guard errors ---------------------------

func TestValidationAndGuardErrors(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	pol := DomainPolicy{Domain: "d.example", TLS: TLSRequired, Version: 1}
	if err := e.ApplyPolicy(pol); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	good := []Relay{{Name: "r1", Priority: 1, SupportsTLS: true}}
	goodDeadline := clock.t.Add(time.Minute)

	cases := []struct {
		name string
		req  EnqueueRequest
		want error
	}{
		{"empty id", EnqueueRequest{Domain: "d.example", Relays: good, Deadline: goodDeadline}, ErrEmptyID},
		{"empty domain", EnqueueRequest{ID: "x", Relays: good, Deadline: goodDeadline}, ErrEmptyDomain},
		{"no relays", EnqueueRequest{ID: "x", Domain: "d.example", Deadline: goodDeadline}, ErrNoRelays},
		{"bad priority", EnqueueRequest{ID: "x", Domain: "d.example", Deadline: goodDeadline,
			Relays: []Relay{{Name: "r", Priority: 0}}}, ErrInvalidPriority},
		{"duplicate relay", EnqueueRequest{ID: "x", Domain: "d.example", Deadline: goodDeadline,
			Relays: []Relay{{Name: "r", Priority: 1}, {Name: "r", Priority: 2}}}, ErrDuplicateRelay},
		{"missing policy", EnqueueRequest{ID: "x", Domain: "other.example", Relays: good, Deadline: goodDeadline}, ErrDomainPolicyNotFound},
		{"past deadline", EnqueueRequest{ID: "x", Domain: "d.example", Relays: good, Deadline: clock.t.Add(-time.Second)}, ErrInvalidDeadline},
	}
	for _, tc := range cases {
		if err := e.Enqueue(tc.req); err != tc.want {
			t.Errorf("%s: got %v, want %v", tc.name, err, tc.want)
		}
	}

	mustEnqueue(t, e, "dup", "d.example", good, goodDeadline)
	if err := e.Enqueue(EnqueueRequest{ID: "dup", Domain: "d.example", Relays: good, Deadline: goodDeadline}); err != ErrDuplicateTransmission {
		t.Errorf("duplicate id: got %v, want ErrDuplicateTransmission", err)
	}
	if _, err := e.Evaluate("unknown"); err != ErrUnknownTransmission {
		t.Errorf("Evaluate unknown: got %v, want ErrUnknownTransmission", err)
	}
	if err := e.ReportHandshake("unknown", HandshakeSuccess, ""); err != ErrUnknownTransmission {
		t.Errorf("Report unknown: got %v, want ErrUnknownTransmission", err)
	}
	if err := e.ReportHandshake("dup", HandshakeSuccess, ""); err != ErrNoActiveCandidate {
		t.Errorf("Report without active candidate: got %v, want ErrNoActiveCandidate", err)
	}
	if err := e.ReportHandshake("dup", HandshakeOutcome(99), ""); err != ErrUnknownOutcome {
		t.Errorf("bad outcome: got %v, want ErrUnknownOutcome", err)
	}
	if err := e.ApplyPolicy(DomainPolicy{Domain: "x", TLS: TLSLevel(42), Version: 1}); err != ErrInvalidTLSLevel {
		t.Errorf("bad TLS level: got %v, want ErrInvalidTLSLevel", err)
	}
	if err := e.ApplyPolicy(DomainPolicy{Domain: "", TLS: TLSNone, Version: 1}); err != ErrEmptyDomain {
		t.Errorf("empty policy domain: got %v, want ErrEmptyDomain", err)
	}
}

// ---- 8. priority ordering ---------------------------------------------------

func TestCandidatesOrderedByPriorityThenName(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "d.example", TLS: TLSNone, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	shuffled := []Relay{
		{Name: "zeta", Priority: 2},
		{Name: "alpha", Priority: 2},
		{Name: "mid", Priority: 1},
	}
	mustEnqueue(t, e, "tx", "d.example", shuffled, clock.t.Add(time.Hour))
	snap, _ := e.Snapshot("tx")
	got := []string{snap.Relays[0].Name, snap.Relays[1].Name, snap.Relays[2].Name}
	want := []string{"mid", "alpha", "zeta"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order = %v, want %v", got, want)
		}
	}
}

// ---- 9. snapshot and decision isolation ------------------------------------

func TestSnapshotsAndDecisionsAreDetachedCopies(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "d.example", TLS: TLSRequired, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	mustEnqueue(t, e, "tx", "d.example", relaySet(), clock.t.Add(time.Hour))

	d, _ := e.Evaluate("tx") // relay-a skipped, relay-b returned
	d.Relay.Name = "mutated"
	d.Attempts[0].RelayName = "mutated"

	snap, _ := e.Snapshot("tx")
	snap.Relays[0].Name = "mutated"
	snap.Attempts[0].RelayName = "mutated"

	snap2, _ := e.Snapshot("tx")
	if snap2.Relays[0].Name != "relay-a" {
		t.Fatalf("engine relay state leaked: %+v", snap2.Relays)
	}
	if snap2.Attempts[0].RelayName != "relay-a" {
		t.Fatalf("engine attempt state leaked: %+v", snap2.Attempts)
	}
	// The selected relay (relay-b) is still in flight: re-evaluation defers
	// rather than reselecting, and its payload must remain uncorrupted.
	d2, _ := e.Evaluate("tx")
	mustDefer(t, d2, ReasonHandshakeInFlight)
	snap3, _ := e.Snapshot("tx")
	if snap3.Relays[1].Name != "relay-b" {
		t.Fatalf("active candidate corrupted in engine state: got %q", snap3.Relays[1].Name)
	}
}

// ---- 10. backoff schedule ---------------------------------------------------

func TestExponentialBackoffIsCapped(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	base := 30 * time.Second
	capV := 10 * time.Minute
	// Rounds 1-5 double geometrically; round 6 would reach 16m and is capped
	// at 10m, as is every later round.
	want := []time.Duration{
		base, base * 2, base * 4, base * 8, base * 16, capV, capV, capV,
	}
	for round := 1; round <= len(want); round++ {
		if got := e.backoffFor(round); got != want[round-1] {
			t.Errorf("backoffFor(%d) = %v, want %v", round, got, want[round-1])
		}
	}
}

// ---- 11. concurrent access smoke test --------------------------------------

func TestConcurrentEvaluationDoesNotRace(t *testing.T) {
	clock := newFakeClock()
	e := testEngine(clock)
	if err := e.ApplyPolicy(DomainPolicy{Domain: "d.example", TLS: TLSRequired, Version: 1}); err != nil {
		t.Fatalf("ApplyPolicy: %v", err)
	}
	const n = 16
	for i := 0; i < n; i++ {
		id := "tx-" + string(rune('a'+i))
		mustEnqueue(t, e, id, "d.example",
			[]Relay{{Name: "tls-1", Priority: 1, SupportsTLS: true}},
			clock.t.Add(time.Hour))
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		for i := 0; i < n; i++ {
			id := "tx-" + string(rune('a'+i))
			if _, err := e.Evaluate(id); err != nil {
				t.Errorf("Evaluate(%s): %v", id, err)
				return
			}
			if err := e.ReportHandshake(id, HandshakeSuccess, ""); err != nil {
				t.Errorf("ReportHandshake(%s): %v", id, err)
				return
			}
		}
	}()
	for i := 0; i < n; i++ {
		if pending := e.PendingRechecks(); pending == nil {
			// nil and empty are both fine; just exercise the read path.
		}
	}
	<-done
}
