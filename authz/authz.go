// Package authz implements consultant authorization for client materials.
//
// # Rules
//
//   - Public materials are visible to every consultant at any time.
//   - The first successful open of a confidential material in an industry
//     commits the consultant to that material's client for that industry.
//     Afterwards only that client's confidential materials are readable in
//     that industry; public materials stay readable.
//   - The commitment point is a successful body download. Catalog previews
//     and failed downloads never consume the choice.
//   - A write is checked against every confidential material the consultant
//     has successfully read. If the write target client differs from any of
//     those read sources, the write is denied.
//
// # Concurrency
//
// Confidential opens are serialized per (consultant, industry) by a
// reference-counted gate. The pre-check, the body download and the commitment
// all happen while holding that gate, so concurrent first opens of different
// clients in one industry cannot both succeed: the winner commits and returns
// the body, and the loser is denied before it downloads anything. Independent
// industries and independent consultants never block each other.
package authz

import (
	"errors"
	"fmt"
	"sync"
)

// ConsultantID identifies a consultant.
type ConsultantID string

// MaterialID identifies a material in the catalog.
type MaterialID string

// ClientID identifies a client whose materials are stored.
type ClientID string

// Material is an immutable catalog entry plus its confidentiality flag.
type Material struct {
	ID           MaterialID
	Industry     string
	Client       ClientID
	Title        string
	Confidential bool
}

// CatalogEntry is the metadata shown by Preview. It intentionally has no body
// field: previewing the catalog can never reveal confidential content.
type CatalogEntry struct {
	ID           MaterialID
	Industry     string
	Client       ClientID
	Title        string
	Confidential bool
}

// Source downloads the body of a material. A download error models a failed
// download: the open fails and no choice is consumed.
type Source interface {
	Download(MaterialID) (string, error)
}

// MapSource is a Source backed by an in-memory map. A missing material is a
// failed download.
type MapSource map[MaterialID]string

// Download implements Source.
func (m MapSource) Download(id MaterialID) (string, error) {
	body, ok := m[id]
	if !ok {
		return "", fmt.Errorf("authz: no body for material %q", id)
	}
	return body, nil
}

// Grant records one successful confidential read. ID increases monotonically
// per consultant, so a caller can name the exact read that constrained an
// operation.
type Grant struct {
	ID         uint64
	Consultant ConsultantID
	Industry   string
	Client     ClientID
	Material   MaterialID
}

// Reason classifies an Outcome.
type Reason string

const (
	// ReasonAllowed means the operation was permitted.
	ReasonAllowed Reason = "allowed"
	// ReasonInvalidConsultant means an empty consultant identity was supplied.
	ReasonInvalidConsultant Reason = "invalid_consultant"
	// ReasonInvalidClient means an empty write target was supplied.
	ReasonInvalidClient Reason = "invalid_client"
	// ReasonMaterialNotFound means the material ID is not in the catalog.
	ReasonMaterialNotFound Reason = "material_not_found"
	// ReasonDownloadFailed means the body could not be downloaded. No
	// commitment is recorded for this attempt.
	ReasonDownloadFailed Reason = "download_failed"
	// ReasonIndustryCommitted means the industry is already committed to a
	// different client. LimitedBy names the read that committed it.
	ReasonIndustryCommitted Reason = "industry_committed"
	// ReasonWriteClientConflict means the write target client differs from a
	// confidential material read earlier. LimitedBy is the earliest such
	// read and Conflicts lists all of them.
	ReasonWriteClientConflict Reason = "write_client_conflict"
)

// Outcome is the result of Preview, Open, or Write.
//
// Body is non-empty only when Allowed is true and a body was downloaded.
//
// LimitedBy is set on a denial and names the successful read that restricted
// the operation. It is nil for allowed operations.
//
// Conflicts is set for a write denial and lists every earlier successful
// confidential read whose client differs from the write target, in read order.
// Repeated reads of the same material appear once, at their earliest read.
//
// NewConstraint is non-nil if and only if this operation actually established
// a new commitment (the first successful confidential open in an industry).
// Reopening an already committed client, previewing, failed downloads, and
// every write leave it nil.
type Outcome struct {
	Allowed       bool
	Reason        Reason
	Body          string
	LimitedBy     *Grant
	Conflicts     []Grant
	NewConstraint *Grant
}

// Snapshot is an immutable copy of a consultant's committed state.
type Snapshot struct {
	// Locks maps an industry to the first read that committed it.
	Locks map[string]Grant
	// Reads lists every successful confidential read in order.
	Reads []Grant
}

// Engine enforces the authorization rules. It is safe for concurrent use.
type Engine struct {
	mu        sync.Mutex
	materials map[MaterialID]Material
	order     []MaterialID
	source    Source
	states    map[ConsultantID]*consultantState
	gates     map[gateKey]*gate
}

// gateKey is the unit of serialization for first opens: one consultant's
// decisions in one industry. Different industries and different consultants
// never block each other.
type gateKey struct {
	consultant ConsultantID
	industry   string
}

// gate is a reference-counted mutex so the map does not grow without bound.
type gate struct {
	mu   sync.Mutex
	refs int
}

func (e *Engine) acquireGate(k gateKey) *gate {
	e.mu.Lock()
	defer e.mu.Unlock()
	g := e.gates[k]
	if g == nil {
		g = &gate{}
		e.gates[k] = g
	}
	g.refs++
	return g
}

func (e *Engine) releaseGate(k gateKey, g *gate) {
	e.mu.Lock()
	defer e.mu.Unlock()
	g.refs--
	if g.refs == 0 {
		delete(e.gates, k)
	}
}

type consultantState struct {
	locks  map[string]*Grant
	reads  []*Grant
	nextID uint64
}

// New builds an Engine. Materials must have a unique non-empty ID, a non-empty
// industry and a non-empty client. source must be non-nil.
func New(materials []Material, source Source) (*Engine, error) {
	if source == nil {
		return nil, errors.New("authz: nil source")
	}
	e := &Engine{
		materials: make(map[MaterialID]Material, len(materials)),
		order:     make([]MaterialID, 0, len(materials)),
		source:    source,
		states:    make(map[ConsultantID]*consultantState),
		gates:     make(map[gateKey]*gate),
	}
	for _, m := range materials {
		if m.ID == "" {
			return nil, errors.New("authz: material with empty ID")
		}
		if _, exists := e.materials[m.ID]; exists {
			return nil, fmt.Errorf("authz: duplicate material %q", m.ID)
		}
		if m.Industry == "" {
			return nil, fmt.Errorf("authz: material %q has empty industry", m.ID)
		}
		if m.Client == "" {
			return nil, fmt.Errorf("authz: material %q has empty client", m.ID)
		}
		e.materials[m.ID] = m
		e.order = append(e.order, m.ID)
	}
	return e, nil
}

// Preview returns the catalog directory for browsing: every material's
// metadata, including confidential entries. The directory itself is what the
// consultant uses to choose, so it is visible for all clients and industries.
//
// A CatalogEntry carries no body, and Preview changes no authorization state,
// so browsing the directory can never reveal confidential content and can
// never consume the consultant's choice. Reading a confidential body requires
// Open, which applies the commitment rules.
func (e *Engine) Preview(c ConsultantID) []CatalogEntry {
	entries := make([]CatalogEntry, 0, len(e.order))
	for _, id := range e.order {
		m := e.materials[id]
		entries = append(entries, CatalogEntry{
			ID:           m.ID,
			Industry:     m.Industry,
			Client:       m.Client,
			Title:        m.Title,
			Confidential: m.Confidential,
		})
	}
	return entries
}

// Open attempts to open a material.
//
// Public materials are always allowed and never create a commitment.
//
// Confidential opens are serialized per (consultant, industry), so a racing
// first open cannot interleave with the decision that commits the industry.
// The check runs before downloading: if the industry is already committed to
// another client, Open is denied without downloading. Otherwise the body is
// downloaded; a failed download is denied and leaves the state untouched. The
// commitment is recorded only after a successful download, under the engine
// lock.
func (e *Engine) Open(c ConsultantID, id MaterialID) Outcome {
	if c == "" {
		return Outcome{Reason: ReasonInvalidConsultant}
	}
	m, ok := e.materials[id]
	if !ok {
		return Outcome{Reason: ReasonMaterialNotFound}
	}
	if !m.Confidential {
		return e.downloadPublic(id)
	}

	key := gateKey{consultant: c, industry: m.Industry}
	g := e.acquireGate(key)
	g.mu.Lock()
	defer func() {
		g.mu.Unlock()
		e.releaseGate(key, g)
	}()

	// The gate makes the check and the commitment atomic: while this open
	// holds the gate no other open for the same consultant and industry can
	// commit, so at most one of them can return a body.
	e.mu.Lock()
	st := e.state(c)
	lock := st.locks[m.Industry]
	if lock != nil && lock.Client != m.Client {
		e.mu.Unlock()
		return Outcome{Reason: ReasonIndustryCommitted, LimitedBy: copyGrant(lock)}
	}
	e.mu.Unlock()

	body, err := e.source.Download(id)
	if err != nil {
		return Outcome{Reason: ReasonDownloadFailed}
	}

	e.mu.Lock()
	defer e.mu.Unlock()
	grant := st.record(c, m)
	out := Outcome{Allowed: true, Reason: ReasonAllowed, Body: body}
	if st.locks[m.Industry] == nil {
		st.locks[m.Industry] = grant
		out.NewConstraint = copyGrant(grant)
	}
	return out
}

func (e *Engine) downloadPublic(id MaterialID) Outcome {
	body, err := e.source.Download(id)
	if err != nil {
		return Outcome{Reason: ReasonDownloadFailed}
	}
	return Outcome{Allowed: true, Reason: ReasonAllowed, Body: body}
}

// Write checks a write to target against every confidential material the
// consultant has successfully read. The target client must equal the client of
// every read material; otherwise the write is denied. Reads of the same
// material collapse to their earliest grant. Writes never create a commitment.
func (e *Engine) Write(c ConsultantID, target ClientID) Outcome {
	if c == "" {
		return Outcome{Reason: ReasonInvalidConsultant}
	}
	if target == "" {
		return Outcome{Reason: ReasonInvalidClient}
	}

	e.mu.Lock()
	defer e.mu.Unlock()
	st := e.lookup(c)
	if st == nil {
		return Outcome{Allowed: true, Reason: ReasonAllowed}
	}
	var conflicts []Grant
	seen := make(map[MaterialID]struct{}, len(st.reads))
	for _, r := range st.reads {
		if r.Client == target {
			continue
		}
		if _, dup := seen[r.Material]; dup {
			continue
		}
		seen[r.Material] = struct{}{}
		conflicts = append(conflicts, *r)
	}
	if len(conflicts) == 0 {
		return Outcome{Allowed: true, Reason: ReasonAllowed}
	}
	return Outcome{
		Reason:    ReasonWriteClientConflict,
		LimitedBy: copyGrant(&conflicts[0]),
		Conflicts: conflicts,
	}
}

// Snapshot returns a copy of a consultant's committed industries and read
// history. It has no side effects, including for an unknown consultant.
func (e *Engine) Snapshot(c ConsultantID) Snapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	snap := Snapshot{
		Locks: make(map[string]Grant),
		Reads: make([]Grant, 0),
	}
	st := e.states[c]
	if st == nil {
		return snap
	}
	for industry, g := range st.locks {
		snap.Locks[industry] = *g
	}
	for _, g := range st.reads {
		snap.Reads = append(snap.Reads, *g)
	}
	return snap
}

func (e *Engine) lookup(c ConsultantID) *consultantState {
	return e.states[c]
}

func (e *Engine) state(c ConsultantID) *consultantState {
	st := e.states[c]
	if st == nil {
		st = &consultantState{locks: make(map[string]*Grant)}
		e.states[c] = st
	}
	return st
}

func (st *consultantState) record(c ConsultantID, m Material) *Grant {
	st.nextID++
	g := &Grant{
		ID:         st.nextID,
		Consultant: c,
		Industry:   m.Industry,
		Client:     m.Client,
		Material:   m.ID,
	}
	st.reads = append(st.reads, g)
	return g
}

func copyGrant(g *Grant) *Grant {
	if g == nil {
		return nil
	}
	c := *g
	return &c
}
