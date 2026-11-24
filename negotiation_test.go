package negotiation

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func tcpRange() ProtocolRange {
	return ProtocolRange{Start: ProtocolTCP, End: ProtocolTCP}
}

func udpRange() ProtocolRange {
	return ProtocolRange{Start: ProtocolUDP, End: ProtocolUDP}
}

func TestNegotiateUsesSendIntersectReceiveInBothDirections(t *testing.T) {
	a := Party{
		ID: "branch-a",
		Send: Policy{Grants: []Grant{{
			Protocol:    "tcp",
			Source:      MustParseAddressRange("10.1.0.0/24"),
			Destination: MustParseAddressRange("10.2.0.0/24"),
			Ports:       Ports(443),
		}}},
		Receive: Policy{Grants: []Grant{{
			Protocol:    "udp",
			Source:      MustParseAddressRange("10.2.0.0/24"),
			Destination: MustParseAddressRange("10.1.0.0/24"),
			Ports:       Ports(53),
		}}},
	}
	b := Party{
		ID: "branch-b",
		Send: Policy{Grants: []Grant{{
			Protocol:    "udp",
			Source:      MustParseAddressRange("10.2.0.0/24"),
			Destination: MustParseAddressRange("10.1.0.0/24"),
			Ports:       Ports(53),
		}}},
		Receive: Policy{Grants: []Grant{{
			Protocol:    "tcp",
			Source:      MustParseAddressRange("10.1.0.0/24"),
			Destination: MustParseAddressRange("10.2.0.0/24"),
			Ports:       Ports(443),
		}}},
	}

	result, err := Negotiate(a, b)
	if err != nil {
		t.Fatal(err)
	}

	if result.Forward.Direction != "branch-a->branch-b" {
		t.Fatalf("unexpected forward direction %q", result.Forward.Direction)
	}
	if len(result.Forward.Agreed) != 1 {
		t.Fatalf("expected one forward rule, got %+v", result.Forward.Agreed)
	}
	forward := result.Forward.Agreed[0]
	if forward.Direction != result.Forward.Direction {
		t.Fatalf("rule direction %q mismatch", forward.Direction)
	}
	if forward.Protocol != tcpRange() {
		t.Fatalf("expected tcp, got %s", forward.Protocol)
	}
	if forward.Source != MustParseAddressRange("10.1.0.0/24") {
		t.Fatalf("unexpected source %v", forward.Source)
	}
	if forward.Destination != MustParseAddressRange("10.2.0.0/24") {
		t.Fatalf("unexpected destination %v", forward.Destination)
	}
	if len(forward.DestinationPorts) != 1 || forward.DestinationPorts[0] != SinglePort(443) {
		t.Fatalf("unexpected ports %v", forward.DestinationPorts)
	}

	if len(result.Reverse.Agreed) != 1 {
		t.Fatalf("expected one reverse rule, got %+v", result.Reverse.Agreed)
	}
	reverse := result.Reverse.Agreed[0]
	if reverse.Direction != "branch-b->branch-a" {
		t.Fatalf("unexpected reverse direction %q", reverse.Direction)
	}
	if reverse.Protocol != udpRange() {
		t.Fatalf("expected reverse udp/53, got %s", reverse.Protocol)
	}
	if len(reverse.DestinationPorts) != 1 || reverse.DestinationPorts[0] != SinglePort(53) {
		t.Fatalf("unexpected reverse ports %v", reverse.DestinationPorts)
	}

	if len(result.Forward.SenderUnmatched) != 0 || len(result.Forward.ReceiverUnmatched) != 0 {
		t.Fatalf("expected no forward unmatched scopes")
	}
	if len(result.Reverse.SenderUnmatched) != 0 || len(result.Reverse.ReceiverUnmatched) != 0 {
		t.Fatalf("expected no reverse unmatched scopes")
	}
	requireRulesDisjoint(t, result.Rules)
}

func TestInclusiveBoundariesAndBothUnmatchedSides(t *testing.T) {
	aSendScope := Scope{
		Protocol:         tcpRange(),
		Source:           MustNewAddressRange("10.0.0.1", "10.0.0.3"),
		Destination:      MustNewAddressRange("10.1.0.1", "10.1.0.3"),
		DestinationPorts: []PortRange{{Start: 10, End: 12}},
	}
	bReceiveScope := Scope{
		Protocol:         tcpRange(),
		Source:           MustNewAddressRange("10.0.0.2", "10.0.0.4"),
		Destination:      MustNewAddressRange("10.1.0.2", "10.1.0.4"),
		DestinationPorts: []PortRange{{Start: 11, End: 13}},
	}
	a := Party{ID: "A", Send: Policy{Grants: []Grant{{
		Protocol:    "tcp",
		Source:      aSendScope.Source,
		Destination: aSendScope.Destination,
		Ports:       aSendScope.DestinationPorts,
	}}}}
	b := Party{ID: "B", Receive: Policy{Grants: []Grant{{
		Protocol:    "tcp",
		Source:      bReceiveScope.Source,
		Destination: bReceiveScope.Destination,
		Ports:       bReceiveScope.DestinationPorts,
	}}}}

	result, err := Negotiate(a, b)
	if err != nil {
		t.Fatal(err)
	}

	wantAgreed := Scope{
		Protocol:         tcpRange(),
		Source:           MustNewAddressRange("10.0.0.2", "10.0.0.3"),
		Destination:      MustNewAddressRange("10.1.0.2", "10.1.0.3"),
		DestinationPorts: []PortRange{{Start: 11, End: 12}},
	}
	if len(result.Forward.Agreed) != 1 || !reflect.DeepEqual(result.Forward.Agreed[0].Scope, wantAgreed) {
		t.Fatalf("boundary intersection mismatch:\ngot  %#v\nwant %#v", result.Forward.Agreed, wantAgreed)
	}

	aFlows := scopeFlowSet([]Scope{aSendScope})
	bFlows := scopeFlowSet([]Scope{bReceiveScope})
	agreedFlows := ruleFlowSet(result.Forward.Agreed)
	requireFlowSetEqual(t, "agreed", agreedFlows, intersectFlowSets(aFlows, bFlows))
	requireFlowSetEqual(t, "A sender unmatched",
		scopeFlowSet(result.Forward.SenderUnmatched), subtractFlowSets(aFlows, agreedFlows))
	requireFlowSetEqual(t, "B receiver unmatched",
		scopeFlowSet(result.Forward.ReceiverUnmatched), subtractFlowSets(bFlows, agreedFlows))

	requireNoScopeOverlap(t, "A sender unmatched", result.Forward.SenderUnmatched)
	requireNoScopeOverlap(t, "B receiver unmatched", result.Forward.ReceiverUnmatched)
	requireRulesDisjoint(t, result.Rules)

	if len(result.Reverse.Agreed) != 0 {
		t.Fatalf("expected no reverse rules, got %+v", result.Reverse.Agreed)
	}
}

func TestDifferentAddressRangesKeepPortSetCorrespondence(t *testing.T) {
	src0 := MustParseAddressRange("10.10.0.0/24")
	src1 := MustParseAddressRange("10.10.1.0/24")
	dst := MustParseAddressRange("10.20.0.0/24")

	a := Party{ID: "A", Send: Policy{Grants: []Grant{
		{Protocol: "tcp", Source: src0, Destination: dst, Ports: Ports(80, 443)},
		{Protocol: "tcp", Source: src1, Destination: dst, Ports: Ports(8080)},
	}}}
	b := Party{ID: "B", Receive: Policy{Grants: []Grant{{
		Protocol:    "tcp",
		Source:      MustParseAddressRange("10.10.0.0/23"),
		Destination: dst,
		Ports:       Ports(80, 443, 8080),
	}}}}

	result, err := Negotiate(a, b)
	if err != nil {
		t.Fatal(err)
	}
	rules := result.Forward.Agreed
	requireRulesDisjoint(t, rules)
	if len(rules) != 2 {
		t.Fatalf("expected two correspondence-preserving rules, got %+v", rules)
	}

	bySource := map[string]Rule{}
	for _, rule := range rules {
		bySource[rule.Source.String()] = rule
	}
	first, ok := bySource[src0.String()]
	if !ok {
		t.Fatalf("missing rule for %s: %+v", src0, rules)
	}
	if got := first.DestinationPorts; len(got) != 2 || got[0] != SinglePort(80) || got[1] != SinglePort(443) {
		t.Fatalf("%s must retain ports 80 and 443, got %v", src0, got)
	}
	second, ok := bySource[src1.String()]
	if !ok {
		t.Fatalf("missing rule for %s: %+v", src1, rules)
	}
	if got := second.DestinationPorts; len(got) != 1 || got[0] != SinglePort(8080) {
		t.Fatalf("%s must retain port 8080, got %v", src1, got)
	}

	// A merged /23 with the union {80,443,8080} would add 8080 to src0 and
	// 80/443 to src1. Ensure that incorrect broad rule does not exist.
	wrongSource := MustParseAddressRange("10.10.0.0/23")
	for _, rule := range rules {
		if rule.Source == wrongSource {
			t.Fatalf("address ranges with different port sets were incorrectly merged: %+v", rule)
		}
	}

	wantFlows := unionFlowSets(
		scopeFlowSet([]Scope{{Protocol: tcpRange(), Source: src0, Destination: dst, DestinationPorts: Ports(80, 443)}}),
		scopeFlowSet([]Scope{{Protocol: tcpRange(), Source: src1, Destination: dst, DestinationPorts: Ports(8080)}}),
	)
	requireFlowSetEqual(t, "correspondence rules", ruleFlowSet(rules), wantFlows)
}

func TestIdenticalPortSetsOnAdjacentRangesMerge(t *testing.T) {
	dst := MustParseAddressRange("10.20.0.0/24")
	a := Party{ID: "A", Send: Policy{Grants: []Grant{
		{Protocol: "tcp", Source: MustParseAddressRange("10.10.0.0/24"), Destination: dst, Ports: Ports(443)},
		{Protocol: "tcp", Source: MustParseAddressRange("10.10.1.0/24"), Destination: dst, Ports: Ports(443)},
	}}}
	b := Party{ID: "B", Receive: Policy{Grants: []Grant{{
		Protocol:    "tcp",
		Source:      MustParseAddressRange("10.10.0.0/23"),
		Destination: dst,
		Ports:       Ports(443),
	}}}}

	result, err := Negotiate(a, b)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Forward.Agreed) != 1 {
		t.Fatalf("expected adjacent identical-port ranges to merge, got %+v", result.Forward.Agreed)
	}
	merged := result.Forward.Agreed[0]
	want := MustNewAddressRange("10.10.0.0", "10.10.1.255")
	if merged.Source != want {
		t.Fatalf("expected merged source %s, got %s", want, merged.Source)
	}
	requireRulesDisjoint(t, result.Rules)
}

func TestExcludeReservedAddressesAndSinglePort(t *testing.T) {
	src := MustParseAddressRange("10.0.0.0/24")
	dst := MustParseAddressRange("10.1.0.0/24")
	policy := Policy{
		Grants: []Grant{{
			Protocol:    "tcp",
			Source:      src,
			Destination: dst,
			Ports:       []PortRange{{Start: 80, End: 82}},
		}},
		Exclusions: Exclusions{
			ReservedAddresses: []AddressRange{
				MustParseAddressRange("10.0.0.128/25"),
				MustParseAddressRange("10.1.0.5"),
			},
			DestinationPorts: Ports(81),
		},
	}
	a := Party{ID: "A", Send: policy}
	b := Party{ID: "B", Receive: Policy{Grants: []Grant{{
		Protocol:    "tcp",
		Source:      MustParseAddressRange("10.0.0.0/16"),
		Destination: MustParseAddressRange("10.1.0.0/16"),
		Ports:       []PortRange{{Start: 80, End: 82}},
	}}}}

	result, err := Negotiate(a, b)
	if err != nil {
		t.Fatal(err)
	}
	rules := result.Forward.Agreed
	requireRulesDisjoint(t, rules)
	if len(rules) != 2 {
		t.Fatalf("expected destination reserved address to split into two rules, got %+v", rules)
	}

	wantSource := MustNewAddressRange("10.0.0.0", "10.0.0.127")
	wantDestLeft := MustNewAddressRange("10.1.0.0", "10.1.0.4")
	wantDestRight := MustNewAddressRange("10.1.0.6", "10.1.0.255")
	byDest := map[string]Rule{}
	for _, rule := range rules {
		if rule.Source != wantSource {
			t.Fatalf("reserved source half not removed correctly: %s", rule.Source)
		}
		if got := rule.DestinationPorts; len(got) != 2 || got[0] != SinglePort(80) || got[1] != SinglePort(82) {
			t.Fatalf("port 81 exclusion should leave 80 and 82, got %v", got)
		}
		byDest[rule.Destination.String()] = rule
	}
	if _, ok := byDest[wantDestLeft.String()]; !ok {
		t.Fatalf("missing destination range %s in %+v", wantDestLeft, rules)
	}
	if _, ok := byDest[wantDestRight.String()]; !ok {
		t.Fatalf("missing destination range %s in %+v", wantDestRight, rules)
	}
}

func TestDifferentDestinationRangesKeepPortCorrespondenceAndGaps(t *testing.T) {
	src := MustParseAddressRange("10.10.0.0/24")
	dst0 := MustParseAddressRange("10.20.0.0/24")
	dst1 := MustParseAddressRange("10.20.1.0/24")
	dstGap := MustParseAddressRange("10.20.2.0/24")

	a := Party{ID: "A", Send: Policy{Grants: []Grant{
		{Protocol: "tcp", Source: src, Destination: dst0, Ports: Ports(80)},
		{Protocol: "tcp", Source: src, Destination: dst1, Ports: Ports(443)},
		{Protocol: "tcp", Source: src, Destination: dstGap, Ports: Ports(8443)},
	}}}
	b := Party{ID: "B", Receive: Policy{Grants: []Grant{{
		Protocol:    "tcp",
		Source:      src,
		Destination: MustParseAddressRange("10.20.0.0/16"),
		Ports:       Ports(80, 443, 8443),
	}}}}

	result, err := Negotiate(a, b)
	if err != nil {
		t.Fatal(err)
	}
	rules := result.Forward.Agreed
	if len(rules) != 3 {
		t.Fatalf("expected three destination/port mappings, got %+v", rules)
	}
	byDest := map[string][]PortRange{}
	for _, rule := range rules {
		byDest[rule.Destination.String()] = rule.DestinationPorts
	}
	if got := byDest[dst0.String()]; len(got) != 1 || got[0] != SinglePort(80) {
		t.Fatalf("bad mapping for %s: %v", dst0, got)
	}
	if got := byDest[dst1.String()]; len(got) != 1 || got[0] != SinglePort(443) {
		t.Fatalf("bad mapping for %s: %v", dst1, got)
	}
	if got := byDest[dstGap.String()]; len(got) != 1 || got[0] != SinglePort(8443) {
		t.Fatalf("bad mapping for %s: %v", dstGap, got)
	}
	requireRulesDisjoint(t, rules)

	// Equal port sets separated by an address gap must not become one broad
	// start-end range.
	a.Send.Grants = []Grant{
		{Protocol: "tcp", Source: src, Destination: dst0, Ports: Ports(443)},
		{Protocol: "tcp", Source: src, Destination: dstGap, Ports: Ports(443)},
	}
	b.Receive.Grants[0].Ports = Ports(443)
	result, err = Negotiate(a, b)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Forward.Agreed) != 2 {
		t.Fatalf("gapped equal-port ranges should remain separate, got %+v", result.Forward.Agreed)
	}
}

func TestReverseUsesIndependentPoliciesAndReportsMismatches(t *testing.T) {
	a := Party{
		ID: "A",
		Send: Policy{Grants: []Grant{{
			Protocol:    "tcp",
			Source:      MustParseAddressRange("10.0.0.0/24"),
			Destination: MustParseAddressRange("10.1.0.0/24"),
			Ports:       Ports(443),
		}}},
		Receive: Policy{Grants: []Grant{{
			Protocol:    "tcp",
			Source:      MustParseAddressRange("10.1.0.0/24"),
			Destination: MustParseAddressRange("10.0.0.0/24"),
			Ports:       Ports(22),
		}}},
	}
	b := Party{
		ID: "B",
		Send: Policy{Grants: []Grant{{
			Protocol:    "tcp",
			Source:      MustParseAddressRange("10.1.0.0/24"),
			Destination: MustParseAddressRange("10.0.0.0/24"),
			Ports:       Ports(8000),
		}}},
		Receive: Policy{Grants: []Grant{{
			Protocol:    "tcp",
			Source:      MustParseAddressRange("10.0.0.0/24"),
			Destination: MustParseAddressRange("10.1.0.0/24"),
			Ports:       Ports(443),
		}}},
	}

	result, err := Negotiate(a, b)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Forward.Agreed) != 1 {
		t.Fatalf("expected forward 443 agreement")
	}
	if len(result.Reverse.Agreed) != 0 {
		t.Fatalf("reverse should have no agreement because 8000 and 22 differ: %+v", result.Reverse.Agreed)
	}
	if len(result.Reverse.SenderUnmatched) == 0 {
		t.Fatal("expected B send 8000 to be unmatched")
	}
	if len(result.Reverse.ReceiverUnmatched) == 0 {
		t.Fatal("expected A receive 22 to be unmatched")
	}

	foundB := false
	for _, u := range result.Unmatched {
		if u.Direction == "B->A" && u.PartyID == "B" && u.Channel == ChannelSend {
			foundB = true
		}
	}
	if !foundB {
		t.Fatalf("flat unmatched list missing B send: %+v", result.Unmatched)
	}
}

func TestMergeRulesCanonicalizesExternalOverlap(t *testing.T) {
	direction := "A->B"
	dst := MustParseAddressRange("10.1.0.1")
	rules := []Rule{
		{Direction: direction, Scope: Scope{
			Protocol: tcpRange(), Source: MustNewAddressRange("10.0.0.0", "10.0.0.2"),
			Destination: dst, DestinationPorts: Ports(80),
		}},
		{Direction: direction, Scope: Scope{
			Protocol: tcpRange(), Source: MustNewAddressRange("10.0.0.2", "10.0.0.4"),
			Destination: dst, DestinationPorts: Ports(80),
		}},
	}
	merged, err := MergeRules(rules)
	if err != nil {
		t.Fatal(err)
	}
	if len(merged) != 1 {
		t.Fatalf("expected overlapping/adjacent external rules to merge, got %+v", merged)
	}
	if merged[0].Source != MustNewAddressRange("10.0.0.0", "10.0.0.4") {
		t.Fatalf("unexpected merged source %s", merged[0].Source)
	}
	requireRulesDisjoint(t, merged)

	// Negotiate output should already be canonical and survive another merge.
	a := Party{Send: Policy{Grants: []Grant{{
		Protocol: "tcp", Source: MustParseAddressRange("10.0.0.0/24"),
		Destination: dst, Ports: Ports(80, 443),
	}}}}
	b := Party{Receive: a.Send}
	result, err := Negotiate(a, b)
	if err != nil {
		t.Fatal(err)
	}
	remerged, err := MergeRules(result.Rules)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(remerged, result.Rules) {
		t.Fatalf("MergeRules is not idempotent:\ngot  %+v\nwant %+v", remerged, result.Rules)
	}
}

func TestInvalidPolicyReturnsError(t *testing.T) {
	cases := []Party{
		{ID: "bad-proto", Send: Policy{Grants: []Grant{{Protocol: "nope"}}}},
		{ID: "bad-source", Send: Policy{Grants: []Grant{{Source: AddressRange{}}}}},
		{ID: "bad-port", Send: Policy{Grants: []Grant{{
			Source:      MustParseAddressRange("10.0.0.1"),
			Destination: MustParseAddressRange("10.0.0.2"),
			Ports:       []PortRange{{Start: 90, End: 80}},
		}}}},
	}
	for _, tc := range cases {
		if _, err := Negotiate(tc, Party{}); err == nil {
			t.Fatalf("expected error for %s", tc.ID)
		}
	}
}

func TestResultJSONEncoding(t *testing.T) {
	a := Party{Send: Policy{Grants: []Grant{{
		Protocol:    "tcp",
		Source:      MustParseAddressRange("10.0.0.1"),
		Destination: MustParseAddressRange("10.0.0.2"),
		Ports:       Ports(443),
	}}}}
	b := Party{Receive: a.Send}
	result, err := Negotiate(a, b)
	if err != nil {
		t.Fatal(err)
	}
	data, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	text := string(data)
	for _, want := range []string{`"protocol": "tcp"`, `"start": "10.0.0.1"`, `"direction": "A-`} {
		if !strings.Contains(text, want) {
			t.Fatalf("JSON missing %s\n%s", want, text)
		}
	}

	var decoded Result
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("result JSON should round-trip: %v\n%s", err, text)
	}
	if len(decoded.Rules) != 1 || decoded.Rules[0].Protocol != tcpRange() {
		t.Fatalf("bad decoded result: %+v", decoded.Rules)
	}
	if decoded.Forward.Direction != "A->B" || decoded.Rules[0].Direction != "A->B" {
		t.Fatalf("direction did not round-trip: %q / %q", decoded.Forward.Direction, decoded.Rules[0].Direction)
	}
}
