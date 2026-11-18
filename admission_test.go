package multicastadmission

import (
	"errors"
	"net"
	"reflect"
	"sort"
	"sync"
	"testing"
)

// ---------- test helpers ----------

func mustIP(t *testing.T, s string) net.IP {
	t.Helper()
	ip := net.ParseIP(s)
	if ip == nil {
		t.Fatalf("bad test IP %q", s)
	}
	return ip
}

func ipSet(ips []net.IP) map[string]bool {
	out := make(map[string]bool, len(ips))
	for _, ip := range ips {
		out[canonical(ip)] = true
	}
	return out
}

func assertSet(t *testing.T, name string, got []net.IP, want []string) {
	t.Helper()
	gotSet := ipSet(got)
	wantSet := make(map[string]bool, len(want))
	for _, s := range want {
		wantSet[s] = true
	}
	if !reflect.DeepEqual(gotSet, wantSet) {
		t.Errorf("%s: got %v, want %v", name, keys(gotSet), want)
	}
}

func keys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func findView(t *testing.T, r *GroupReport, terminalID string) TerminalView {
	t.Helper()
	for _, v := range r.Terminals {
		if v.TerminalID == terminalID {
			return v
		}
	}
	t.Fatalf("terminal %q has no view in report", terminalID)
	return TerminalView{}
}

func basePolicy(t *testing.T) *SegmentPolicy {
	t.Helper()
	// 10.0.0.0/24 allowed; the /32 hole is denied; 10.0.0.9 is protected.
	p, err := ParseSegmentPolicy(
		[]string{"10.0.0.0/24"},
		[]string{"10.0.0.9/32"},
		[]net.IP{mustIP(t, "10.0.0.9")},
	)
	if err != nil {
		t.Fatalf("ParseSegmentPolicy: %v", err)
	}
	return p
}

const group239 = "239.1.2.3"
const group239Other = "239.9.9.9"

// ---------- 1. acceptable result: merged upstream, per-terminal isolation ----------

func TestMergedUpstreamButTerminalIsolation(t *testing.T) {
	c := NewController()
	if err := c.SetPolicy("seg-a", basePolicy(t)); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}

	// T1 requests s1, s2. T2 (same group) requests s2, s3.
	if err := c.Join("T1", "seg-a", mustIP(t, group239),
		SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.1"), mustIP(t, "10.0.0.2")}}); err != nil {
		t.Fatalf("Join T1: %v", err)
	}
	if err := c.Join("T2", "seg-a", mustIP(t, group239),
		SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.2"), mustIP(t, "10.0.0.3")}}); err != nil {
		t.Fatalf("Join T2: %v", err)
	}

	r := c.GroupReportFor(mustIP(t, group239))
	if r == nil {
		t.Fatal("expected report for group")
	}

	// Upstream subscription is the merged union of admitted sources.
	assertSet(t, "upstream merged", r.UpstreamSources,
		[]string{"10.0.0.1", "10.0.0.2", "10.0.0.3"})

	// Each terminal only receives its own requested sources: T2 joining with
	// s3 must not widen T1's entitlement.
	v1 := findView(t, r, "T1")
	assertSet(t, "T1 allowed", v1.AllowedSources, []string{"10.0.0.1", "10.0.0.2"})
	if reason, ok := v1.Rejected["10.0.0.3"]; !ok || reason != ReasonNotRequested {
		t.Errorf("T1 must reject 10.0.0.3 as not_requested, got reason=%d ok=%v", reason, ok)
	}

	v2 := findView(t, r, "T2")
	assertSet(t, "T2 allowed", v2.AllowedSources, []string{"10.0.0.2", "10.0.0.3"})
	if reason, ok := v2.Rejected["10.0.0.1"]; !ok || reason != ReasonNotRequested {
		t.Errorf("T2 must reject 10.0.0.1 as not_requested, got reason=%d ok=%v", reason, ok)
	}
}

// ---------- 2. blocked result: emergency withdrawal overrides merged upstream ----------

func TestEmergencyWithdrawalOverridesMergedUpstream(t *testing.T) {
	c := NewController()
	if err := c.SetPolicy("seg-a", basePolicy(t)); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	protected := mustIP(t, "10.0.0.9")

	if err := c.Join("T1", "seg-a", mustIP(t, group239),
		SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.1"), protected}}); err != nil {
		t.Fatalf("Join T1: %v", err)
	}
	if err := c.Join("T2", "seg-a", mustIP(t, group239),
		SourceCondition{Include: []net.IP{protected}}); err != nil {
		t.Fatalf("Join T2: %v", err)
	}

	// Before withdrawal, note that the /32 deny prefix already blocks the
	// protected source. Tighten the policy so the source is ordinarily
	// allowed, making the withdrawal the decisive rule.
	loose, err := ParseSegmentPolicy(
		[]string{"10.0.0.0/24"}, nil, []net.IP{protected})
	if err != nil {
		t.Fatalf("ParseSegmentPolicy: %v", err)
	}
	if err := c.SetPolicy("seg-a", loose); err != nil {
		t.Fatalf("SetPolicy loose: %v", err)
	}

	r := c.GroupReportFor(mustIP(t, group239))
	assertSet(t, "upstream pre-withdrawal", r.UpstreamSources,
		[]string{"10.0.0.1", "10.0.0.9"})

	if err := c.WithdrawSource(protected); err != nil {
		t.Fatalf("WithdrawSource: %v", err)
	}
	if !c.IsWithdrawn(protected) {
		t.Error("IsWithdrawn must report true after withdrawal")
	}

	r = c.GroupReportFor(mustIP(t, group239))
	// Immediate effect on the merged upstream scope.
	assertSet(t, "upstream post-withdrawal", r.UpstreamSources, []string{"10.0.0.1"})

	for _, id := range []string{"T1", "T2"} {
		v := findView(t, r, id)
		if reason, ok := v.Rejected["10.0.0.9"]; !ok || reason != ReasonWithdrawn {
			t.Errorf("%s: protected source must be rejected as withdrawn, got reason=%d ok=%v", id, reason, ok)
		}
		for _, ip := range v.AllowedSources {
			if canonical(ip) == "10.0.0.9" {
				t.Errorf("%s must not receive withdrawn source", id)
			}
		}
	}

	// A non-protected source cannot be withdrawn: the operation is refused and
	// changes nothing.
	ordinary := mustIP(t, "10.0.0.1")
	if err := c.WithdrawSource(ordinary); !errors.Is(err, ErrNotProtected) {
		t.Fatalf("withdrawing unprotected source: got %v, want ErrNotProtected", err)
	}
	r = c.GroupReportFor(mustIP(t, group239))
	assertSet(t, "upstream unchanged after refused withdrawal", r.UpstreamSources,
		[]string{"10.0.0.1"})

	// Restore returns the source to ordinary rule evaluation.
	if err := c.RestoreSource(protected); err != nil {
		t.Fatalf("RestoreSource: %v", err)
	}
	r = c.GroupReportFor(mustIP(t, group239))
	assertSet(t, "upstream restored", r.UpstreamSources,
		[]string{"10.0.0.1", "10.0.0.9"})
}

// ---------- 3. boundary: CIDR edges, deny wins over allow, empty allow fails closed ----------

func TestSegmentPolicyBoundaryAndDenyPrecedence(t *testing.T) {
	c := NewController()
	policy, err := ParseSegmentPolicy(
		[]string{"10.10.0.0/24"},
		[]string{"10.10.0.200/32"},
		nil)
	if err != nil {
		t.Fatalf("ParseSegmentPolicy: %v", err)
	}
	if err := c.SetPolicy("seg-b", policy); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}

	insideFirst := mustIP(t, "10.10.0.0")  // first address of the /24
	insideLast := mustIP(t, "10.10.0.255") // last address of the /24
	outside := mustIP(t, "10.10.1.0")      // just outside
	denied := mustIP(t, "10.10.0.200")     // inside the allow prefix but denied

	if err := c.Join("T1", "seg-b", mustIP(t, group239), SourceCondition{
		Include: []net.IP{insideFirst, insideLast, outside, denied},
	}); err != nil {
		t.Fatalf("Join: %v", err)
	}

	r := c.GroupReportFor(mustIP(t, group239))
	assertSet(t, "boundary allowed", r.UpstreamSources,
		[]string{"10.10.0.0", "10.10.0.255"})

	v := findView(t, r, "T1")
	if v.Rejected["10.10.1.0"] != ReasonNotInAllowedSegment {
		t.Errorf("outside prefix: got %v, want not_in_allowed_segment", v.Rejected["10.10.1.0"])
	}
	if v.Rejected["10.10.0.200"] != ReasonSegmentDenied {
		t.Errorf("denied /32 must win over allow: got %v, want segment_denied", v.Rejected["10.10.0.200"])
	}

	// Empty allow list fails closed: even an explicitly requested source is
	// never admitted.
	closed, err := ParseSegmentPolicy(nil, nil, nil)
	if err != nil {
		t.Fatalf("ParseSegmentPolicy closed: %v", err)
	}
	if err := c.SetPolicy("seg-b", closed); err != nil {
		t.Fatalf("SetPolicy closed: %v", err)
	}
	r = c.GroupReportFor(mustIP(t, group239))
	if len(r.UpstreamSources) != 0 {
		t.Errorf("fail-closed policy must produce empty upstream, got %v", r.UpstreamSources)
	}
	v = findView(t, r, "T1")
	for _, reason := range v.Rejected {
		if reason != ReasonNotInAllowedSegment {
			t.Errorf("fail-closed: every candidate must be not_in_allowed_segment, got %v", reason)
		}
	}
}

// ---------- 4. interleaving: Update and Leave only affect the owning session ----------

func TestUpdateAndLeaveScope(t *testing.T) {
	c := NewController()
	if err := c.SetPolicy("seg-a", basePolicy(t)); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	if err := c.Join("T1", "seg-a", mustIP(t, group239),
		SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.1")}}); err != nil {
		t.Fatalf("Join T1: %v", err)
	}
	if err := c.Join("T2", "seg-a", mustIP(t, group239),
		SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.2")}}); err != nil {
		t.Fatalf("Join T2: %v", err)
	}

	// Update T1: swap its source. T2 must remain untouched.
	if err := c.Update("T1", mustIP(t, group239),
		SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.3")}}); err != nil {
		t.Fatalf("Update: %v", err)
	}
	r := c.GroupReportFor(mustIP(t, group239))
	assertSet(t, "upstream after update", r.UpstreamSources,
		[]string{"10.0.0.2", "10.0.0.3"})
	v1 := findView(t, r, "T1")
	assertSet(t, "T1 after update", v1.AllowedSources, []string{"10.0.0.3"})
	if _, stillRejected := v1.Rejected["10.0.0.1"]; stillRejected {
		t.Error("dropped source 10.0.0.1 should no longer be a candidate for T1 after update")
	}
	v2 := findView(t, r, "T2")
	assertSet(t, "T2 unaffected by update", v2.AllowedSources, []string{"10.0.0.2"})

	// An update targeting another group must be rejected atomically.
	if err := c.Update("T1", mustIP(t, group239Other),
		SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.4")}}); !errors.Is(err, ErrGroupMismatch) {
		t.Fatalf("cross-group update: got %v, want ErrGroupMismatch", err)
	}
	r = c.GroupReportFor(mustIP(t, group239))
	v1 = findView(t, r, "T1")
	assertSet(t, "T1 unchanged after rejected update", v1.AllowedSources, []string{"10.0.0.3"})

	// Leave T2: its source disappears from the merged upstream; T1 keeps its
	// own entitlement.
	if err := c.Leave("T2"); err != nil {
		t.Fatalf("Leave: %v", err)
	}
	r = c.GroupReportFor(mustIP(t, group239))
	assertSet(t, "upstream after leave", r.UpstreamSources, []string{"10.0.0.3"})
	if len(r.Terminals) != 1 || r.Terminals[0].TerminalID != "T1" {
		t.Errorf("only T1 should remain, got %d terminals", len(r.Terminals))
	}
}

// ---------- 5. invalid / refused inputs ----------

func TestInvalidOperationsRejected(t *testing.T) {
	c := NewController()
	if err := c.SetPolicy("seg-a", basePolicy(t)); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	g := mustIP(t, group239)
	cond := SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.1")}}

	if err := c.Join("", "seg-a", g, cond); !errors.Is(err, ErrEmptyTerminalID) {
		t.Errorf("empty id: got %v", err)
	}
	if err := c.Join("X", "seg-a", mustIP(t, "10.0.0.1"), cond); !errors.Is(err, ErrNotMulticast) {
		t.Errorf("non-multicast group: got %v", err)
	}
	if err := c.Join("X", "missing-seg", g, cond); !errors.Is(err, ErrUnknownPolicy) {
		t.Errorf("unknown segment: got %v", err)
	}
	if err := c.Join("X", "seg-a", g, SourceCondition{Include: []net.IP{nil}}); !errors.Is(err, ErrNilSource) {
		t.Errorf("nil source: got %v", err)
	}
	if err := c.Update("ghost", g, cond); !errors.Is(err, ErrSessionNotFound) {
		t.Errorf("update unknown session: got %v", err)
	}
	if err := c.Leave("ghost"); !errors.Is(err, ErrSessionNotFound) {
		t.Errorf("leave unknown session: got %v", err)
	}
	if _, err := ParseSegmentPolicy([]string{"not-a-cidr"}, nil, nil); !errors.Is(err, ErrInvalidPrefix) {
		t.Errorf("bad cidr: got %v", err)
	}

	if err := c.Join("X", "seg-a", g, cond); err != nil {
		t.Fatalf("Join X: %v", err)
	}
	if err := c.Join("X", "seg-a", g, cond); !errors.Is(err, ErrSessionExists) {
		t.Errorf("duplicate join: got %v, want ErrSessionExists", err)
	}
}

// ---------- 6. group isolation: independent multicast groups never merge ----------

func TestDistinctGroupsStayIndependent(t *testing.T) {
	c := NewController()
	if err := c.SetPolicy("seg-a", basePolicy(t)); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	if err := c.Join("T1", "seg-a", mustIP(t, group239),
		SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.1")}}); err != nil {
		t.Fatalf("Join T1: %v", err)
	}
	if err := c.Join("T2", "seg-a", mustIP(t, group239Other),
		SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.2")}}); err != nil {
		t.Fatalf("Join T2: %v", err)
	}

	reports := c.Report()
	if len(reports) != 2 {
		t.Fatalf("expected 2 group reports, got %d", len(reports))
	}
	r1 := c.GroupReportFor(mustIP(t, group239))
	r2 := c.GroupReportFor(mustIP(t, group239Other))
	assertSet(t, "group 1 upstream", r1.UpstreamSources, []string{"10.0.0.1"})
	assertSet(t, "group 2 upstream", r2.UpstreamSources, []string{"10.0.0.2"})
	if len(r1.Terminals) != 1 || r1.Terminals[0].TerminalID != "T1" {
		t.Error("group 1 must contain only T1")
	}
	if len(r2.Terminals) != 1 || r2.Terminals[0].TerminalID != "T2" {
		t.Error("group 2 must contain only T2")
	}
}

// ---------- 7. returned state is a defensive copy ----------

func TestReportReturnsDefensiveCopies(t *testing.T) {
	c := NewController()
	if err := c.SetPolicy("seg-a", basePolicy(t)); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}
	src := mustIP(t, "10.0.0.1")
	if err := c.Join("T1", "seg-a", mustIP(t, group239),
		SourceCondition{Include: []net.IP{src}}); err != nil {
		t.Fatalf("Join: %v", err)
	}

	r := c.GroupReportFor(mustIP(t, group239))
	r.UpstreamSources[0] = mustIP(t, "192.0.2.255")
	r.Terminals[0].AllowedSources[0] = mustIP(t, "192.0.2.254")
	r.Terminals[0].Rejected["tampered"] = ReasonWithdrawn

	again := c.GroupReportFor(mustIP(t, group239))
	assertSet(t, "internal upstream unchanged after mutation", again.UpstreamSources,
		[]string{"10.0.0.1"})
	v := findView(t, again, "T1")
	assertSet(t, "internal allowed unchanged after mutation", v.AllowedSources,
		[]string{"10.0.0.1"})
	if _, ok := v.Rejected["tampered"]; ok {
		t.Error("internal rejected map must not be mutable through the returned copy")
	}
}

// ---------- 8. concurrent interleaving stays consistent ----------

func TestConcurrentInterleaving(t *testing.T) {
	c := NewController()
	protected := mustIP(t, "10.0.0.9")
	loose, err := ParseSegmentPolicy(
		[]string{"10.0.0.0/24"}, nil, []net.IP{protected})
	if err != nil {
		t.Fatalf("ParseSegmentPolicy: %v", err)
	}
	if err := c.SetPolicy("seg-a", loose); err != nil {
		t.Fatalf("SetPolicy: %v", err)
	}

	const n = 12
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		id := "T" + string(rune('A'+i))
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := c.Join(id, "seg-a", mustIP(t, group239),
				SourceCondition{Include: []net.IP{mustIP(t, "10.0.0.1"), protected}}); err != nil {
				t.Errorf("Join %s: %v", id, err)
				return
			}
			_ = c.Report()
			if err := c.Leave(id); err != nil {
				t.Errorf("Leave %s: %v", id, err)
			}
			_ = c.Report()
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < 20; i++ {
			_ = c.WithdrawSource(protected)
			_ = c.RestoreSource(protected)
		}
	}()
	wg.Wait()

	// Every session left: no group reports remain, and the controller is
	// internally consistent.
	if len(c.Report()) != 0 {
		t.Errorf("expected no remaining groups after all leaves, got %d", len(c.Report()))
	}
}
