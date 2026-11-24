package negotiation

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// Valid destination port bounds. Port zero is reserved and is not used by
// ordinary branch business policies.
const (
	MinPort uint16 = 1
	MaxPort uint16 = 65535
)

// PortRange is an inclusive destination-port range.
type PortRange struct {
	Start uint16 `json:"start"`
	End   uint16 `json:"end"`
}

// SinglePort returns an inclusive range containing exactly port p.
func SinglePort(p uint16) PortRange {
	return PortRange{Start: p, End: p}
}

// Ports returns singleton ranges for the supplied ports.
func Ports(ports ...uint16) []PortRange {
	out := make([]PortRange, 0, len(ports))
	for _, p := range ports {
		out = append(out, SinglePort(p))
	}
	return out
}

// ParsePortRange parses "80" or "8000-9000".
func ParsePortRange(spec string) (PortRange, error) {
	spec = strings.TrimSpace(spec)
	if spec == "" {
		return PortRange{}, fmt.Errorf("port range is empty")
	}
	if dash := strings.Index(spec, "-"); dash >= 0 {
		start, err := parsePort(spec[:dash])
		if err != nil {
			return PortRange{}, err
		}
		end, err := parsePort(spec[dash+1:])
		if err != nil {
			return PortRange{}, err
		}
		r := PortRange{Start: start, End: end}
		if err := validatePortRange(r); err != nil {
			return PortRange{}, err
		}
		return r, nil
	}
	p, err := parsePort(spec)
	if err != nil {
		return PortRange{}, err
	}
	return SinglePort(p), nil
}

// MustParsePortRange parses a port range or panics.
func MustParsePortRange(spec string) PortRange {
	r, err := ParsePortRange(spec)
	if err != nil {
		panic(err)
	}
	return r
}

// String renders a singleton as "80" and a range as "8000-9000".
func (r PortRange) String() string {
	if r.Start == r.End {
		return strconv.Itoa(int(r.Start))
	}
	return fmt.Sprintf("%d-%d", r.Start, r.End)
}

// CanonicalizePortRanges validates, sorts, and merges overlapping or adjacent
// port ranges.
func CanonicalizePortRanges(in []PortRange) ([]PortRange, error) {
	if len(in) == 0 {
		return nil, nil
	}
	for _, r := range in {
		if err := validatePortRange(r); err != nil {
			return nil, err
		}
	}

	rs := append([]PortRange(nil), in...)
	sort.Slice(rs, func(i, j int) bool {
		if rs[i].Start != rs[j].Start {
			return rs[i].Start < rs[j].Start
		}
		return rs[i].End < rs[j].End
	})

	out := []PortRange{rs[0]}
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

func validatePortRange(r PortRange) error {
	if r.Start < MinPort || r.End > MaxPort {
		return fmt.Errorf("port range %d-%d is outside valid ports %d-%d", r.Start, r.End, MinPort, MaxPort)
	}
	if r.Start > r.End {
		return fmt.Errorf("port range %d-%d has start greater than end", r.Start, r.End)
	}
	return nil
}

func parsePort(s string) (uint16, error) {
	v := strings.TrimSpace(s)
	n, err := strconv.ParseUint(v, 10, 16)
	if err != nil {
		return 0, fmt.Errorf("invalid port %q: %w", s, err)
	}
	p := uint16(n)
	if p < MinPort {
		return 0, fmt.Errorf("port %d is outside valid ports %d-%d", p, MinPort, MaxPort)
	}
	return p, nil
}

func allPortRange() PortRange {
	return PortRange{Start: MinPort, End: MaxPort}
}

func portRangesEqual(a, b []PortRange) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func comparePortRanges(a, b []PortRange) int {
	n := len(a)
	if len(b) < n {
		n = len(b)
	}
	for i := 0; i < n; i++ {
		if a[i].Start != b[i].Start {
			if a[i].Start < b[i].Start {
				return -1
			}
			return 1
		}
		if a[i].End != b[i].End {
			if a[i].End < b[i].End {
				return -1
			}
			return 1
		}
	}
	if len(a) != len(b) {
		if len(a) < len(b) {
			return -1
		}
		return 1
	}
	return 0
}

func rangesOverlapPort(a, b PortRange) bool {
	return a.Start <= b.End && b.Start <= a.End
}

func portSetsIntersect(a, b []PortRange) bool {
	for _, x := range a {
		for _, y := range b {
			if rangesOverlapPort(x, y) {
				return true
			}
		}
	}
	return false
}
