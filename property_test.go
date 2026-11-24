package negotiation

import (
	"math/rand"
	"net/netip"
	"testing"
)

type rawGrant struct {
	protocol ProtocolRange
	sourceLo int
	sourceHi int
	destLo   int
	destHi   int
	portLo   uint16
	portHi   uint16
}

func TestRandomizedInterleavedPoliciesMatchFlowOracle(t *testing.T) {
	r := rand.New(rand.NewSource(15877))
	for iteration := 0; iteration < 300; iteration++ {
		a := Party{ID: "A"}
		b := Party{ID: "B"}
		var aSendFlows, aReceiveFlows, bSendFlows, bReceiveFlows map[testFlow]struct{}

		a.Send, aSendFlows = randomOraclePolicy(r)
		a.Receive, aReceiveFlows = randomOraclePolicy(r)
		b.Send, bSendFlows = randomOraclePolicy(r)
		b.Receive, bReceiveFlows = randomOraclePolicy(r)

		result, err := Negotiate(a, b)
		if err != nil {
			t.Fatalf("iteration %d: Negotiate returned error: %v", iteration, err)
		}

		checkDirectionOracle(t, iteration, result.Forward,
			aSendFlows, bReceiveFlows,
			result.Forward.SenderUnmatched, result.Forward.ReceiverUnmatched)
		checkDirectionOracle(t, iteration, result.Reverse,
			bSendFlows, aReceiveFlows,
			result.Reverse.SenderUnmatched, result.Reverse.ReceiverUnmatched)

		requireRulesDisjoint(t, result.Rules)
		if got := len(result.Unmatched); got !=
			len(result.Forward.SenderUnmatched)+len(result.Forward.ReceiverUnmatched)+
				len(result.Reverse.SenderUnmatched)+len(result.Reverse.ReceiverUnmatched) {
			t.Fatalf("iteration %d: flat unmatched count %d does not match directional scopes", iteration, got)
		}
	}
}

func checkDirectionOracle(
	t *testing.T,
	iteration int,
	dir DirectionResult,
	senderFlows, receiverFlows map[testFlow]struct{},
	senderUnmatched, receiverUnmatched []Scope,
) {
	t.Helper()
	wantAgreed := intersectFlowSets(senderFlows, receiverFlows)
	gotAgreed := ruleFlowSet(dir.Agreed)
	requireFlowSetEqual(t, "iter direction "+dir.Direction+" agreed", gotAgreed, wantAgreed)

	wantSenderDenied := subtractFlowSets(senderFlows, wantAgreed)
	gotSenderDenied := scopeFlowSet(senderUnmatched)
	requireFlowSetEqual(t, "iter direction "+dir.Direction+" sender unmatched", gotSenderDenied, wantSenderDenied)

	wantReceiverDenied := subtractFlowSets(receiverFlows, wantAgreed)
	gotReceiverDenied := scopeFlowSet(receiverUnmatched)
	requireFlowSetEqual(t, "iter direction "+dir.Direction+" receiver unmatched", gotReceiverDenied, wantReceiverDenied)

	requireRulesDisjoint(t, dir.Agreed)
	requireNoScopeOverlap(t, dir.Direction+" sender unmatched", senderUnmatched)
	requireNoScopeOverlap(t, dir.Direction+" receiver unmatched", receiverUnmatched)

	// The two residual sides must also be mutually exclusive because each is
	// its own policy minus the common intersection.
	for i, a := range senderUnmatched {
		for j, b := range receiverUnmatched {
			if ScopesOverlap(a, b) {
				t.Fatalf("iteration %d %s: sender residual %d overlaps receiver residual %d", iteration, dir.Direction, i, j)
			}
		}
	}
}

func randomOraclePolicy(r *rand.Rand) (Policy, map[testFlow]struct{}) {
	grantCount := r.Intn(5)
	raw := make([]rawGrant, 0, grantCount)
	grants := make([]Grant, 0, grantCount)
	for i := 0; i < grantCount; i++ {
		srcLo := 1 + r.Intn(18)
		srcHi := srcLo + r.Intn(4)
		dstLo := 1 + r.Intn(18)
		dstHi := dstLo + r.Intn(4)
		portLo := uint16(30000 + r.Intn(18))
		portHi := portLo + uint16(r.Intn(4))

		protocols := []ProtocolRange{
			{Start: ProtocolTCP, End: ProtocolTCP},
			{Start: ProtocolUDP, End: ProtocolUDP},
			{Start: ProtocolICMP, End: ProtocolICMP},
			{Start: ProtocolTCP, End: ProtocolUDP},
		}
		proto := protocols[r.Intn(len(protocols))]
		g := rawGrant{
			protocol: proto,
			sourceLo: srcLo,
			sourceHi: srcHi,
			destLo:   dstLo,
			destHi:   dstHi,
			portLo:   portLo,
			portHi:   portHi,
		}
		raw = append(raw, g)
		grants = append(grants, Grant{
			Protocol:    proto.String(),
			Source:      ipv4LastOctetRange(0, srcLo, srcHi),
			Destination: ipv4LastOctetRange(1, dstLo, dstHi),
			Ports:       []PortRange{{Start: portLo, End: portHi}},
		})
	}

	exclusions := Exclusions{}
	reservedCount := r.Intn(3)
	for i := 0; i < reservedCount; i++ {
		octet := byte(r.Intn(2)) // 10.0.0.x source or 10.1.0.x destination
		lo := 1 + r.Intn(22)
		hi := lo + r.Intn(2)
		exclusions.ReservedAddresses = append(exclusions.ReservedAddresses, ipv4LastOctetRange(octet, lo, hi))
	}
	excludedPortCount := r.Intn(3)
	for i := 0; i < excludedPortCount; i++ {
		exclusions.DestinationPorts = append(exclusions.DestinationPorts, SinglePort(uint16(30000+r.Intn(25))))
	}

	return Policy{Grants: grants, Exclusions: exclusions}, expectedOracleFlows(raw, exclusions)
}

func expectedOracleFlows(grants []rawGrant, exclusions Exclusions) map[testFlow]struct{} {
	out := make(map[testFlow]struct{})
	for _, g := range grants {
		for proto := int(g.protocol.Start); proto <= int(g.protocol.End); proto++ {
			for src := g.sourceLo; src <= g.sourceHi; src++ {
				srcAddr := netip.AddrFrom4([4]byte{10, 0, 0, byte(src)})
				for dst := g.destLo; dst <= g.destHi; dst++ {
					dstAddr := netip.AddrFrom4([4]byte{10, 1, 0, byte(dst)})
					for port := int(g.portLo); port <= int(g.portHi); port++ {
						if oracleExcluded(srcAddr, dstAddr, port, exclusions) {
							continue
						}
						out[testFlow{
							Protocol: proto,
							Source:   srcAddr.String(),
							Dest:     dstAddr.String(),
							Port:     port,
						}] = struct{}{}
					}
				}
			}
		}
	}
	return out
}

func oracleExcluded(src, dst netip.Addr, port int, exclusions Exclusions) bool {
	for _, reserved := range exclusions.ReservedAddresses {
		if reserved.Contains(src) || reserved.Contains(dst) {
			return true
		}
	}
	p := SinglePort(uint16(port))
	for _, excluded := range exclusions.DestinationPorts {
		if rangesOverlapPort(p, excluded) {
			return true
		}
	}
	return false
}

func ipv4LastOctetRange(secondOctet byte, lo, hi int) AddressRange {
	return AddressRange{
		Start: netip.AddrFrom4([4]byte{10, secondOctet, 0, byte(lo)}),
		End:   netip.AddrFrom4([4]byte{10, secondOctet, 0, byte(hi)}),
	}
}
