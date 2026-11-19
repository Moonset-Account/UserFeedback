package connpool

import (
	"fmt"
	"net"
	"strconv"
	"strings"
)

// ProtocolHTTP2 is the application protocol name (ALPN identifier) that may be
// multiplexed in this pool. Records negotiated with any other protocol are
// kept for observability but are never reused.
const ProtocolHTTP2 = "h2"

// HandshakeRecord is the locally observed result of a TLS/HTTP handshake. It is
// the only source of truth for what a connection can serve:
//
//   - Protocol is the negotiated protocol, normally "h2" for HTTP/2.
//   - VerifiedDomains lists every name proven by the peer certificate. Wildcard
//     entries such as "*.example.com" are accepted.
//   - RemoteAddr is the peer address that was actually connected to, as an IP
//     literal, optionally with a port ("10.0.0.10:443").
//   - Zone is the isolation zone the connection belongs to.
type HandshakeRecord struct {
	ConnectionID    string
	Protocol        string
	VerifiedDomains []string
	RemoteAddr      string
	Zone            string
}

type connection struct {
	id       string
	protocol string
	verified []string
	remote   address
	zone     string

	// revoked holds domains that received a 421 from this connection. The
	// revocation is deliberately per (domain, connection), never global.
	revoked map[string]bool
	// active holds ids of streams that have been opened and not yet ended.
	active map[uint64]struct{}
}

// address is a canonical IP endpoint. Ports are optional because a resolver is
// allowed to return bare IP literals.
type address struct {
	ip      net.IP
	port    string
	hasPort bool
}

func (a address) String() string {
	if a.hasPort {
		return net.JoinHostPort(a.ip.String(), a.port)
	}
	return a.ip.String()
}

func normalizeDomain(raw string, allowWildcard bool) (string, error) {
	domain := strings.ToLower(strings.TrimSpace(raw))
	domain = strings.TrimSuffix(domain, ".")
	if domain == "" {
		return "", ErrEmptyDomain
	}
	if len(domain) > 253 {
		return "", fmt.Errorf("%w: domain longer than 253 bytes", ErrInvalidDomain)
	}

	labels := strings.Split(domain, ".")
	for i, label := range labels {
		if i == 0 && allowWildcard && label == "*" {
			if len(labels) < 3 {
				return "", fmt.Errorf("%w: wildcard must cover at least two labels", ErrInvalidDomain)
			}
			continue
		}
		if label == "" {
			return "", fmt.Errorf("%w: empty label in %q", ErrInvalidDomain, raw)
		}
		if len(label) > 63 {
			return "", fmt.Errorf("%w: label longer than 63 bytes in %q", ErrInvalidDomain, raw)
		}
		if label[0] == '-' || label[len(label)-1] == '-' {
			return "", fmt.Errorf("%w: label starts or ends with '-' in %q", ErrInvalidDomain, raw)
		}
		for _, r := range label {
			if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') || r == '-' {
				continue
			}
			return "", fmt.Errorf("%w: unsupported character %q in %q", ErrInvalidDomain, r, raw)
		}
	}
	return domain, nil
}

func normalizeVerifiedDomains(raw []string) ([]string, error) {
	if len(raw) == 0 {
		return nil, fmt.Errorf("%w: no verified domains", ErrInvalidRecord)
	}
	seen := make(map[string]bool, len(raw))
	verified := make([]string, 0, len(raw))
	for _, item := range raw {
		domain, err := normalizeDomain(item, true)
		if err != nil {
			return nil, fmt.Errorf("%w: verified domain %q: %v", ErrInvalidRecord, item, err)
		}
		if seen[domain] {
			continue
		}
		seen[domain] = true
		verified = append(verified, domain)
	}
	return verified, nil
}

// certificateCovers reports whether any verified name proves the requested
// domain. A wildcard covers exactly one additional label, so "*.example.com"
// covers "a.example.com" but not "example.com" or "a.b.example.com".
func certificateCovers(verified []string, domain string) bool {
	for _, pattern := range verified {
		if pattern == domain {
			return true
		}
		if !strings.HasPrefix(pattern, "*.") {
			continue
		}
		suffix := pattern[1:] // ".example.com"
		if !strings.HasSuffix(domain, suffix) {
			continue
		}
		prefix := domain[:len(domain)-len(suffix)]
		if prefix != "" && !strings.Contains(prefix, ".") {
			return true
		}
	}
	return false
}

func parseAddress(raw string) (address, error) {
	value := strings.TrimSpace(raw)
	if value == "" {
		return address{}, fmt.Errorf("%w: empty address", ErrInvalidAddress)
	}

	host := value
	port := ""
	hasPort := false
	if h, p, err := net.SplitHostPort(value); err == nil {
		host = h
		if p != "" {
			number, convErr := strconv.Atoi(p)
			if convErr != nil || number < 0 || number > 65535 {
				return address{}, fmt.Errorf("%w: invalid port in %q", ErrInvalidAddress, raw)
			}
			port = strconv.Itoa(number)
			hasPort = true
		}
	} else if strings.HasPrefix(host, "[") && strings.HasSuffix(host, "]") {
		// A bracketed IPv6 literal without a port.
		host = host[1 : len(host)-1]
	}

	ip := net.ParseIP(host)
	if ip == nil {
		return address{}, fmt.Errorf("%w: %q is not an IP literal", ErrInvalidAddress, raw)
	}
	if ipv4 := ip.To4(); ipv4 != nil {
		ip = ipv4
	}
	return address{ip: ip, port: port, hasPort: hasPort}, nil
}

func normalizeAddresses(raw []string) ([]address, error) {
	seen := make(map[string]bool, len(raw))
	addresses := make([]address, 0, len(raw))
	for _, item := range raw {
		if strings.TrimSpace(item) == "" {
			continue
		}
		addr, err := parseAddress(item)
		if err != nil {
			return nil, err
		}
		key := addr.String()
		if seen[key] {
			continue
		}
		seen[key] = true
		addresses = append(addresses, addr)
	}
	if len(addresses) == 0 {
		return nil, ErrNoAddresses
	}
	return addresses, nil
}

func addressesMatch(remote address, resolved []address) bool {
	for _, candidate := range resolved {
		if !candidate.ip.Equal(remote.ip) {
			continue
		}
		if candidate.hasPort && remote.hasPort && candidate.port != remote.port {
			continue
		}
		return true
	}
	return false
}

func addressStrings(addresses []address) []string {
	out := make([]string, len(addresses))
	for i, addr := range addresses {
		out[i] = addr.String()
	}
	return out
}

func newConnection(record HandshakeRecord) (*connection, error) {
	id := strings.TrimSpace(record.ConnectionID)
	if id == "" {
		return nil, fmt.Errorf("%w: empty connection id", ErrInvalidRecord)
	}
	protocol := strings.ToLower(strings.TrimSpace(record.Protocol))
	if protocol == "" {
		return nil, fmt.Errorf("%w: empty protocol", ErrInvalidRecord)
	}
	verified, err := normalizeVerifiedDomains(record.VerifiedDomains)
	if err != nil {
		return nil, err
	}
	remote, err := parseAddress(record.RemoteAddr)
	if err != nil {
		return nil, fmt.Errorf("%w: remote address: %v", ErrInvalidRecord, err)
	}
	zone := strings.TrimSpace(record.Zone)
	if zone == "" {
		return nil, fmt.Errorf("%w: empty isolation zone", ErrInvalidRecord)
	}

	return &connection{
		id:       id,
		protocol: protocol,
		verified: verified,
		remote:   remote,
		zone:     zone,
		revoked:  make(map[string]bool),
		active:   make(map[uint64]struct{}),
	}, nil
}

func isHTTP2(protocol string) bool {
	return strings.EqualFold(protocol, ProtocolHTTP2)
}
