// Package dictpool implements a compression-dictionary pool that separates
// public dictionary snapshots from customer-private dictionary data.
//
// The pool supports four lifecycle operations required by the specification:
//
//   - derive a writable handle from a public (or owned private) snapshot,
//   - append additional dictionary data to a handle,
//   - save the current handle state as a new immutable snapshot,
//   - return (release) a handle.
//
// Before the first private append, a derived handle shares the source
// snapshot's entry storage. The first append performs copy-on-write
// separation so that public branch state is never mutated. Every derived
// copy inherits the confidentiality scope of its source branch; a handle
// derived from a private branch stays private to that customer.
package dictpool

import (
	"errors"
	"sync"
)

// Provenance tags carried by every source label.
const (
	// PublicData marks dictionary text that originated from a public snapshot.
	PublicData = "public"
	// CustomerData marks dictionary text appended as customer-private data.
	CustomerData = "customer"
)

// Sentinel errors returned by the pool. Callers should use errors.Is.
var (
	// ErrEmptyText is returned when an empty dictionary text is supplied.
	ErrEmptyText = errors.New("dictpool: dictionary text must not be empty")
	// ErrInvalidCustomer is returned when a customer identifier is blank.
	ErrInvalidCustomer = errors.New("dictpool: customer id must not be empty")
	// ErrSnapshotNotFound is returned when a snapshot id is unknown to the pool.
	ErrSnapshotNotFound = errors.New("dictpool: snapshot not found")
	// ErrHandleNotFound is returned when a handle id is unknown to the pool.
	ErrHandleNotFound = errors.New("dictpool: handle not found")
	// ErrHandleReturned is returned when an operation targets a released handle.
	ErrHandleReturned = errors.New("dictpool: handle has been returned")
	// ErrNotAuthorized is returned when a customer reaches for another
	// customer's private branch or snapshot.
	ErrNotAuthorized = errors.New("dictpool: access to private snapshot denied")
)

// Scope is the confidentiality scope of a snapshot, handle or source label.
type Scope int

const (
	// ScopePublic data may be shared across customers.
	ScopePublic Scope = iota
	// ScopePrivate data is restricted to one owning customer.
	ScopePrivate
)

// String returns a human-readable scope name.
func (s Scope) String() string {
	switch s {
	case ScopePublic:
		return "public"
	case ScopePrivate:
		return "private"
	default:
		return "unknown"
	}
}

// SourceLabel records the provenance and confidentiality of a dictionary
// entry. Labels are the audit trail used to verify pool isolation behaviour.
type SourceLabel struct {
	// Provenance is either PublicData or CustomerData.
	Provenance string
	// Scope is the confidentiality scope of the entry.
	Scope Scope
	// Customer is the owning customer for private entries, empty for public ones.
	Customer string
	// OriginSnapshotID is the snapshot in which the text first entered the pool.
	OriginSnapshotID string
	// SnapshotID is the snapshot that supplied this concrete entry copy
	// (equal to OriginSnapshotID for public entries).
	SnapshotID string
}

// Entry is one dictionary record: the dictionary text plus its source label.
type Entry struct {
	Text  string
	Label SourceLabel
}

// Snapshot is an immutable, committed dictionary state.
type Snapshot struct {
	ID    string
	Scope Scope
	// Customer is the owner of a private snapshot, empty for public snapshots.
	Customer string
	entries  []Entry
}

// Handle is a leased, potentially writable view derived from a snapshot.
type Handle struct {
	ID       string
	Customer string
	entries  []Entry
	scope    Scope
	detached bool
	valid    bool
}

// Pool owns every snapshot and leased handle and guards their consistency.
type Pool struct {
	mu        sync.Mutex
	seq       uint64
	snapshots map[string]*Snapshot
	handles   map[string]*Handle
}

// New creates an empty dictionary pool.
func New() *Pool {
	return &Pool{
		snapshots: make(map[string]*Snapshot),
		handles:   make(map[string]*Handle),
	}
}

// nextID must be called with p.mu held.
func (p *Pool) nextID(prefix string) string {
	p.seq++
	return prefix + "-" + itoa(p.seq)
}

// CreatePublicSnapshot commits a public snapshot built from the supplied
// dictionary texts. Every entry is labelled as public data.
func (p *Pool) CreatePublicSnapshot(texts []string) (*Snapshot, error) {
	for _, t := range texts {
		if t == "" {
			return nil, ErrEmptyText
		}
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	id := p.nextID("snap")
	entries := make([]Entry, 0, len(texts))
	for _, t := range texts {
		entries = append(entries, Entry{
			Text: t,
			Label: SourceLabel{
				Provenance:       PublicData,
				Scope:            ScopePublic,
				Customer:         "",
				OriginSnapshotID: id,
				SnapshotID:       id,
			},
		})
	}

	snap := &Snapshot{
		ID:       id,
		Scope:    ScopePublic,
		Customer: "",
		entries:  entries,
	}
	p.snapshots[id] = snap
	return cloneSnapshot(snap), nil
}

// Derive leases a writable handle for customer from snapshotID. The handle
// inherits the confidentiality scope of the source branch: a private
// snapshot may only be derived by its owning customer. No entry data is
// copied until the first private append (copy-on-write).
func (p *Pool) Derive(customer, snapshotID string) (*Handle, error) {
	if customer == "" {
		return nil, ErrInvalidCustomer
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	snap, ok := p.snapshots[snapshotID]
	if !ok {
		return nil, ErrSnapshotNotFound
	}
	if !snap.visibleTo(customer) {
		return nil, ErrNotAuthorized
	}

	h := &Handle{
		ID:       p.nextID("handle"),
		Customer: customer,
		// Share backing storage; copy-on-write happens on the first append.
		entries:  snap.entries,
		scope:    snap.Scope,
		detached: false,
		valid:    true,
	}
	p.handles[h.ID] = h
	return cloneHandle(h), nil
}

// Append adds customer-private dictionary text to a handle. On the first
// append the writable state is detached from the source branch so public and
// other-private branches stay untouched. The handle scope is raised to
// private for the leasing customer.
func (p *Pool) Append(handleID, customer, text string) error {
	if customer == "" {
		return ErrInvalidCustomer
	}
	if text == "" {
		return ErrEmptyText
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	h, err := p.liveHandleLocked(handleID)
	if err != nil {
		return err
	}
	if h.Customer != customer {
		return ErrNotAuthorized
	}

	if !h.detached {
		// Copy-on-write: separate writable state from the source branch.
		copied := make([]Entry, len(h.entries), len(h.entries)+1)
		copy(copied, h.entries)
		h.entries = copied
		h.detached = true
	}

	h.entries = append(h.entries, Entry{
		Text: text,
		Label: SourceLabel{
			Provenance:       CustomerData,
			Scope:            ScopePrivate,
			Customer:         customer,
			OriginSnapshotID: "",
			SnapshotID:       "",
		},
	})
	h.scope = ScopePrivate
	return nil
}

// SaveSnapshot commits the current handle state as a new immutable snapshot
// and returns it. Private entries are stamped with the new snapshot id while
// inherited entries keep their original labels. The handle keeps working and
// remains detached from the newly committed snapshot.
func (p *Pool) SaveSnapshot(handleID string) (*Snapshot, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	h, err := p.liveHandleLocked(handleID)
	if err != nil {
		return nil, err
	}

	id := p.nextID("snap")
	entries := make([]Entry, len(h.entries))
	copy(entries, h.entries)
	for i := range entries {
		if entries[i].Label.Provenance == CustomerData {
			entries[i].Label.OriginSnapshotID = id
			entries[i].Label.SnapshotID = id
		}
	}

	snap := &Snapshot{
		ID:       id,
		Scope:    h.scope,
		Customer: "",
		entries:  entries,
	}
	if h.scope == ScopePrivate {
		snap.Customer = h.Customer
	}
	p.snapshots[id] = snap
	return cloneSnapshot(snap), nil
}

// Return releases a handle. Further operations on the handle are rejected.
// Committed snapshots are unaffected and remain usable within their scope.
func (p *Pool) Return(handleID string) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	h, ok := p.handles[handleID]
	if !ok {
		return ErrHandleNotFound
	}
	if !h.valid {
		return ErrHandleReturned
	}
	h.valid = false
	return nil
}

// Lookup searches a live handle's dictionary for an exact text match and
// returns the entry including its source label.
func (p *Pool) Lookup(handleID, text string) (Entry, bool, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	h, err := p.liveHandleLocked(handleID)
	if err != nil {
		return Entry{}, false, err
	}
	if e, ok := findEntry(h.entries, text); ok {
		return e, true, nil
	}
	return Entry{}, false, nil
}

// LookupSnapshot searches a committed snapshot for an exact text match.
// Private snapshots are only searchable by their owning customer.
func (p *Pool) LookupSnapshot(customer, snapshotID, text string) (Entry, bool, error) {
	if customer == "" {
		return Entry{}, false, ErrInvalidCustomer
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	snap, ok := p.snapshots[snapshotID]
	if !ok {
		return Entry{}, false, ErrSnapshotNotFound
	}
	if !snap.visibleTo(customer) {
		return Entry{}, false, ErrNotAuthorized
	}
	if e, ok := findEntry(snap.entries, text); ok {
		return e, true, nil
	}
	return Entry{}, false, nil
}

// HandleScope returns the current confidentiality scope of a live handle.
func (p *Pool) HandleScope(handleID string) (Scope, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	h, err := p.liveHandleLocked(handleID)
	if err != nil {
		return 0, err
	}
	return h.scope, nil
}

// Detached reports whether the handle has performed copy-on-write separation
// from its source branch. It exists for verification of isolation behaviour.
func (p *Pool) Detached(handleID string) (bool, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	h, err := p.liveHandleLocked(handleID)
	if err != nil {
		return false, err
	}
	return h.detached, nil
}

// HandleEntries returns a defensive copy of the handle's labelled entries.
func (p *Pool) HandleEntries(handleID string) ([]Entry, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	h, err := p.liveHandleLocked(handleID)
	if err != nil {
		return nil, err
	}
	return cloneEntries(h.entries), nil
}

// SnapshotEntries returns a defensive copy of a snapshot's labelled entries.
// Private snapshots are only readable by their owning customer.
func (p *Pool) SnapshotEntries(customer, snapshotID string) ([]Entry, error) {
	if customer == "" {
		return nil, ErrInvalidCustomer
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	snap, ok := p.snapshots[snapshotID]
	if !ok {
		return nil, ErrSnapshotNotFound
	}
	if !snap.visibleTo(customer) {
		return nil, ErrNotAuthorized
	}
	return cloneEntries(snap.entries), nil
}

// SnapshotScope reports the confidentiality scope of a committed snapshot.
// Scope metadata is not itself confidential.
func (p *Pool) SnapshotScope(snapshotID string) (Scope, error) {
	p.mu.Lock()
	defer p.mu.Unlock()

	snap, ok := p.snapshots[snapshotID]
	if !ok {
		return 0, ErrSnapshotNotFound
	}
	return snap.Scope, nil
}

// liveHandleLocked resolves a handle id and enforces liveness. Caller holds p.mu.
func (p *Pool) liveHandleLocked(handleID string) (*Handle, error) {
	h, ok := p.handles[handleID]
	if !ok {
		return nil, ErrHandleNotFound
	}
	if !h.valid {
		return nil, ErrHandleReturned
	}
	return h, nil
}

func (s *Snapshot) visibleTo(customer string) bool {
	return s.Scope == ScopePublic || s.Customer == customer
}

func findEntry(entries []Entry, text string) (Entry, bool) {
	for _, e := range entries {
		if e.Text == text {
			return e, true
		}
	}
	return Entry{}, false
}

func cloneEntries(in []Entry) []Entry {
	out := make([]Entry, len(in))
	copy(out, in)
	return out
}

func cloneSnapshot(s *Snapshot) *Snapshot {
	return &Snapshot{
		ID:       s.ID,
		Scope:    s.Scope,
		Customer: s.Customer,
		entries:  cloneEntries(s.entries),
	}
}

func cloneHandle(h *Handle) *Handle {
	return &Handle{
		ID:       h.ID,
		Customer: h.Customer,
		entries:  cloneEntries(h.entries),
		scope:    h.scope,
		detached: h.detached,
		valid:    h.valid,
	}
}

// itoa formats a positive integer without pulling in strconv.
func itoa(n uint64) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
