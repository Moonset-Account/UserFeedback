package negotiation_test

import (
	"fmt"

	negotiation "policynegotiation"
)

func mustRange(spec string) negotiation.AddressRange {
	r, err := negotiation.ParseAddressRange(spec)
	if err != nil {
		panic(err)
	}
	return r
}

func ExampleNegotiate() {
	beijing := negotiation.Party{
		ID: "beijing",
		Send: negotiation.Policy{Grants: []negotiation.Grant{
			{
				Protocol:    "tcp",
				Source:      mustRange("10.10.0.0/24"),
				Destination: mustRange("10.20.0.0/24"),
				Ports:       negotiation.Ports(80, 443),
			},
			{
				Protocol:    "tcp",
				Source:      mustRange("10.10.1.0/24"),
				Destination: mustRange("10.20.0.0/24"),
				Ports:       negotiation.Ports(8080),
			},
		}},
	}
	shanghai := negotiation.Party{
		ID: "shanghai",
		Receive: negotiation.Policy{
			Grants: []negotiation.Grant{{
				Protocol:    "tcp",
				Source:      mustRange("10.10.0.0/23"),
				Destination: mustRange("10.20.0.0/24"),
				Ports:       negotiation.Ports(80, 443, 8080),
			}},
			Exclusions: negotiation.Exclusions{
				DestinationPorts: []negotiation.PortRange{negotiation.SinglePort(22)},
			},
		},
	}

	result, err := negotiation.Negotiate(beijing, shanghai)
	if err != nil {
		panic(err)
	}
	for _, rule := range result.Forward.Agreed {
		fmt.Printf("%s %s %s->%s ports%v\n",
			rule.Direction, rule.Protocol, rule.Source, rule.Destination, rule.DestinationPorts)
	}
	// Output:
	// beijing->shanghai tcp 10.10.0.0-10.10.0.255->10.20.0.0-10.20.0.255 ports[80 443]
	// beijing->shanghai tcp 10.10.1.0-10.10.1.255->10.20.0.0-10.20.0.255 ports[8080]
}
