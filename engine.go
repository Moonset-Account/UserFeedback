// Package routeauth implements an offline routing authorization decision
// engine. It mirrors the semantics of an RPKI route origin authorization
// (ROA) validation pipeline, but operates entirely on in-memory,
// already-verified authorization records and explicit revocation events.
//
// A route announcement is accepted only when:
//
//  1. an authorization record whose prefix covers the announcement prefix is
//     active at the evaluation instant (inside its validity window and not
//     revoked at that instant),
//  2. the announcement prefix length does not exceed that authorization's
//     maximum prefix length, and
//  3. the announcement origin AS matches the authorization origin AS.
//
// The three outcomes are deliberately distinguished:
//
//   - Valid:    at least one active covering authorization matches;
//   - Invalid:  at least one active covering authorization exists, but none
//     matches (wrong origin and/or prefix too specific);
//   - NotFound: no active authorization covers the announcement at all.
package routeauth

import (
	"errors"
	"fmt"
	"net/netip"
	"sort"
	"sync"
	"time"
)

// State is the outcome of evaluating one announcement.
type State string

const (
	// StateValid means an active covering authorization fully matched.
	StateValid State = "valid"
	// StateInvalid means an active covering authorization existed but none
	// matched the announcement.
	StateInvalid State = "invalid"
	// StateNotFound means no active authorization covered the announcement.
	StateNotFound State = "not_found"
)

// Errors returned for malformed input or unknown records.
var (
	ErrDuplicateRecord = errors.New("routeauth: duplicate authorization record id")
	ErrUnknownRecord   = errors.New("routeauth: unknown authorization record id")
	ErrAlreadyRevoked  = errors.New("routeauth: record already revoked")
	ErrInvalidInput    = errors.New("routeauth: invalid input")
)

// Authorization is an already-verified route origin authorization record.
type Authorization struct {
	// ID uniquely identifies the record. A revocation references this ID.
	ID string
	// Prefix is the authorized IP prefix in CIDR notation, e.g. 10.0.0.0/8
	// or 2001:db8::/32.
	Prefix string
	// MaxPrefixLength is the most specific prefix length an announcement may
	// have while still being covered by this record. It must be at least the
	// prefix length of Prefix and at most the address family width (32/128).
	MaxPrefixLength int
	// OriginAS is the only autonomous system permitted to originate a
	// covered announcement. ASN 0 is reserved and rejected.
	OriginAS uint32
	// NotBefore is the inclusive start of the validity window.
	NotBefore time.Time
	// NotAfter is the exclusive end of the validity window.
	NotAfter time.Time
}

// Revocation is an explicit withdrawal of a single authorization record.
type Revocation struct {
	// RecordID references Authorization.ID.
	RecordID string
	// At is the instant at which the withdrawal takes effect. A record is
	// considered revoked for every evaluation instant t with At <= t.
	At time.Time
}

// Announcement is a route origin announcement to be adjudicated.
type Announcement struct {
	// Prefix is the announced IP prefix in CIDR notation.
	Prefix string
	// OriginAS is the autonomous system originating the announcement.
	OriginAS uint32
}

// Decision is the adjudication result for one announcement at one instant.
type Decision struct {
	// State is the outcome.
	State State
	// Reason is a short human-readable explanation.
	Reason string
	// Associated holds the IDs of active covering authorization records
	// considered for the decision:
	//
	//   - for Valid, the IDs of every matching active record;
	//   - for Invalid, the IDs of every covering active record (none matched);
	//   - for NotFound, nil.
	//
	// IDs are sorted for deterministic output.
	Associated []string
}

type record struct {
	rec       Authorization
	prefix    netip.Prefix
	revokedAt time.Time
	revoked   bool
}

// Engine is a concurrency-safe, append-only store of authorizations and
// revocations that evaluates announcements against them.
type Engine struct {
	mu      sync.RWMutex
	records map[string]*record
	// order preserves insertion order so identical sets yield identical output.
	order []string
}

// NewEngine creates an empty decision engine.
func NewEngine() *Engine {
	return &Engine{records: make(map[string]*record)}
}

// Add stores one verified authorization record. It returns an error wrapping
// ErrDuplicateRecord for a repeated ID or ErrInvalidInput for a malformed or
// internally inconsistent record.
func (e *Engine) Add(a Authorization) error {
	p, err := validateAuthorization(a)
	if err != nil {
		return err
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	if _, exists := e.records[a.ID]; exists {
		return fmt.Errorf("%w: %q", ErrDuplicateRecord, a.ID)
	}
	e.records[a.ID] = &record{rec: a, prefix: p}
	e.order = append(e.order, a.ID)
	return nil
}

// Revoke applies a withdrawal event. The referenced record must exist and not
// have been revoked already; otherwise an error wrapping ErrUnknownRecord or
// ErrAlreadyRevoked is returned and the engine is unchanged.
func (e *Engine) Revoke(r Revocation) error {
	if r.RecordID == "" {
		return fmt.Errorf("%w: empty record id", ErrInvalidInput)
	}

	e.mu.Lock()
	defer e.mu.Unlock()

	rec, ok := e.records[r.RecordID]
	if !ok {
		return fmt.Errorf("%w: %q", ErrUnknownRecord, r.RecordID)
	}
	if rec.revoked {
		return fmt.Errorf("%w: %q", ErrAlreadyRevoked, r.RecordID)
	}
	rec.revoked = true
	rec.revokedAt = r.At
	return nil
}

// Evaluate adjudicates an announcement at the current instant. It is a
// convenience wrapper around EvaluateAt(time.Now()).
func (e *Engine) Evaluate(a Announcement) (Decision, error) {
	return e.EvaluateAt(a, time.Now())
}

// EvaluateAt adjudicates an announcement as of an explicit instant, which
// makes every decision reproducible: the same announcement evaluated at the
// same instant always yields the same decision.
func (e *Engine) EvaluateAt(a Announcement, at time.Time) (Decision, error) {
	ann, err := parseAnnouncement(a)
	if err != nil {
		return Decision{}, err
	}

	e.mu.RLock()
	defer e.mu.RUnlock()

	var covering, matched []string
	for _, id := range e.order {
		rec := e.records[id]
		if !rec.activeAt(at) {
			continue
		}
		if !prefixCovers(rec.prefix, ann) {
			continue
		}
		covering = append(covering, id)
		if rec.rec.OriginAS == a.OriginAS && ann.Bits() <= rec.rec.MaxPrefixLength {
			matched = append(matched, id)
		}
	}

	sort.Strings(covering)
	sort.Strings(matched)

	switch {
	case len(matched) > 0:
		return Decision{
			State:      StateValid,
			Reason:     "matched an active covering authorization",
			Associated: matched,
		}, nil
	case len(covering) > 0:
		return Decision{
			State:      StateInvalid,
			Reason:     "covered by an active authorization but origin and/or prefix length does not match",
			Associated: covering,
		}, nil
	default:
		return Decision{
			State:      StateNotFound,
			Reason:     "no active authorization covers the announcement",
			Associated: nil,
		}, nil
	}
}

// Get returns a stored authorization by ID. The boolean reports whether it
// exists. Revocation state can be inspected via Revoked and RevokedAt.
func (e *Engine) Get(id string) (Authorization, bool) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	rec, ok := e.records[id]
	if !ok {
		return Authorization{}, false
	}
	return rec.rec, true
}

// Revoked reports whether the record has received a revocation event.
func (e *Engine) Revoked(id string) bool {
	e.mu.RLock()
	defer e.mu.RUnlock()
	rec, ok := e.records[id]
	return ok && rec.revoked
}

// RevokedAt returns the revocation instant of a record. The boolean is false
// when the record is unknown or has not been revoked.
func (e *Engine) RevokedAt(id string) (time.Time, bool) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	rec, ok := e.records[id]
	if !ok || !rec.revoked {
		return time.Time{}, false
	}
	return rec.revokedAt, true
}

// activeAt reports whether the record is in force at instant t: inside its
// validity window (NotBefore inclusive, NotAfter exclusive) and not revoked
// at or before t.
func (r *record) activeAt(t time.Time) bool {
	if t.Before(r.rec.NotBefore) {
		return false
	}
	if !t.Before(r.rec.NotAfter) {
		return false
	}
	if r.revoked && !r.revokedAt.After(t) {
		return false
	}
	return true
}

// validateAuthorization parses and cross-checks a record, returning the
// parsed authorized prefix.
func validateAuthorization(a Authorization) (netip.Prefix, error) {
	if a.ID == "" {
		return netip.Prefix{}, fmt.Errorf("%w: empty record id", ErrInvalidInput)
	}
	if a.OriginAS == 0 {
		return netip.Prefix{}, fmt.Errorf("%w: record %q uses reserved ASN 0", ErrInvalidInput, a.ID)
	}
	p, err := netip.ParsePrefix(a.Prefix)
	if err != nil {
		return netip.Prefix{}, fmt.Errorf("%w: record %q prefix: %v", ErrInvalidInput, a.ID, err)
	}
	p = p.Masked()
	familyWidth := p.Addr().BitLen()
	if a.MaxPrefixLength < p.Bits() || a.MaxPrefixLength > familyWidth {
		return netip.Prefix{}, fmt.Errorf(
			"%w: record %q max prefix length %d out of range [%d,%d]",
			ErrInvalidInput, a.ID, a.MaxPrefixLength, p.Bits(), familyWidth,
		)
	}
	if a.NotAfter.Before(a.NotBefore) || a.NotAfter.Equal(a.NotBefore) {
		return netip.Prefix{}, fmt.Errorf("%w: record %q has empty validity window", ErrInvalidInput, a.ID)
	}
	return p, nil
}

// parseAnnouncement parses an announcement prefix and rejects the reserved
// origin ASN 0.
func parseAnnouncement(a Announcement) (netip.Prefix, error) {
	if a.OriginAS == 0 {
		return netip.Prefix{}, fmt.Errorf("%w: announcement uses reserved ASN 0", ErrInvalidInput)
	}
	p, err := netip.ParsePrefix(a.Prefix)
	if err != nil {
		return netip.Prefix{}, fmt.Errorf("%w: announcement prefix: %v", ErrInvalidInput, err)
	}
	return p.Masked(), nil
}

// prefixCovers reports whether the authorized prefix outer covers the whole
// announcement prefix inner: same address family, outer no more specific than
// inner, and every address of inner falls inside outer.
func prefixCovers(outer, inner netip.Prefix) bool {
	if outer.Addr().BitLen() != inner.Addr().BitLen() {
		return false
	}
	if outer.Bits() > inner.Bits() {
		return false
	}
	return outer.Contains(inner.Addr())
}
