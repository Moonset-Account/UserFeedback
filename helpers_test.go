package negotiation

import (
	"fmt"
	"net/netip"
	"sort"
	"testing"
)

type testFlow struct {
	Protocol int
	Source   string
	Dest     string
	Port     int
}

func enumerateAddresses(r AddressRange) []netip.Addr {
	var out []netip.Addr
	for a := r.Start; a.Compare(r.End) <= 0; a = a.Next() {
		out = append(out, a)
		if a == r.End {
			break
		}
	}
	return out
}

func scopeFlowSet(scopes []Scope) map[testFlow]struct{} {
	out := make(map[testFlow]struct{})
	for _, scope := range scopes {
		sources := enumerateAddresses(scope.Source)
		dests := enumerateAddresses(scope.Destination)
		for proto := int(scope.Protocol.Start); proto <= int(scope.Protocol.End); proto++ {
			for _, src := range sources {
				for _, dst := range dests {
					for _, portRange := range scope.DestinationPorts {
						for port := int(portRange.Start); port <= int(portRange.End); port++ {
							f := testFlow{
								Protocol: proto,
								Source:   src.String(),
								Dest:     dst.String(),
								Port:     port,
							}
							out[f] = struct{}{}
						}
					}
				}
			}
		}
	}
	return out
}

func ruleFlowSet(rules []Rule) map[testFlow]struct{} {
	scopes := make([]Scope, 0, len(rules))
	for _, rule := range rules {
		scopes = append(scopes, rule.Scope)
	}
	return scopeFlowSet(scopes)
}

func unionFlowSets(sets ...map[testFlow]struct{}) map[testFlow]struct{} {
	out := make(map[testFlow]struct{})
	for _, set := range sets {
		for f := range set {
			out[f] = struct{}{}
		}
	}
	return out
}

func intersectFlowSets(a, b map[testFlow]struct{}) map[testFlow]struct{} {
	out := make(map[testFlow]struct{})
	for f := range a {
		if _, ok := b[f]; ok {
			out[f] = struct{}{}
		}
	}
	return out
}

func subtractFlowSets(a, b map[testFlow]struct{}) map[testFlow]struct{} {
	out := make(map[testFlow]struct{})
	for f := range a {
		_, blocked := b[f]
		if !blocked {
			out[f] = struct{}{}
		}
	}
	return out
}

func requireFlowSetEqual(t *testing.T, name string, got, want map[testFlow]struct{}) {
	t.Helper()
	missing := subtractFlowSets(want, got)
	extra := subtractFlowSets(got, want)
	if len(missing) == 0 && len(extra) == 0 {
		return
	}
	t.Errorf("%s mismatch: missing=%s extra=%s", name, formatFlows(missing), formatFlows(extra))
}

func formatFlows(set map[testFlow]struct{}) string {
	flows := make([]testFlow, 0, len(set))
	for f := range set {
		flows = append(flows, f)
	}
	sort.Slice(flows, func(i, j int) bool {
		if flows[i].Source != flows[j].Source {
			return flows[i].Source < flows[j].Source
		}
		if flows[i].Dest != flows[j].Dest {
			return flows[i].Dest < flows[j].Dest
		}
		if flows[i].Protocol != flows[j].Protocol {
			return flows[i].Protocol < flows[j].Protocol
		}
		return flows[i].Port < flows[j].Port
	})
	const limit = 20
	parts := make([]string, 0, len(flows))
	for i, f := range flows {
		if i >= limit {
			parts = append(parts, fmt.Sprintf("...(%d more)", len(flows)-limit))
			break
		}
		parts = append(parts, fmt.Sprintf("{p:%d %s->%s dport:%d}", f.Protocol, f.Source, f.Dest, f.Port))
	}
	return fmt.Sprintf("count=%d %v", len(set), parts)
}

func requireNoScopeOverlap(t *testing.T, name string, scopes []Scope) {
	t.Helper()
	for i := 0; i < len(scopes); i++ {
		for j := i + 1; j < len(scopes); j++ {
			if ScopesOverlap(scopes[i], scopes[j]) {
				t.Fatalf("%s scopes %d and %d overlap:\n%+v\n%+v", name, i, j, scopes[i], scopes[j])
			}
			intersection := intersectFlowSets(scopeFlowSet([]Scope{scopes[i]}), scopeFlowSet([]Scope{scopes[j]}))
			if len(intersection) != 0 {
				t.Fatalf("%s scopes %d and %d share enumerated flows: %s", name, i, j, formatFlows(intersection))
			}
		}
	}
}

func requireRulesDisjoint(t *testing.T, rules []Rule) {
	t.Helper()
	if err := CheckRulesDisjoint(rules); err != nil {
		t.Fatalf("rules are not pairwise disjoint: %v", err)
	}
	if !RulesPairwiseDisjoint(rules) {
		t.Fatal("RulesPairwiseDisjoint returned false")
	}
	for i := 0; i < len(rules); i++ {
		for j := i + 1; j < len(rules); j++ {
			if rules[i].Direction != rules[j].Direction {
				continue
			}
			intersection := intersectFlowSets(ruleFlowSet([]Rule{rules[i]}), ruleFlowSet([]Rule{rules[j]}))
			if len(intersection) != 0 {
				t.Fatalf("rules %d and %d share enumerated flows: %s", i, j, formatFlows(intersection))
			}
		}
	}
}
