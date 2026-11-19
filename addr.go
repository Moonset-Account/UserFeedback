package anonymize

import (
	"fmt"
	"net"
)

// Addr is an IPv4 address stored in network byte order.
type Addr [4]byte

// ParseAddr parses a dotted-quad IPv4 address.
func ParseAddr(s string) (Addr, error) {
	ip := net.ParseIP(s)
	if ip == nil {
		return Addr{}, fmt.Errorf("anonymize: parse %q: not an IP address", s)
	}
	ip4 := ip.To4()
	if ip4 == nil {
		return Addr{}, fmt.Errorf("anonymize: parse %q: not an IPv4 address", s)
	}
	var a Addr
	copy(a[:], ip4)
	return a, nil
}

// String formats the address in dotted-quad notation.
func (a Addr) String() string {
	return net.IP(a[:]).String()
}
