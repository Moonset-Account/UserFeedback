package negotiation

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// Protocol is an 8-bit IP protocol number.
type Protocol uint8

// Common IP protocol numbers used by branch security policies.
const (
	ProtocolICMP    Protocol = 1
	ProtocolTCP     Protocol = 6
	ProtocolUDP     Protocol = 17
	ProtocolICMPv6  Protocol = 58
	ProtocolAnyLow  Protocol = 0
	ProtocolAnyHigh Protocol = 255
)

// ProtocolRange is an inclusive, contiguous range of IP protocol numbers.
type ProtocolRange struct {
	Start Protocol `json:"start"`
	End   Protocol `json:"end"`
}

// ProtocolSet is a canonical collection of protocol ranges.
type ProtocolSet []ProtocolRange

// FullProtocolRange returns the wildcard protocol range 0-255.
func FullProtocolRange() ProtocolRange {
	return ProtocolRange{Start: ProtocolAnyLow, End: ProtocolAnyHigh}
}

// ParseProtocol parses a protocol name or numeric IP protocol identifier.
func ParseProtocol(s string) (Protocol, error) {
	v := strings.TrimSpace(s)
	if v == "" {
		return 0, fmt.Errorf("protocol is empty")
	}
	if n, err := strconv.ParseUint(v, 10, 8); err == nil {
		return Protocol(n), nil
	}

	switch strings.ToLower(v) {
	case "icmp":
		return ProtocolICMP, nil
	case "tcp":
		return ProtocolTCP, nil
	case "udp":
		return ProtocolUDP, nil
	case "ipv6-icmp", "icmpv6":
		return ProtocolICMPv6, nil
	default:
		return 0, fmt.Errorf("unknown protocol %q", s)
	}
}

// ParseProtocolSet parses "tcp", "6", "tcp,udp", "1-5", "any", or "*".
// An empty string is treated as "any".
func ParseProtocolSet(spec string) (ProtocolSet, error) {
	spec = strings.TrimSpace(spec)
	if spec == "" || strings.EqualFold(spec, "any") || spec == "*" {
		return ProtocolSet{FullProtocolRange()}, nil
	}

	var ranges ProtocolSet
	for _, part := range strings.Split(spec, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			return nil, fmt.Errorf("empty protocol entry in %q", spec)
		}
		if strings.EqualFold(part, "any") || part == "*" {
			ranges = append(ranges, FullProtocolRange())
			continue
		}

		if dash := strings.Index(part, "-"); dash >= 0 {
			startText := strings.TrimSpace(part[:dash])
			endText := strings.TrimSpace(part[dash+1:])
			start, err := ParseProtocol(startText)
			if err != nil {
				return nil, err
			}
			end, err := ParseProtocol(endText)
			if err != nil {
				return nil, err
			}
			if start > end {
				return nil, fmt.Errorf("protocol range %q has start greater than end", part)
			}
			ranges = append(ranges, ProtocolRange{Start: start, End: end})
			continue
		}

		p, err := ParseProtocol(part)
		if err != nil {
			return nil, err
		}
		ranges = append(ranges, ProtocolRange{Start: p, End: p})
	}

	return CanonicalizeProtocols(ranges)
}

// CanonicalizeProtocols sorts protocol ranges and merges overlapping or
// adjacent ranges.
func CanonicalizeProtocols(in ProtocolSet) (ProtocolSet, error) {
	if len(in) == 0 {
		return nil, nil
	}
	for _, r := range in {
		if r.Start > r.End {
			return nil, fmt.Errorf("protocol range %d-%d has start greater than end", r.Start, r.End)
		}
	}

	rs := append(ProtocolSet(nil), in...)
	sort.Slice(rs, func(i, j int) bool {
		if rs[i].Start != rs[j].Start {
			return rs[i].Start < rs[j].Start
		}
		return rs[i].End < rs[j].End
	})

	out := ProtocolSet{rs[0]}
	for _, r := range rs[1:] {
		last := &out[len(out)-1]
		if int(r.Start) <= int(last.End)+1 {
			if r.End > last.End {
				last.End = r.End
			}
			continue
		}
		out = append(out, r)
	}
	return out, nil
}

// String returns a compact canonical representation.
func (r ProtocolRange) String() string {
	if r.Start == ProtocolAnyLow && r.End == ProtocolAnyHigh {
		return "any"
	}
	if r.Start == r.End {
		return protocolName(r.Start)
	}
	return fmt.Sprintf("%d-%d", r.Start, r.End)
}

// String returns a comma-separated canonical protocol set.
func (s ProtocolSet) String() string {
	parts := make([]string, 0, len(s))
	for _, r := range s {
		parts = append(parts, r.String())
	}
	return strings.Join(parts, ",")
}

// MarshalJSON encodes a protocol range as its compact string form.
func (r ProtocolRange) MarshalJSON() ([]byte, error) {
	return json.Marshal(r.String())
}

// UnmarshalJSON accepts either a compact protocol string or the explicit
// {"start":n,"end":n} object form.
func (r *ProtocolRange) UnmarshalJSON(data []byte) error {
	var text string
	if err := json.Unmarshal(data, &text); err == nil {
		set, err := ParseProtocolSet(text)
		if err != nil {
			return err
		}
		if len(set) != 1 {
			return fmt.Errorf("protocol %q is not one contiguous range", text)
		}
		*r = set[0]
		return nil
	}

	type alias ProtocolRange
	var explicit alias
	if err := json.Unmarshal(data, &explicit); err != nil {
		return err
	}
	*r = ProtocolRange(explicit)
	if r.Start > r.End {
		return fmt.Errorf("protocol range %d-%d has start greater than end", r.Start, r.End)
	}
	return nil
}

func protocolName(p Protocol) string {
	switch p {
	case ProtocolICMP:
		return "icmp"
	case ProtocolTCP:
		return "tcp"
	case ProtocolUDP:
		return "udp"
	case ProtocolICMPv6:
		return "icmpv6"
	default:
		return strconv.Itoa(int(p))
	}
}

func compareProtocolRange(a, b ProtocolRange) int {
	if a.Start != b.Start {
		if a.Start < b.Start {
			return -1
		}
		return 1
	}
	if a.End != b.End {
		if a.End < b.End {
			return -1
		}
		return 1
	}
	return 0
}

func protocolRangesEqual(a, b ProtocolRange) bool {
	return a == b
}
