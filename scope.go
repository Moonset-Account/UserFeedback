package negotiation

import (
	"fmt"
	"sort"
)

// Scope is a merged set of flows sharing one contiguous protocol range and
// one source/destination address interval. DestinationPorts is canonical and
// may contain several disjoint intervals.
type Scope struct {
	Protocol         ProtocolRange `json:"protocol"`
	Source           AddressRange  `json:"source"`
	Destination      AddressRange  `json:"destination"`
	DestinationPorts []PortRange   `json:"destination_ports"`
}

// MergeScopes canonicalizes arbitrary overlapping scopes into an exact union
// represented by pairwise-disjoint scopes. It also merges adjacent address
// ranges only when protocol, the other address dimension, and port set are
// identical.
func MergeScopes(scopes []Scope) ([]Scope, error) {
	boxes, err := scopesToBoxes(scopes)
	if err != nil {
		return nil, err
	}
	return boxesToScopes(disjointizeBoxes(boxes)), nil
}

func scopesToBoxes(scopes []Scope) ([]box, error) {
	var boxes []box
	for i, scope := range scopes {
		if scope.Protocol.Start > scope.Protocol.End {
			return nil, fmt.Errorf("scope[%d]: protocol range %d-%d is invalid", i, scope.Protocol.Start, scope.Protocol.End)
		}
		if !scope.Source.Valid() {
			return nil, fmt.Errorf("scope[%d]: invalid source address range", i)
		}
		if !scope.Destination.Valid() {
			return nil, fmt.Errorf("scope[%d]: invalid destination address range", i)
		}
		if scope.Source.Family() != scope.Destination.Family() {
			return nil, fmt.Errorf("scope[%d]: source and destination use different IP families", i)
		}
		ports, err := CanonicalizePortRanges(scope.DestinationPorts)
		if err != nil {
			return nil, fmt.Errorf("scope[%d]: %w", i, err)
		}
		if len(ports) == 0 {
			ports = []PortRange{allPortRange()}
		}
		for _, port := range ports {
			boxes = append(boxes, box{
				protocol: scope.Protocol,
				source:   scope.Source,
				dest:     scope.Destination,
				ports:    port,
			})
		}
	}
	return boxes, nil
}

type scopeKey struct {
	protocol ProtocolRange
	source   AddressRange
	dest     AddressRange
}

func boxesToScopes(boxes []box) []Scope {
	portsByKey := make(map[scopeKey][]PortRange)
	for _, b := range boxes {
		key := scopeKey{protocol: b.protocol, source: b.source, dest: b.dest}
		portsByKey[key] = append(portsByKey[key], b.ports)
	}

	scopes := make([]Scope, 0, len(portsByKey))
	for key, ports := range portsByKey {
		canonicalPorts, err := CanonicalizePortRanges(ports)
		if err != nil {
			// Boxes were constructed from validated ranges, so this cannot
			// happen unless internal code is invalid.
			panic(err)
		}
		scopes = append(scopes, Scope{
			Protocol:         key.protocol,
			Source:           key.source,
			Destination:      key.dest,
			DestinationPorts: canonicalPorts,
		})
	}

	scopes = mergeScopes(scopes)
	sortScopes(scopes)
	return scopes
}

func mergeScopes(in []Scope) []Scope {
	out := append([]Scope{}, in...)

	for {
		merged := false
		for i := 0; i < len(out) && !merged; i++ {
			for j := i + 1; j < len(out); j++ {
				if combined, ok := mergeAdjacentSource(out[i], out[j]); ok {
					out[i] = combined
					out = append(out[:j], out[j+1:]...)
					merged = true
					break
				}
				if combined, ok := mergeAdjacentDestination(out[i], out[j]); ok {
					out[i] = combined
					out = append(out[:j], out[j+1:]...)
					merged = true
					break
				}
			}
		}
		if !merged {
			break
		}
	}

	sortScopes(out)
	return out
}

func mergeAdjacentSource(a, b Scope) (Scope, bool) {
	if compareProtocolRange(a.Protocol, b.Protocol) != 0 {
		return Scope{}, false
	}
	if !addressRangesEqual(a.Destination, b.Destination) {
		return Scope{}, false
	}
	if !portRangesEqual(a.DestinationPorts, b.DestinationPorts) {
		return Scope{}, false
	}
	merged, ok := unionTouchingAddressRange(a.Source, b.Source)
	if !ok {
		return Scope{}, false
	}
	a.Source = merged
	return a, true
}

func mergeAdjacentDestination(a, b Scope) (Scope, bool) {
	if compareProtocolRange(a.Protocol, b.Protocol) != 0 {
		return Scope{}, false
	}
	if !addressRangesEqual(a.Source, b.Source) {
		return Scope{}, false
	}
	if !portRangesEqual(a.DestinationPorts, b.DestinationPorts) {
		return Scope{}, false
	}
	merged, ok := unionTouchingAddressRange(a.Destination, b.Destination)
	if !ok {
		return Scope{}, false
	}
	a.Destination = merged
	return a, true
}

func sortScopes(scopes []Scope) {
	sort.SliceStable(scopes, func(i, j int) bool {
		a, b := scopes[i], scopes[j]
		if c := compareProtocolRange(a.Protocol, b.Protocol); c != 0 {
			return c < 0
		}
		if c := compareAddressRange(a.Source, b.Source); c != 0 {
			return c < 0
		}
		if c := compareAddressRange(a.Destination, b.Destination); c != 0 {
			return c < 0
		}
		return comparePortRanges(a.DestinationPorts, b.DestinationPorts) < 0
	})
}

// ScopesOverlap reports whether two scopes share at least one protocol,
// source address, destination address, and destination port tuple.
func ScopesOverlap(a, b Scope) bool {
	if _, ok := intersectProtocolRange(a.Protocol, b.Protocol); !ok {
		return false
	}
	if _, ok := intersectAddressRange(a.Source, b.Source); !ok {
		return false
	}
	if _, ok := intersectAddressRange(a.Destination, b.Destination); !ok {
		return false
	}
	return portSetsIntersect(a.DestinationPorts, b.DestinationPorts)
}
