package l2switch_test

import (
	"errors"
	"testing"

	"l2switch"
)

// Line-side labels used by the fixtures.
const (
	vlan10  l2switch.Label = 10
	vlan100 l2switch.Label = 100
	vlan200 l2switch.Label = 200
	vlan20  l2switch.Label = 20
)

const (
	bdA l2switch.BridgeDomain = "BD-A" // cabinet 1 service
	bdB l2switch.BridgeDomain = "BD-B" // cabinet 2 service
	bdX l2switch.BridgeDomain = "BD-X"
	bdY l2switch.BridgeDomain = "BD-Y"
	bd  l2switch.BridgeDomain = "BD"
)

const (
	pCab1 l2switch.PortID = "cab1"
	pCab2 l2switch.PortID = "cab2"
	pAgg1 l2switch.PortID = "agg1"
	pAgg2 l2switch.PortID = "agg2"
	pA    l2switch.PortID = "pA"
	pB    l2switch.PortID = "pB"
	pC    l2switch.PortID = "pC"
	pP    l2switch.PortID = "pP"
	pQ    l2switch.PortID = "pQ"
	pR    l2switch.PortID = "pR"
)

var (
	macH1   = l2switch.MustParseMAC("02:00:00:00:00:01")
	macH2   = l2switch.MustParseMAC("02:00:00:00:00:02")
	macH3   = l2switch.MustParseMAC("02:00:00:00:00:03")
	macH4   = l2switch.MustParseMAC("02:00:00:00:00:04")
	macMove = l2switch.MustParseMAC("02:aa:bb:cc:dd:ee")
	macZ    = l2switch.MustParseMAC("02:00:00:00:00:99")
)

func labeled(p l2switch.PortID, l l2switch.Label, src, dst l2switch.MAC, payload string) l2switch.Frame {
	return l2switch.Frame{Ingress: p, InLabel: l, HasLabel: true, SrcMAC: src, DstMAC: dst, Payload: payload}
}

func untagged(p l2switch.PortID, src, dst l2switch.MAC, payload string) l2switch.Frame {
	return l2switch.Frame{Ingress: p, HasLabel: false, SrcMAC: src, DstMAC: dst, Payload: payload}
}

func recv(t *testing.T, sw *l2switch.Switch, f l2switch.Frame) []l2switch.EgressFrame {
	t.Helper()
	out, err := sw.Receive(f)
	if err != nil {
		t.Fatalf("Receive on %s port %s returned unexpected error: %v", sw.Name(), f.Ingress, err)
	}
	return out
}

func assertEgressPorts(t *testing.T, out []l2switch.EgressFrame, want ...l2switch.PortID) {
	t.Helper()
	got := make(map[l2switch.PortID]bool, len(out))
	for _, ef := range out {
		if got[ef.Egress] {
			t.Errorf("duplicate egress frame from port %s", ef.Egress)
		}
		got[ef.Egress] = true
	}
	if len(got) != len(want) {
		t.Fatalf("egress port set = %v (count %d), want %v (count %d)", keys(got), len(got), want, len(want))
	}
	for _, p := range want {
		if !got[p] {
			t.Errorf("expected egress from %s, got ports %v", p, keys(got))
		}
	}
}

func keys(m map[l2switch.PortID]bool) []l2switch.PortID {
	out := make([]l2switch.PortID, 0, len(m))
	for p := range m {
		out = append(out, p)
	}
	return out
}

func frameFrom(out []l2switch.EgressFrame, p l2switch.PortID) (l2switch.EgressFrame, bool) {
	for _, ef := range out {
		if ef.Egress == p {
			return ef, true
		}
	}
	return l2switch.EgressFrame{}, false
}

// newAggregation builds the title scenario:
// two cabinets keep VLAN 10 for their own services; the aggregation switch
// maps them into different bridge domains with different uplink labels.
//
//	cab1: VLAN10 <-> BD-A <-> VLAN100 on agg1
//	cab2: VLAN10 <-> BD-B <-> VLAN200 on agg2
//
// All ports start in the default blocking state.
func newAggregation(t *testing.T) *l2switch.Switch {
	t.Helper()
	sw := l2switch.NewSwitch("aggregation")
	for _, p := range []l2switch.PortID{pCab1, pCab2, pAgg1, pAgg2} {
		sw.AddPort(p)
	}
	must(t, sw.SetIngressMapping(pCab1, vlan10, true, bdA))
	must(t, sw.SetIngressMapping(pAgg1, vlan100, true, bdA))
	must(t, sw.SetIngressMapping(pCab2, vlan10, true, bdB))
	must(t, sw.SetIngressMapping(pAgg2, vlan200, true, bdB))
	return sw
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected configuration error: %v", err)
	}
}

// newThreePortBD builds one bridge domain with three symmetric members, all
// using VLAN 10. Ports start blocking; the caller sets states.
func newThreePortBD(t *testing.T) *l2switch.Switch {
	sw := l2switch.NewSwitch("three-port")
	for _, p := range []l2switch.PortID{pA, pB, pC} {
		sw.AddPort(p)
		if err := sw.SetIngressMapping(p, vlan10, true, bd); err != nil {
			t.Fatalf("setup mapping on %s: %v", p, err)
		}
	}
	return sw
}

// TestUnknownUnicastFloodsWithMappedEgressLabel covers the normal path: an
// unknown destination is flooded inside the bridge domain and emitted with
// the egress port's own line-side label (VLAN 10 -> VLAN 100), never back to
// the ingress port.
func TestUnknownUnicastFloodsWithMappedEgressLabel(t *testing.T) {
	sw := newAggregation(t)
	must(t, sw.SetState(pCab1, bdA, l2switch.StateForwarding))
	must(t, sw.SetState(pAgg1, bdA, l2switch.StateForwarding))

	out := recv(t, sw, labeled(pCab1, vlan10, macH1, macH2, "first-arp"))

	assertEgressPorts(t, out, pAgg1)
	ef, ok := frameFrom(out, pAgg1)
	if !ok {
		t.Fatal("no frame emitted from agg1")
	}
	if !ef.HasLabel || ef.OutLabel != vlan100 {
		t.Errorf("agg1 egress label = (present=%v, value=%d), want VLAN 100", ef.HasLabel, ef.OutLabel)
	}
	if ef.SrcMAC != macH1 || ef.DstMAC != macH2 || ef.Payload != "first-arp" {
		t.Errorf("egress frame content altered: %+v", ef)
	}
	if learned, ok := sw.LookupLearned(bdA, macH1); !ok || learned != pCab1 {
		t.Errorf("source MAC learned at %v (hit=%v), want cab1", learned, ok)
	}
	if _, hit := sw.LookupLearned(bdB, macH1); hit {
		t.Error("frame of BD-A must not create learning entries in BD-B")
	}
}

// TestKnownUnicastTriesOnlyLearnedEgress shows that after learning, a
// unicast hit uses exactly one egress port instead of flooding.
func TestKnownUnicastTriesOnlyLearnedEgress(t *testing.T) {
	sw := newThreePortBD(t)
	for _, p := range []l2switch.PortID{pA, pB, pC} {
		must(t, sw.SetState(p, bd, l2switch.StateForwarding))
	}

	// Unknown H2: flooded to both other forwarding ports.
	out := recv(t, sw, labeled(pA, vlan10, macH1, macH2, "discover"))
	assertEgressPorts(t, out, pB, pC)

	// H2 speaks from pB and is learned there.
	recv(t, sw, labeled(pB, vlan10, macH2, macH1, "reply"))

	// Now a frame to H2 must leave via pB only.
	out = recv(t, sw, labeled(pA, vlan10, macH1, macH2, "unicast"))
	assertEgressPorts(t, out, pB)
	if ef, _ := frameFrom(out, pB); !ef.HasLabel || ef.OutLabel != vlan10 {
		t.Errorf("egress label on pB = (present=%v, value=%d), want VLAN 10", ef.HasLabel, ef.OutLabel)
	}
}

// TestSameVlanTenDifferentBridgeDomainsDoNotLeak covers the core isolation
// requirement: VLAN 10 from the two cabinets maps to different bridge
// domains, so frames never cross between them.
func TestSameVlanTenDifferentBridgeDomainsDoNotLeak(t *testing.T) {
	sw := newAggregation(t)
	must(t, sw.SetState(pCab1, bdA, l2switch.StateForwarding))
	must(t, sw.SetState(pAgg1, bdA, l2switch.StateForwarding))
	must(t, sw.SetState(pCab2, bdB, l2switch.StateForwarding))
	must(t, sw.SetState(pAgg2, bdB, l2switch.StateForwarding))

	// Cabinet 1 frame aimed at a MAC that only exists in cabinet 2's domain.
	outA := recv(t, sw, labeled(pCab1, vlan10, macH1, macH3, "cab1-traffic"))
	assertEgressPorts(t, outA, pAgg1)
	if _, leaked := frameFrom(outA, pAgg2); leaked {
		t.Error("BD-A frame leaked onto the BD-B uplink agg2")
	}
	if _, leaked := frameFrom(outA, pCab2); leaked {
		t.Error("BD-A frame leaked onto cabinet 2 access port")
	}

	// Cabinet 2 VLAN 10 stays inside BD-B and gets VLAN 200 on the wire.
	outB := recv(t, sw, labeled(pCab2, vlan10, macH3, macH4, "cab2-traffic"))
	assertEgressPorts(t, outB, pAgg2)
	ef, _ := frameFrom(outB, pAgg2)
	if !ef.HasLabel || ef.OutLabel != vlan200 {
		t.Errorf("agg2 egress label = (present=%v, value=%d), want VLAN 200", ef.HasLabel, ef.OutLabel)
	}
}

// TestBlockingDropsIngressClearsLearningAndExcludesEgress covers three
// blocking rules: blocking ingress drops before learning, a blocked port is
// excluded from flooding, and transitioning to blocking removes the port's
// learned entries in that bridge domain.
func TestBlockingDropsIngressClearsLearningAndExcludesEgress(t *testing.T) {
	sw := newThreePortBD(t)
	must(t, sw.SetState(pA, bd, l2switch.StateBlocking))
	must(t, sw.SetState(pB, bd, l2switch.StateForwarding))
	must(t, sw.SetState(pC, bd, l2switch.StateForwarding))

	// Ingress blocking: dropped, nothing learned.
	if out := recv(t, sw, labeled(pA, vlan10, macH1, macH2, "x")); len(out) != 0 {
		t.Fatalf("blocking ingress emitted frames: %+v", out)
	}
	if _, hit := sw.LookupLearned(bd, macH1); hit {
		t.Error("blocking ingress must not learn source MACs")
	}

	// Flood from pB must skip blocked pA and only reach pC.
	out := recv(t, sw, labeled(pB, vlan10, macH2, macH3, "flood"))
	assertEgressPorts(t, out, pC)

	// Bring pA up, learn a MAC, then block it: the entry must be purged.
	must(t, sw.SetState(pA, bd, l2switch.StateForwarding))
	recv(t, sw, labeled(pA, vlan10, macH1, macH3, "learn-me"))
	if learned, hit := sw.LookupLearned(bd, macH1); !hit || learned != pA {
		t.Fatalf("expected H1 learned at pA, got port %v hit=%v", learned, hit)
	}
	must(t, sw.SetState(pA, bd, l2switch.StateBlocking))
	if _, hit := sw.LookupLearned(bd, macH1); hit {
		t.Error("transitioning pA to blocking must remove its learned MAC in this bridge domain")
	}
}

// TestLearnOnlyUpdatesSourceMacButNeverForwards verifies the learn-only
// state: the source MAC is learned, but no frame is emitted, and a later
// unicast hit toward that port is suppressed until it becomes forwarding.
func TestLearnOnlyUpdatesSourceMacButNeverForwards(t *testing.T) {
	sw := newThreePortBD(t)
	must(t, sw.SetState(pA, bd, l2switch.StateForwarding))
	must(t, sw.SetState(pB, bd, l2switch.StateLearnOnly))
	must(t, sw.SetState(pC, bd, l2switch.StateForwarding))

	if out := recv(t, sw, labeled(pB, vlan10, macH2, macH1, "silent")); len(out) != 0 {
		t.Fatalf("learn-only ingress must not forward, got %+v", out)
	}
	learned, hit := sw.LookupLearned(bd, macH2)
	if !hit || learned != pB {
		t.Fatalf("learn-only port must still learn its source MAC, got %v hit=%v", learned, hit)
	}

	// Unicast hit to a non-forwarding egress: no emission.
	if out := recv(t, sw, labeled(pA, vlan10, macH1, macH2, "held")); len(out) != 0 {
		t.Fatalf("frame toward a learn-only egress port must be suppressed, got %+v", out)
	}

	// Once forwarding, the same unicast flows with the port's label.
	must(t, sw.SetState(pB, bd, l2switch.StateForwarding))
	out := recv(t, sw, labeled(pA, vlan10, macH1, macH2, "now-flowing"))
	assertEgressPorts(t, out, pB)
}

// TestMacMoveReroutesImmediately requires that the same source MAC appearing
// on a new port overwrites the old entry and traffic is rerouted at once.
func TestMacMoveReroutesImmediately(t *testing.T) {
	sw := newThreePortBD(t)
	for _, p := range []l2switch.PortID{pA, pB, pC} {
		must(t, sw.SetState(p, bd, l2switch.StateForwarding))
	}

	recv(t, sw, labeled(pA, vlan10, macMove, macH2, "from-A"))
	if learned, _ := sw.LookupLearned(bd, macMove); learned != pA {
		t.Fatalf("expected MAC initially at pA, got %v", learned)
	}

	// The host migrates to pC: learning must immediately overwrite.
	recv(t, sw, labeled(pC, vlan10, macMove, macH2, "from-C"))
	if learned, _ := sw.LookupLearned(bd, macMove); learned != pC {
		t.Fatalf("expected MAC rerouted to pC, got %v", learned)
	}

	// Traffic from pB toward the moved MAC must now leave via pC only.
	out := recv(t, sw, labeled(pB, vlan10, macH2, macMove, "chased"))
	assertEgressPorts(t, out, pC)
}

// TestStateChangeInOneBridgeDomainKeepsSameMacPathInOther is the interleaved
// scenario from the statement: the same MAC is known in two bridge domains;
// changing one port's state in one domain must not disturb the other domain's
// forwarding path or its learned entry.
func TestStateChangeInOneBridgeDomainKeepsSameMacPathInOther(t *testing.T) {
	sw := newAggregation(t)
	must(t, sw.SetState(pCab1, bdA, l2switch.StateForwarding))
	must(t, sw.SetState(pAgg1, bdA, l2switch.StateForwarding))
	must(t, sw.SetState(pCab2, bdB, l2switch.StateForwarding))
	must(t, sw.SetState(pAgg2, bdB, l2switch.StateForwarding))

	// The same MAC shows up behind each cabinet, in separate bridge domains.
	recv(t, sw, labeled(pCab1, vlan10, macMove, macH2, "domain-A"))
	recv(t, sw, labeled(pCab2, vlan10, macMove, macH4, "domain-B"))
	if l, _ := sw.LookupLearned(bdA, macMove); l != pCab1 {
		t.Fatalf("BD-A should learn shared MAC at cab1, got %v", l)
	}
	if l, _ := sw.LookupLearned(bdB, macMove); l != pCab2 {
		t.Fatalf("BD-B should learn shared MAC at cab2, got %v", l)
	}

	// Block cabinet 1 inside BD-A only.
	must(t, sw.SetState(pCab1, bdA, l2switch.StateBlocking))
	if _, hit := sw.LookupLearned(bdA, macMove); hit {
		t.Error("blocking cab1 in BD-A must purge its BD-A learning entry")
	}
	if l, hit := sw.LookupLearned(bdB, macMove); !hit || l != pCab2 {
		t.Fatalf("BD-B learning entry must survive, got port %v hit=%v", l, hit)
	}
	if st, _ := sw.State(pCab2, bdB); st != l2switch.StateForwarding {
		t.Errorf("cab2 BD-B state changed unexpectedly: %s", st)
	}

	// BD-B still forwards to the shared MAC along its original path,
	// rewriting the uplink label back to line-side VLAN 10 at cab2.
	outB := recv(t, sw, labeled(pAgg2, vlan200, macZ, macMove, "domain-B-unicast"))
	assertEgressPorts(t, outB, pCab2)
	if ef, _ := frameFrom(outB, pCab2); !ef.HasLabel || ef.OutLabel != vlan10 {
		t.Errorf("cab2 egress label = (present=%v, value=%d), want VLAN 10", ef.HasLabel, ef.OutLabel)
	}

	// In BD-A the MAC is now unknown and the only other member is blocked,
	// so nothing is emitted.
	outA := recv(t, sw, labeled(pAgg1, vlan100, macZ, macMove, "domain-A-unknown"))
	if len(outA) != 0 {
		t.Errorf("BD-A should have no usable egress after block, got %+v", outA)
	}
}

// TestPortStateIsIndependentPerBridgeDomain puts one physical port in two
// bridge domains with different states and checks both ingress behaviors and
// that changing one state leaves the other intact.
func TestPortStateIsIndependentPerBridgeDomain(t *testing.T) {
	sw := l2switch.NewSwitch("multi-domain")
	for _, p := range []l2switch.PortID{pP, pQ} {
		sw.AddPort(p)
		must(t, sw.SetIngressMapping(p, vlan10, true, bdX))
		must(t, sw.SetIngressMapping(p, vlan20, true, bdY))
	}
	must(t, sw.SetState(pP, bdX, l2switch.StateBlocking))
	must(t, sw.SetState(pP, bdY, l2switch.StateForwarding))
	must(t, sw.SetState(pQ, bdX, l2switch.StateForwarding))
	must(t, sw.SetState(pQ, bdY, l2switch.StateForwarding))

	// Label 10 -> BD-X: pP is blocking, frame is dropped.
	if out := recv(t, sw, labeled(pP, vlan10, macH1, macH2, "x-blocked")); len(out) != 0 {
		t.Fatalf("blocked BD-X ingress emitted frames: %+v", out)
	}
	// Label 20 -> BD-Y: pP is forwarding, floods to pQ with VLAN 20.
	out := recv(t, sw, labeled(pP, vlan20, macH1, macH2, "y-flowing"))
	assertEgressPorts(t, out, pQ)
	if ef, _ := frameFrom(out, pQ); !ef.HasLabel || ef.OutLabel != vlan20 {
		t.Errorf("pQ BD-Y egress label = (present=%v, value=%d), want VLAN 20", ef.HasLabel, ef.OutLabel)
	}

	// Move pP in BD-X to learn-only: learning works, forwarding still does
	// not, while BD-Y remains fully forwarding.
	must(t, sw.SetState(pP, bdX, l2switch.StateLearnOnly))
	recv(t, sw, labeled(pP, vlan10, macH3, macH4, "x-learning"))
	if l, hit := sw.LookupLearned(bdX, macH3); !hit || l != pP {
		t.Fatalf("learn-only BD-X should learn H3 at pP, got %v hit=%v", l, hit)
	}
	if st, _ := sw.State(pP, bdY); st != l2switch.StateForwarding {
		t.Fatalf("BD-Y state on pP must remain forwarding, got %s", st)
	}
	out = recv(t, sw, labeled(pP, vlan20, macH1, macH2, "y-still-flowing"))
	assertEgressPorts(t, out, pQ)
}

// TestUntaggedFramesAndUntaggedEgressAreValidMappings proves that "no label"
// is a first-class mapping value on both ingress and egress.
func TestUntaggedFramesAndUntaggedEgressAreValidMappings(t *testing.T) {
	sw := l2switch.NewSwitch("untagged")
	for _, p := range []l2switch.PortID{pP, pQ, pR} {
		sw.AddPort(p)
	}
	// pP receives untagged traffic into the domain.
	must(t, sw.SetIngressMapping(pP, 0, false, bd))
	// q and r use tagged VLAN 10 ingress.
	must(t, sw.SetIngressMapping(pQ, vlan10, true, bd))
	must(t, sw.SetIngressMapping(pR, vlan10, true, bd))
	// q emits untagged, r keeps the tagged egress label.
	must(t, sw.SetEgressMapping(pQ, bd, 0, false))
	for _, p := range []l2switch.PortID{pP, pQ, pR} {
		must(t, sw.SetState(p, bd, l2switch.StateForwarding))
	}

	out := recv(t, sw, untagged(pP, macH1, macH2, "no-label"))
	assertEgressPorts(t, out, pQ, pR)

	qf, ok := frameFrom(out, pQ)
	if !ok {
		t.Fatal("missing egress frame on pQ")
	}
	if qf.HasLabel {
		t.Errorf("pQ should emit an untagged frame, got label %d", qf.OutLabel)
	}
	rf, _ := frameFrom(out, pR)
	if !rf.HasLabel || rf.OutLabel != vlan10 {
		t.Errorf("pR should emit VLAN 10, got present=%v value=%d", rf.HasLabel, rf.OutLabel)
	}
	if l, _ := sw.LookupLearned(bd, macH1); l != pP {
		t.Errorf("untagged ingress should still learn source MAC, got %v", l)
	}
}

// TestBroadcastFloodAndHairpinDrop covers boundary behavior: broadcast never
// returns to the ingress, and a unicast whose learned egress is the ingress
// itself is dropped rather than looped back.
func TestBroadcastFloodAndHairpinDrop(t *testing.T) {
	sw := newThreePortBD(t)
	for _, p := range []l2switch.PortID{pA, pB, pC} {
		must(t, sw.SetState(p, bd, l2switch.StateForwarding))
	}

	out := recv(t, sw, labeled(pA, vlan10, macH1, l2switch.BroadcastMAC, "bcast"))
	assertEgressPorts(t, out, pB, pC)
	for _, ef := range out {
		if ef.DstMAC != l2switch.BroadcastMAC {
			t.Errorf("broadcast destination rewritten unexpectedly: %s", ef.DstMAC)
		}
	}

	// H1 is learned at pA; a frame sourced from pA targeting its own
	// learned entry must not be emitted back out of pA.
	recv(t, sw, labeled(pA, vlan10, macH1, macH2, "learn-self"))
	if out := recv(t, sw, labeled(pA, vlan10, macH3, macH1, "hairpin")); len(out) != 0 {
		t.Fatalf("frame whose egress equals ingress must be dropped, got %+v", out)
	}

	// The same destination reached from another port exits normally at pA.
	out = recv(t, sw, labeled(pB, vlan10, macH2, macH1, "from-b"))
	assertEgressPorts(t, out, pA)
}

// TestMACParsingAndString exercises the MAC parser, its error paths and the
// canonical string form used in diagnostics.
func TestMACParsingAndString(t *testing.T) {
	m, err := l2switch.ParseMAC("aa:bb:cc:dd:ee:ff")
	if err != nil {
		t.Fatalf("ParseMAC returned unexpected error: %v", err)
	}
	want := l2switch.MAC{0xaa, 0xbb, 0xcc, 0xdd, 0xee, 0xff}
	if m != want {
		t.Errorf("parsed MAC = %v, want %v", m, want)
	}
	if got := m.String(); got != "aa:bb:cc:dd:ee:ff" {
		t.Errorf("MAC.String() = %q, want canonical lowercase form", got)
	}
	if l2switch.BroadcastMAC.String() != "ff:ff:ff:ff:ff:ff" {
		t.Errorf("broadcast MAC renders incorrectly: %s", l2switch.BroadcastMAC)
	}

	bad := []string{"", "short", "gg:00:00:00:00:00", "aa-bb-cc-dd-ee-ff", "aa:bb:cc:dd:ee:ff:00"}
	for _, in := range bad {
		if _, err := l2switch.ParseMAC(in); err == nil {
			t.Errorf("ParseMAC(%q) expected an error, got nil", in)
		}
	}

	// MustParseMAC on valid input equals ParseMAC; on bad input it panics.
	if got := l2switch.MustParseMAC("00:11:22:33:44:55"); got == (l2switch.MAC{}) {
		t.Error("MustParseMAC returned zero MAC for a valid input")
	}
	func() {
		defer func() {
			if recover() == nil {
				t.Error("MustParseMAC on malformed input must panic")
			}
		}()
		_ = l2switch.MustParseMAC("not-a-mac")
	}()
}

// TestUnknownPortConfigurationErrors verifies that mapping calls on ports
// that were never added fail with the documented sentinel.
func TestUnknownPortConfigurationErrors(t *testing.T) {
	sw := l2switch.NewSwitch("errors")
	if err := sw.SetIngressMapping(pA, vlan10, true, bd); !errors.Is(err, l2switch.ErrUnknownPort) {
		t.Errorf("SetIngressMapping unknown port error = %v, want ErrUnknownPort", err)
	}
	sw.AddPort(pA)
	if err := sw.SetEgressMapping(pA, bd, vlan20, true); err != nil {
		t.Errorf("SetEgressMapping on known port returned unexpected error: %v", err)
	}
	if err := sw.SetEgressMapping(pB, bd, vlan20, true); !errors.Is(err, l2switch.ErrUnknownPort) {
		t.Errorf("SetEgressMapping unknown port error = %v, want ErrUnknownPort", err)
	}
	if _, err := sw.State(pB, bd); !errors.Is(err, l2switch.ErrUnknownPort) {
		t.Errorf("State unknown port error = %v, want ErrUnknownPort", err)
	}
	if sw.Name() != "errors" {
		t.Errorf("Name() = %q, want %q", sw.Name(), "errors")
	}
}

// TestLearnedCountIsScopedPerBridgeDomain ensures the diagnostic counter
// only counts entries of the requested domain and reacts to MAC moves.
func TestLearnedCountIsScopedPerBridgeDomain(t *testing.T) {
	sw := l2switch.NewSwitch("counts")
	for _, p := range []l2switch.PortID{pP, pQ} {
		sw.AddPort(p)
		must(t, sw.SetIngressMapping(p, vlan10, true, bdX))
		must(t, sw.SetIngressMapping(p, vlan20, true, bdY))
		must(t, sw.SetState(p, bdX, l2switch.StateForwarding))
		must(t, sw.SetState(p, bdY, l2switch.StateForwarding))
	}
	recv(t, sw, labeled(pP, vlan10, macH1, macH2, "x1"))
	recv(t, sw, labeled(pQ, vlan10, macH2, macH1, "x2"))
	recv(t, sw, labeled(pP, vlan20, macH3, macH4, "y1"))

	if got := sw.LearnedCount(bdX); got != 2 {
		t.Errorf("BD-X learned count = %d, want 2", got)
	}
	if got := sw.LearnedCount(bdY); got != 1 {
		t.Errorf("BD-Y learned count = %d, want 1", got)
	}

	// A MAC move overwrites rather than adds an entry.
	recv(t, sw, labeled(pQ, vlan10, macH1, macH2, "x1-moved"))
	if got := sw.LearnedCount(bdX); got != 2 {
		t.Errorf("BD-X learned count after MAC move = %d, still want 2", got)
	}
}

// TestPortStateStringAndZeroSourceNotLearned covers two remaining boundary
// rules: the diagnostic rendering of every valid state, and that an
// all-zero source MAC never creates a learning entry.
func TestPortStateStringAndZeroSourceNotLearned(t *testing.T) {
	cases := map[l2switch.PortState]string{
		l2switch.StateBlocking:   "blocking",
		l2switch.StateLearnOnly:  "learn-only",
		l2switch.StateForwarding: "forwarding",
	}
	for st, want := range cases {
		if got := st.String(); got != want {
			t.Errorf("PortState(%d).String() = %q, want %q", int(st), got, want)
		}
	}

	sw := newThreePortBD(t)
	for _, p := range []l2switch.PortID{pA, pB} {
		must(t, sw.SetState(p, bd, l2switch.StateForwarding))
	}
	zeroSrc := l2switch.MAC{}
	recv(t, sw, labeled(pA, vlan10, zeroSrc, macH2, "zero-src"))
	if _, hit := sw.LookupLearned(bd, zeroSrc); hit {
		t.Error("all-zero source MAC must not be learned")
	}
	if got := sw.LearnedCount(bd); got != 0 {
		t.Errorf("learned count with zero source only = %d, want 0", got)
	}
}

// TestDefaultStateAndConfigurationErrors checks the default blocking state
// and the documented error sentinels.
func TestDefaultStateAndConfigurationErrors(t *testing.T) {
	sw := l2switch.NewSwitch("errors")
	sw.AddPort(pA)
	must(t, sw.SetIngressMapping(pA, vlan10, true, bd))

	if st, err := sw.State(pA, bd); err != nil || st != l2switch.StateBlocking {
		t.Fatalf("new member default state = %s, err=%v; want blocking", st, err)
	}

	if _, err := sw.Receive(labeled(pA, vlan20, macH1, macH2, "wrong-label")); !errors.Is(err, l2switch.ErrNoIngressMapping) {
		t.Errorf("unmapped label error = %v, want ErrNoIngressMapping", err)
	}
	if _, err := sw.Receive(labeled(pB, vlan10, macH1, macH2, "no-port")); !errors.Is(err, l2switch.ErrUnknownPort) {
		t.Errorf("unknown port error = %v, want ErrUnknownPort", err)
	}
	if err := sw.SetState(pA, bdB, l2switch.StateForwarding); !errors.Is(err, l2switch.ErrNoPortState) {
		t.Errorf("state on non-member port error = %v, want ErrNoPortState", err)
	}
	if err := sw.SetState(pA, bd, l2switch.PortState(99)); !errors.Is(err, l2switch.ErrInvalidPortState) {
		t.Errorf("invalid state error = %v, want ErrInvalidPortState", err)
	}
}
