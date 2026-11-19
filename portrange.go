package trustlist

import (
	"fmt"
	"strconv"
	"strings"
)

// PortRange scopes a trust-list line to a set of ports. The zero value matches
// no port at all, so an unset range fails closed; use AnyPort for "every port".
type PortRange struct {
	any    bool
	lo, hi int
}

// AnyPort returns a PortRange that matches every port from 1 to 65535.
func AnyPort() PortRange { return PortRange{any: true} }

// ExactPort returns a PortRange that matches only port.
func ExactPort(port int) PortRange { return PortRange{lo: port, hi: port} }

// Any reports whether the range covers every port.
func (r PortRange) Any() bool { return r.any }

// Contains reports whether port is inside the range. Both endpoints are
// inclusive.
func (r PortRange) Contains(port int) bool {
	if port < 1 || port > 65535 {
		return false
	}
	if r.any {
		return true
	}
	return port >= r.lo && port <= r.hi
}

// String renders the range in trust-list syntax.
func (r PortRange) String() string {
	switch {
	case r.any:
		return "*"
	case r.lo == r.hi:
		return strconv.Itoa(r.lo)
	default:
		return fmt.Sprintf("%d-%d", r.lo, r.hi)
	}
}

// ParsePortRange parses "*", a single port such as "22", or an inclusive range
// such as "2200-2222".
func ParsePortRange(spec string) (PortRange, error) {
	s := strings.TrimSpace(spec)
	if s == "" {
		return PortRange{}, fmt.Errorf("empty port range")
	}
	if s == "*" {
		return AnyPort(), nil
	}

	lo, hi := s, s
	if i := strings.IndexByte(s, '-'); i >= 0 {
		lo, hi = s[:i], s[i+1:]
	}

	loPort, err := parsePort(lo)
	if err != nil {
		return PortRange{}, err
	}
	hiPort, err := parsePort(hi)
	if err != nil {
		return PortRange{}, err
	}
	if loPort > hiPort {
		return PortRange{}, fmt.Errorf("port range %q is inverted", s)
	}
	return PortRange{lo: loPort, hi: hiPort}, nil
}

func parsePort(s string) (int, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return 0, fmt.Errorf("empty port")
	}
	port, err := strconv.Atoi(s)
	if err != nil {
		return 0, fmt.Errorf("port %q is not a number", s)
	}
	if port < 1 || port > 65535 {
		return 0, fmt.Errorf("port %d is outside 1..65535", port)
	}
	return port, nil
}
