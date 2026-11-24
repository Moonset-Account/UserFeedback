// Package projection projects service announcements received by the campus
// device discovery gateway into per-network resolvable record sets.
//
// An announcement carries service instances (each with a purpose label and a
// validity window), service-owned host aliases, address records and the
// destination networks ("zones") that are allowed to see services. Project is
// a pure snapshot function: it reads an announcement plus a point in time and
// never mutates its input.
package projection

import (
	"fmt"
	"net/netip"
	"time"
)

// Window is a half-open validity interval [NotBefore, Until). A zero bound
// means the interval is unbounded on that side, so the zero Window is always
// active.
type Window struct {
	NotBefore time.Time
	Until     time.Time
}

// ActiveAt reports whether t is inside the window.
func (w Window) ActiveAt(t time.Time) bool {
	if !w.NotBefore.IsZero() && t.Before(w.NotBefore) {
		return false
	}
	if !w.Until.IsZero() && !t.Before(w.Until) {
		return false
	}
	return true
}

// PendingAt reports whether t is before the start of the window.
func (w Window) PendingAt(t time.Time) bool {
	return !w.NotBefore.IsZero() && t.Before(w.NotBefore)
}

// Announcement is one snapshot of gateway input. Services, aliases, addresses
// and zone policies are global to the snapshot; Project decides what each
// destination zone may see.
type Announcement struct {
	Services  []Service
	Aliases   []Alias
	Addresses []Address
	Zones     []Zone
	// Withdrawn lists service IDs retracted by this snapshot. A retracted
	// service contributes no aliases, no addresses and no records in any
	// zone, even if its validity window is still open.
	Withdrawn []string
}

// Service is a service instance announced by the gateway.
type Service struct {
	ID      string
	Purpose string
	// Private services are never selected by purpose label; a zone must list
	// their ID explicitly in Zone.ApprovedServices.
	Private  bool
	Validity Window
	// EntryNames are the host aliases clients in a zone may query. Aliases
	// used to resolve these names must be owned by this service.
	EntryNames []string
}

// TargetKind says what an alias points at.
type TargetKind int

const (
	// TargetAddress points at an Address record by ID.
	TargetAddress TargetKind = iota + 1
	// TargetAlias points at another alias by name.
	TargetAlias
)

// String implements fmt.Stringer.
func (k TargetKind) String() string {
	switch k {
	case TargetAddress:
		return "address"
	case TargetAlias:
		return "alias"
	default:
		return fmt.Sprintf("TargetKind(%d)", int(k))
	}
}

// Target is an alias target reference.
type Target struct {
	Kind TargetKind
	Ref  string
}

// Alias is a service-owned host alias. Every alias in a resolution chain must
// be owned by the same service; sharing happens only through Address records.
type Alias struct {
	Name     string
	Owner    string
	Target   Target
	Validity Window
}

// Address is an address record that one or more services may share.
type Address struct {
	ID       string
	Name     string
	IP       netip.Addr
	Validity Window
}

// Zone is one destination network and its approval policy.
//
// Public services are approved when their purpose appears in
// ApprovedPurposes or their ID appears in ApprovedServices. Private services
// are approved only by explicit ID. DeniedServices always wins.
type Zone struct {
	Name             string
	ApprovedPurposes []string
	ApprovedServices []string
	DeniedServices   []string
}

// RecordType is the type of a projected record.
type RecordType string

const (
	// RecordA maps a canonical address name to an IP address.
	RecordA RecordType = "A"
	// RecordCNAME maps an alias name to another host name.
	RecordCNAME RecordType = "CNAME"
)

// Record is one projected, resolvable record. Records are comparable and are
// deduplicated per zone.
type Record struct {
	Name  string
	Type  RecordType
	Value string
}

// String implements fmt.Stringer.
func (r Record) String() string {
	return fmt.Sprintf("%s %s %s", r.Name, r.Type, r.Value)
}

// ServiceStatus describes how a service fared in one zone projection.
type ServiceStatus string

const (
	// StatusPublished means every entry name resolved and records were emitted.
	StatusPublished ServiceStatus = "published"
	// StatusIncomplete means a dependency was missing or inactive; nothing was
	// emitted for the service and it is retried on the next announcement.
	StatusIncomplete ServiceStatus = "incomplete"
	// StatusWithdrawn means the service was retracted.
	StatusWithdrawn ServiceStatus = "withdrawn"
	// StatusPending means the service validity window has not started.
	StatusPending ServiceStatus = "pending"
	// StatusExpired means the service validity window has ended.
	StatusExpired ServiceStatus = "expired"
)

// IssueReason is a stable code explaining why a service is incomplete.
type IssueReason string

const (
	// MissingAlias: an entry name has no announced alias record.
	MissingAlias IssueReason = "missing_alias"
	// MissingAliasTarget: a chained alias target has no announced alias record.
	MissingAliasTarget IssueReason = "missing_alias_target"
	// ForeignAlias: an alias in the chain is owned by another service.
	ForeignAlias IssueReason = "alias_foreign_owner"
	// AliasExpired: an alias in the chain is past its validity window.
	AliasExpired IssueReason = "alias_expired"
	// AliasNotYetValid: an alias in the chain is before its validity window.
	AliasNotYetValid IssueReason = "alias_not_yet_valid"
	// MissingAddress: an alias references an address record that is absent.
	MissingAddress IssueReason = "missing_address"
	// AddressExpired: a referenced address record is past its validity window.
	AddressExpired IssueReason = "address_expired"
	// AddressNotYetValid: a referenced address is before its validity window.
	AddressNotYetValid IssueReason = "address_not_yet_valid"
	// AliasCycle: the alias chain loops.
	AliasCycle IssueReason = "alias_cycle"
	// AliasChainTooDeep: the alias chain exceeds maxAliasDepth.
	AliasChainTooDeep IssueReason = "alias_chain_too_deep"
	// InvalidTarget: an alias has an unknown target kind (Validate rejects it).
	InvalidTarget IssueReason = "invalid_target"
)

// Issue describes one unresolved dependency.
type Issue struct {
	Reason IssueReason
	Entry  string
	Ref    string
}

// String implements fmt.Stringer.
func (i Issue) String() string {
	return fmt.Sprintf("%s (entry %q, ref %q)", i.Reason, i.Entry, i.Ref)
}

// ServiceResult reports one approved service in one zone. Services that the
// zone is not allowed to see never appear.
type ServiceResult struct {
	ID      string
	Purpose string
	Private bool
	Status  ServiceStatus
	// RecordCount is the number of chain records emitted before zone-wide
	// deduplication. It is zero unless Status is StatusPublished.
	RecordCount int
	Issues      []Issue
}

// ZoneResult is the projection for one destination zone.
type ZoneResult struct {
	Zone     string
	Records  []Record
	Services []ServiceResult
}

// Result is the full projection of one announcement snapshot.
type Result struct {
	AsOf  time.Time
	Zones []ZoneResult
}

// Zone returns the projection for the named zone.
func (r Result) Zone(name string) (ZoneResult, bool) {
	for _, z := range r.Zones {
		if z.Zone == name {
			return z, true
		}
	}
	return ZoneResult{}, false
}
