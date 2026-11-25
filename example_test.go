package mirror_test

import (
	"fmt"

	"example.com/mirror"
)

// Example shows the boundary-switch scenario: one mirrored frame that is also
// authorized for two receivers, each retaining a different byte prefix.
func Example() {
	frame := make([]byte, 60) // eth(14) vlan(4) ipv4(20) tcp(22)

	cfg := mirror.Config{Receivers: []mirror.Receiver{
		{
			Name:      "troubleshooting",
			Ingresses: []string{"ge-0/0/1"},
			VLANs:     []int{100},
			MaxDepth:  mirror.DepthL4,
		},
		{
			Name:      "security-archive",
			Ingresses: []string{"ge-0/0/1", "ge-0/0/2"},
			VLANs:     []int{100},
			MaxDepth:  mirror.DepthL2,
		},
	}}

	capture := mirror.Capture{
		ID:      "frame-1",
		Ingress: "ge-0/0/1",
		VLANs:   []int{100},
		Frame:   frame,
		Fields: []mirror.FieldRange{
			{Name: "eth", Depth: mirror.DepthL2, Start: 0, End: 14},
			{Name: "vlan", Depth: mirror.DepthL2, Start: 14, End: 18},
			{Name: "ipv4", Depth: mirror.DepthL3, Start: 18, End: 38},
			{Name: "tcp", Depth: mirror.DepthL4, Start: 38, End: 60},
		},
	}

	result := mirror.Distribute(cfg, capture)
	for _, c := range result.Copies {
		if !c.Allowed {
			fmt.Printf("%s denied: %s\n", c.Receiver, c.Denied)
			continue
		}
		fmt.Printf("%s got %d bytes (%v)", c.Receiver, len(c.Data), c.Fields)
		if c.Truncation != nil {
			fmt.Printf(" stopped at %s: %s", c.Truncation.Field, c.Truncation.Reason)
		}
		fmt.Println()
	}

	// Output:
	// troubleshooting got 60 bytes ([eth vlan ipv4 tcp])
	// security-archive got 18 bytes ([eth vlan]) stopped at ipv4: depth_limit
}
