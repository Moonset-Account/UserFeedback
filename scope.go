package sourceselect

import "net"

// Scope is the reachability scope of an address or destination.
//
// The numeric values follow the common IPv6 scope numbering so that a larger
// value means a broader scope. ScopeAuto asks DeriveScope to infer the scope
// from the IP address.
type Scope uint8

const (
	ScopeUnknown           Scope = 0
	ScopeAuto              Scope = 0
	ScopeNodeLocal         Scope = 1
	ScopeInterfaceLocal    Scope = 1
	ScopeLinkLocal         Scope = 2
	ScopeAdminLocal        Scope = 4
	ScopeSiteLocal         Scope = 5
	ScopeOrganizationLocal Scope = 8
	ScopeGlobal            Scope = 14
)

// String returns a stable name for the scope.
func (s Scope) String() string {
	switch s {
	case ScopeNodeLocal:
		return "node-local"
	case ScopeLinkLocal:
		return "link-local"
	case ScopeAdminLocal:
		return "admin-local"
	case ScopeSiteLocal:
		return "site-local"
	case ScopeOrganizationLocal:
		return "organization-local"
	case ScopeGlobal:
		return "global"
	default:
		return "unknown"
	}
}

// Covers reports whether source scope s is broad enough to reach a destination
// with scope target. A broader source scope covers narrower scopes; a
// link-local source, for example, cannot cover a global destination.
func (s Scope) Covers(target Scope) bool {
	if s == ScopeUnknown || target == ScopeUnknown {
		return false
	}
	return s >= target
}

// DeriveScope infers a usable scope from a unicast or multicast IP address.
// Explicitly configured Address.Scope values take precedence.
func DeriveScope(ip net.IP) Scope {
	ip = normalizeIP(ip)
	if ip == nil {
		return ScopeUnknown
	}

	if v4 := ip.To4(); v4 != nil {
		switch {
		case v4[0] == 127:
			return ScopeNodeLocal
		case v4[0] == 169 && v4[1] == 254:
			return ScopeLinkLocal
		case v4[0] == 10:
			return ScopeSiteLocal
		case v4[0] == 172 && v4[1] >= 16 && v4[1] <= 31:
			return ScopeSiteLocal
		case v4[0] == 192 && v4[1] == 168:
			return ScopeSiteLocal
		case v4[0] == 100 && v4[1] >= 64 && v4[1] <= 127:
			// RFC 6598 shared/CGNAT space is treated as organization scope.
			return ScopeOrganizationLocal
		case v4[0]&0xf0 == 224:
			// IPv4 multicast scope is normally determined by TTL/admin policy.
			// The link-local block has unambiguous link scope.
			if v4[0] == 224 && v4[1] == 0 && v4[2] == 0 {
				return ScopeLinkLocal
			}
			return ScopeOrganizationLocal
		default:
			return ScopeGlobal
		}
	}

	if ip.Equal(net.IPv6loopback) {
		return ScopeNodeLocal
	}
	if ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() {
		return ScopeLinkLocal
	}
	if ip[0] == 0xff {
		if s := Scope(ip[1] & 0x0f); s != ScopeUnknown {
			return s
		}
		return ScopeGlobal
	}
	// fc00::/7: unique local addressing.
	if ip[0]&0xfe == 0xfc {
		return ScopeOrganizationLocal
	}
	// fec0::/10: deprecated site-local addressing.
	if ip[0] == 0xfe && ip[1]&0xc0 == 0xc0 {
		return ScopeSiteLocal
	}
	return ScopeGlobal
}
