// Package recdns implements a small recursive DNS-style resolver that is
// driven entirely by a local, in-memory message fixture. No network access is
// performed anywhere in the package: every "wire" exchange is a call into the
// Fixture's Handler.
package recdns

import (
	"strings"
	"time"
)

// Name is an absolute, dotted domain name. The root zone is ".".
type Name string

// Type is the query/record type, e.g. A, AAAA, CNAME or TXT.
type Type string

// Server identifies an authoritative name server inside the fixture.
type Server string

const (
	TypeA     Type = "A"
	TypeAAAA  Type = "AAAA"
	TypeCNAME Type = "CNAME"
	TypeTXT   Type = "TXT"
)

// RCode is the response status carried by a fixture Message. Result-level
// statuses (StatusNODATA, StatusLoop) are derived by the resolver and never
// appear in fixture messages.
type RCode string

const (
	// Fixture/protocol level codes.
	RCodeOK       RCode = "NOERROR"
	RCodeNXDomain RCode = "NXDOMAIN"
	RCodeServFail RCode = "SERVFAIL"

	// Resolver-level result codes.
	StatusOK       RCode = "NOERROR"
	StatusNXDOMAIN RCode = "NXDOMAIN"
	StatusNODATA   RCode = "NODATA" // name exists, but the asked type is absent
	StatusLoop     RCode = "LOOP"   // a CNAME cycle was detected
)

// RR is a resource record. Origin is the authoritative zone that supplied the
// record (the "original zone"), and TTL is the freshness lifetime granted by
// that zone.
type RR struct {
	Name   Name
	Type   Type
	Value  string
	Origin Name
	TTL    time.Duration
}

// SOA models the SOA authority record attached to negative answers. Only the
// negative-cache minimum field is relevant to this resolver.
type SOA struct {
	Name   Name
	Origin Name
	MinTTL time.Duration
}

// Delegation describes a zone cut: the zone name, the servers authoritative
// for it, the TTL of the delegation information and the zone that issued the
// referral.
type Delegation struct {
	Zone    Name
	Servers []Server
	TTL     time.Duration
	Origin  Name
}

// Query is one resolution step's question to a single server.
type Query struct {
	Name Name
	Type Type
	At   time.Time
}

// Message is a fixture-supplied DNS response. A non-nil Delegation means the
// message is a referral and Answers/Authority must be empty.
type Message struct {
	Query      Query
	Server     Server
	RCode      RCode
	Delegation *Delegation
	Answers    []RR
	Authority  *SOA
}

// QueryLog records one attempted server exchange, in arrival order. Error is
// nil for successful exchanges.
type QueryLog struct {
	Server Server
	Name   Name
	Type   Type
	At     time.Time
	Error  error
}

// parentZone returns the zone one label above n. parentZone(".") == ".".
func parentZone(n Name) Name {
	s := string(n)
	if s == "." || s == "" {
		return "."
	}
	s = strings.TrimSuffix(s, ".")
	i := strings.IndexByte(s, '.')
	if i < 0 {
		return "."
	}
	return Name(s[i+1:] + ".")
}

// zoneMatches reports whether name lives inside zone (root matches everything).
func zoneMatches(zone, name Name) bool {
	if zone == "." {
		return true
	}
	if name == zone {
		return true
	}
	return strings.HasSuffix(string(name), "."+string(zone))
}
