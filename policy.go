package negotiation

import (
	"fmt"
)

// Grant is one requested allow scope submitted by a branch.
//
// Protocol accepts "tcp", "udp", "icmp", a numeric protocol, a comma separated
// protocol set, or "any". An empty Ports slice means all valid destination
// ports.
type Grant struct {
	Protocol    string       `json:"protocol"`
	Source      AddressRange `json:"source"`
	Destination AddressRange `json:"destination"`
	Ports       []PortRange  `json:"ports,omitempty"`
}

// Exclusions removes reserved addresses or individual destination ports from
// the grants in the same Policy. ReservedAddresses applies to both source and
// destination dimensions; the directional address fields cover unusual cases.
type Exclusions struct {
	ReservedAddresses    []AddressRange `json:"reserved_addresses,omitempty"`
	SourceAddresses      []AddressRange `json:"source_addresses,omitempty"`
	DestinationAddresses []AddressRange `json:"destination_addresses,omitempty"`
	DestinationPorts     []PortRange    `json:"destination_ports,omitempty"`
}

// Policy is one branch's effective allowed-send or allowed-receive policy.
type Policy struct {
	Grants     []Grant    `json:"grants,omitempty"`
	Exclusions Exclusions `json:"exclusions,omitempty"`
}

// Party groups a branch's send application and receive application.
type Party struct {
	ID      string `json:"id"`
	Send    Policy `json:"send"`
	Receive Policy `json:"receive"`
}

// Branch is the domain-oriented alias for Party.
type Branch = Party

func (p Policy) compile() ([]box, error) {
	var grants []box
	for i, grant := range p.Grants {
		compiled, err := compileGrant(grant)
		if err != nil {
			return nil, fmt.Errorf("grant[%d]: %w", i, err)
		}
		grants = append(grants, compiled...)
	}

	cuts, err := p.Exclusions.boxes()
	if err != nil {
		return nil, fmt.Errorf("exclusions: %w", err)
	}

	effective := subtractBoxes(grants, cuts)
	return effective, nil
}

func compileGrant(grant Grant) ([]box, error) {
	protocols, err := ParseProtocolSet(grant.Protocol)
	if err != nil {
		return nil, err
	}
	if !grant.Source.Valid() {
		return nil, fmt.Errorf("invalid source address range %q", grant.Source.String())
	}
	if !grant.Destination.Valid() {
		return nil, fmt.Errorf("invalid destination address range %q", grant.Destination.String())
	}
	if grant.Source.Family() != grant.Destination.Family() {
		return nil, fmt.Errorf("source %s and destination %s use different IP families", grant.Source, grant.Destination)
	}

	ports, err := CanonicalizePortRanges(grant.Ports)
	if err != nil {
		return nil, err
	}
	if len(ports) == 0 {
		ports = []PortRange{allPortRange()}
	}

	var boxes []box
	for _, protocol := range protocols {
		for _, port := range ports {
			boxes = append(boxes, box{
				protocol: protocol,
				source:   grant.Source,
				dest:     grant.Destination,
				ports:    port,
			})
		}
	}
	return boxes, nil
}

func (e Exclusions) boxes() ([]box, error) {
	var cuts []box
	fullProtocol := FullProtocolRange()
	fullPorts := allPortRange()

	addAddressCut := func(r AddressRange, asSource, asDestination bool) error {
		if !r.Valid() {
			return fmt.Errorf("invalid reserved address range %q", r.String())
		}
		all := allAddressRange(r.Family())
		if asSource {
			cuts = append(cuts, box{
				protocol: fullProtocol,
				source:   r,
				dest:     all,
				ports:    fullPorts,
			})
		}
		if asDestination {
			cuts = append(cuts, box{
				protocol: fullProtocol,
				source:   all,
				dest:     r,
				ports:    fullPorts,
			})
		}
		return nil
	}

	for i, r := range e.ReservedAddresses {
		if err := addAddressCut(r, true, true); err != nil {
			return nil, fmt.Errorf("reserved_addresses[%d]: %w", i, err)
		}
	}
	for i, r := range e.SourceAddresses {
		if err := addAddressCut(r, true, false); err != nil {
			return nil, fmt.Errorf("source_addresses[%d]: %w", i, err)
		}
	}
	for i, r := range e.DestinationAddresses {
		if err := addAddressCut(r, false, true); err != nil {
			return nil, fmt.Errorf("destination_addresses[%d]: %w", i, err)
		}
	}

	ports, err := CanonicalizePortRanges(e.DestinationPorts)
	if err != nil {
		return nil, err
	}
	for _, port := range ports {
		for _, family := range []int{4, 6} {
			all := allAddressRange(family)
			cuts = append(cuts, box{
				protocol: fullProtocol,
				source:   all,
				dest:     all,
				ports:    port,
			})
		}
	}

	return cuts, nil
}
