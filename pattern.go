package trustlist

import (
	"net"
	"strings"
)

// canonicalName normalises a host name or a name pattern so equivalent
// spellings compare equal:
//
//   - surrounding whitespace is removed;
//   - surrounding square brackets are removed, so "[::1]" and "::1" agree;
//   - one trailing dot is removed, so "db.example.com." and "db.example.com"
//     agree;
//   - ASCII letters are lower-cased, because DNS names are case-insensitive.
func canonicalName(s string) string {
	s = strings.TrimSpace(s)
	if len(s) >= 2 && s[0] == '[' && s[len(s)-1] == ']' {
		s = s[1 : len(s)-1]
	}
	s = strings.TrimSuffix(s, ".")
	return strings.ToLower(s)
}

// matchName reports whether one trust-list pattern matches host. The pattern
// may be a glob, a CIDR block, or a literal name. An empty pattern never
// matches, so a malformed entry can never widen scope.
func matchName(pattern, host string) bool {
	p := canonicalName(pattern)
	h := canonicalName(host)
	if p == "" || h == "" {
		return false
	}
	if strings.Contains(p, "/") {
		if _, network, err := net.ParseCIDR(p); err == nil {
			if ip := net.ParseIP(h); ip != nil {
				return network.Contains(ip)
			}
			return false
		}
	}
	return globMatch(p, h)
}

// matchNameAny reports whether any pattern matches host.
func matchNameAny(patterns []string, host string) bool {
	for _, p := range patterns {
		if matchName(p, host) {
			return true
		}
	}
	return false
}

// globMatch implements '*' (any run of runes, including none) and '?' (exactly
// one rune) over already-canonical strings. It runs in rune space so multi-byte
// names behave predictably.
func globMatch(pattern, name string) bool {
	pr := []rune(pattern)
	nr := []rune(name)

	// dp[j] reports whether pattern[0:i] matches name[0:j].
	dp := make([]bool, len(nr)+1)
	dp[0] = true

	for _, c := range pr {
		next := make([]bool, len(nr)+1)
		switch c {
		case '*':
			next[0] = dp[0]
			for j := 1; j <= len(nr); j++ {
				next[j] = dp[j] || next[j-1]
			}
		case '?':
			for j := 1; j <= len(nr); j++ {
				next[j] = dp[j-1]
			}
		default:
			for j := 1; j <= len(nr); j++ {
				next[j] = dp[j-1] && nr[j-1] == c
			}
		}
		dp = next
	}
	return dp[len(nr)]
}
