package relaygate

import (
	"errors"
	"fmt"
	"reflect"
	"sort"
	"sync"
)

// ErrConflict is wrapped by all immutable-evidence conflict errors.
var ErrConflict = errors.New("relaygate: conflicting evidence")

// ConflictError reports that an evidence ID/sequence pair was reused with
// different immutable content.
type ConflictError struct {
	Evidence string
	Session  SessionKey
	Sequence uint64
}

func (e *ConflictError) Error() string {
	return fmt.Sprintf("%s conflict for %s sequence %d", e.Evidence, e.Session, e.Sequence)
}

// Unwrap enables errors.Is(err, ErrConflict).
func (e *ConflictError) Unwrap() error {
	return ErrConflict
}

// SubmitStatus describes how a submitted evidence item was stored.
type SubmitStatus string

const (
	SubmitStored    SubmitStatus = "stored"
	SubmitDuplicate SubmitStatus = "duplicate"
	SubmitConflict  SubmitStatus = "conflict"
)

// SubmitResult contains submission status and verdicts changed by the new
// evidence.
type SubmitResult struct {
	Status  SubmitStatus `json:"status"`
	Session SessionKey   `json:"session"`

	// Sequence is the keepalive sequence for both proof and confirmation
	// submissions.
	Sequence uint64 `json:"sequence"`

	// Changed contains fresh verdicts for known records whose verdict changed.
	Changed []Verdict `json:"changed,omitempty"`
}

type options struct {
	requireWakeConfirmation bool
}

// Option configures an Adjudicator.
type Option func(*options)

// WithWakeConfirmationRequired controls whether a matching wake confirmation
// is required. The default is true.
func WithWakeConfirmationRequired(required bool) Option {
	return func(o *options) {
		o.requireWakeConfirmation = required
	}
}

type ackRef struct {
	session  SessionKey
	sequence uint64
}

type ackState struct {
	directions []Direction
	grants     []DirectionGrant
}

type recordState struct {
	record  Record
	verdict Verdict
}

// Adjudicator stores verified evidence and pending record state. It is safe
// for concurrent use.
type Adjudicator struct {
	mu sync.Mutex

	requireWake bool

	proofs map[SessionKey]map[uint64]KeepaliveProof
	acks   map[ackRef]ackState

	order  []string
	states map[string]*recordState
}

// New creates an empty adjudicator.
func New(opts ...Option) *Adjudicator {
	cfg := options{requireWakeConfirmation: true}
	for _, opt := range opts {
		opt(&cfg)
	}
	return &Adjudicator{
		requireWake: cfg.requireWakeConfirmation,
		proofs:      make(map[SessionKey]map[uint64]KeepaliveProof),
		acks:        make(map[ackRef]ackState),
		states:      make(map[string]*recordState),
	}
}

// SubmitKeepalive stores an already-verified keepalive proof.
//
// An equal proof with the same Session/Sequence is an idempotent duplicate.
// A different proof with that key returns a ConflictError and leaves the
// original proof and lease window unchanged.
func (g *Adjudicator) SubmitKeepalive(proof KeepaliveProof) (SubmitResult, error) {
	if err := proof.Validate(); err != nil {
		return SubmitResult{}, fmt.Errorf("invalid keepalive proof: %w", err)
	}

	proof = normalizeProof(cloneProof(proof))

	g.mu.Lock()
	defer g.mu.Unlock()

	bySequence, ok := g.proofs[proof.Session]
	if !ok {
		bySequence = make(map[uint64]KeepaliveProof)
		g.proofs[proof.Session] = bySequence
	}

	status := SubmitStored
	if existing, exists := bySequence[proof.Sequence]; exists {
		if !proofsEqual(existing, proof) {
			return SubmitResult{Status: SubmitConflict, Session: proof.Session, Sequence: proof.Sequence},
				&ConflictError{Evidence: "keepalive proof", Session: proof.Session, Sequence: proof.Sequence}
		}
		status = SubmitDuplicate
	} else {
		bySequence[proof.Sequence] = proof
	}

	changed := g.refreshLocked()
	return SubmitResult{Status: status, Session: proof.Session, Sequence: proof.Sequence, Changed: changed}, nil
}

// SubmitWakeConfirmation stores an already-verified wake confirmation.
//
// Confirmations may arrive before or after the referenced proof. Only exact
// direction/window grants also present in that proof can ever make a record
// deliverable. Multiple confirmations for one proof may contribute grants.
func (g *Adjudicator) SubmitWakeConfirmation(confirmation WakeConfirmation) (SubmitResult, error) {
	if err := confirmation.Validate(); err != nil {
		return SubmitResult{}, fmt.Errorf("invalid wake confirmation: %w", err)
	}

	confirmation = cloneConfirmation(confirmation)
	ref := ackRef{session: confirmation.Session, sequence: confirmation.KeepaliveSequence}

	g.mu.Lock()
	defer g.mu.Unlock()

	existing := g.acks[ref]
	mergedDirections, directionsAdded := mergeDirections(existing.directions, confirmation.Directions)
	mergedGrants, grantsAdded := mergeGrants(existing.grants, confirmation.Grants)
	added := directionsAdded || grantsAdded
	_, existed := g.acks[ref]
	g.acks[ref] = ackState{directions: mergedDirections, grants: mergedGrants}

	status := SubmitStored
	if existed && !added {
		status = SubmitDuplicate
	}

	changed := g.refreshLocked()
	return SubmitResult{
		Status:   status,
		Session:  confirmation.Session,
		Sequence: confirmation.KeepaliveSequence,
		Changed:  changed,
	}, nil
}

// Admit evaluates records against all evidence submitted so far and retains
// known record IDs for later re-evaluation. Returned verdicts have the same
// order as records.
func (g *Adjudicator) Admit(records ...Record) []Verdict {
	g.mu.Lock()
	defer g.mu.Unlock()

	results := make([]Verdict, len(records))
	for i, incoming := range records {
		record := cloneRecord(incoming)
		verdict := g.evaluateLocked(record)

		if record.ID == "" {
			results[i] = cloneVerdict(verdict)
			continue
		}

		if existing, ok := g.states[record.ID]; ok {
			if !recordsEqual(existing.record, record) {
				conflict := Verdict{
					RecordID:    record.ID,
					Status:      StatusConflict,
					Deliverable: false,
					Attribution: &Attribution{Basis: BasisNone},
					Issues: []Issue{{
						Code:        IssueDuplicateRecordID,
						Description: "record ID was already used by different record metadata",
						Session:     record.Session,
						Direction:   record.Direction,
					}},
				}
				results[i] = cloneVerdict(conflict)
				continue
			}
			existing.verdict = verdict
		} else {
			g.states[record.ID] = &recordState{record: record, verdict: verdict}
			g.order = append(g.order, record.ID)
		}

		results[i] = cloneVerdict(verdict)
	}
	return results
}

// Refresh re-evaluates every known record. Evidence submission already does
// this, so Refresh is mainly useful after externally observable state changes
// in a custom setup.
func (g *Adjudicator) Refresh() []Verdict {
	g.mu.Lock()
	defer g.mu.Unlock()
	return cloneVerdicts(g.refreshLocked())
}

// Verdict returns the latest verdict for a record ID.
func (g *Adjudicator) Verdict(recordID string) (Verdict, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	state, ok := g.states[recordID]
	if !ok {
		return Verdict{}, false
	}
	return cloneVerdict(state.verdict), true
}

// Verdicts returns all known verdicts in first-admission order.
func (g *Adjudicator) Verdicts() []Verdict {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := make([]Verdict, 0, len(g.order))
	for _, id := range g.order {
		out = append(out, cloneVerdict(g.states[id].verdict))
	}
	return out
}

// Pending returns known records that currently require more or matching
// evidence, in first-admission order.
func (g *Adjudicator) Pending() []Verdict {
	g.mu.Lock()
	defer g.mu.Unlock()
	var out []Verdict
	for _, id := range g.order {
		v := g.states[id].verdict
		if v.Status == StatusPending {
			out = append(out, cloneVerdict(v))
		}
	}
	return out
}

// Admitted returns known records that are currently deliverable, in
// first-admission order.
func (g *Adjudicator) Admitted() []Verdict {
	g.mu.Lock()
	defer g.mu.Unlock()
	var out []Verdict
	for _, id := range g.order {
		v := g.states[id].verdict
		if v.Status == StatusAdmitted {
			out = append(out, cloneVerdict(v))
		}
	}
	return out
}

func (g *Adjudicator) refreshLocked() []Verdict {
	changed := make([]Verdict, 0)
	for _, id := range g.order {
		state := g.states[id]
		next := g.evaluateLocked(state.record)
		if !reflect.DeepEqual(state.verdict, next) {
			changed = append(changed, cloneVerdict(next))
		}
		state.verdict = next
	}
	return changed
}

func recordsEqual(a, b Record) bool {
	if a.ID != b.ID || a.Session != b.Session || a.Direction != b.Direction || !a.TrustedAt.Equal(b.TrustedAt) {
		return false
	}
	if a.KeepaliveSequence == nil || b.KeepaliveSequence == nil {
		return a.KeepaliveSequence == b.KeepaliveSequence
	}
	return *a.KeepaliveSequence == *b.KeepaliveSequence
}

func cloneRecord(in Record) Record {
	out := in
	if in.KeepaliveSequence != nil {
		sequence := *in.KeepaliveSequence
		out.KeepaliveSequence = &sequence
	}
	return out
}

func cloneProof(in KeepaliveProof) KeepaliveProof {
	out := in
	if in.Grants != nil {
		out.Grants = append([]DirectionGrant(nil), in.Grants...)
	}
	return out
}

func cloneConfirmation(in WakeConfirmation) WakeConfirmation {
	out := in
	if in.Directions != nil {
		out.Directions = append([]Direction(nil), in.Directions...)
	}
	if in.Grants != nil {
		out.Grants = append([]DirectionGrant(nil), in.Grants...)
	}
	return out
}

func cloneVerdicts(in []Verdict) []Verdict {
	out := make([]Verdict, len(in))
	for i := range in {
		out[i] = cloneVerdict(in[i])
	}
	return out
}

func cloneVerdict(in Verdict) Verdict {
	out := in
	if in.Attribution != nil {
		attribution := *in.Attribution
		if in.Attribution.KeepaliveSequence != nil {
			sequence := *in.Attribution.KeepaliveSequence
			attribution.KeepaliveSequence = &sequence
		}
		if in.Attribution.Window != nil {
			window := *in.Attribution.Window
			attribution.Window = &window
		}
		out.Attribution = &attribution
	}
	if in.MissingEvidence != nil {
		out.MissingEvidence = make([]MissingEvidence, len(in.MissingEvidence))
		for i, need := range in.MissingEvidence {
			needCopy := need
			if need.KeepaliveSequence != nil {
				sequence := *need.KeepaliveSequence
				needCopy.KeepaliveSequence = &sequence
			}
			if need.Window != nil {
				window := *need.Window
				needCopy.Window = &window
			}
			out.MissingEvidence[i] = needCopy
		}
	}
	if in.Issues != nil {
		out.Issues = make([]Issue, len(in.Issues))
		for i, issue := range in.Issues {
			issueCopy := issue
			if issue.RelatedSession != nil {
				session := *issue.RelatedSession
				issueCopy.RelatedSession = &session
			}
			if issue.Sequence != nil {
				sequence := *issue.Sequence
				issueCopy.Sequence = &sequence
			}
			if issue.Window != nil {
				window := *issue.Window
				issueCopy.Window = &window
			}
			out.Issues[i] = issueCopy
		}
	}
	return out
}

func normalizeProof(proof KeepaliveProof) KeepaliveProof {
	proof.Grants = normalizeGrants(proof.Grants)
	return proof
}

func normalizeGrants(grants []DirectionGrant) []DirectionGrant {
	if len(grants) == 0 {
		return nil
	}
	out := append([]DirectionGrant(nil), grants...)
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].Direction != out[j].Direction {
			return out[i].Direction < out[j].Direction
		}
		if !out[i].Window.NotBefore.Equal(out[j].Window.NotBefore) {
			return out[i].Window.NotBefore.Before(out[j].Window.NotBefore)
		}
		return out[i].Window.ExpiresAt.Before(out[j].Window.ExpiresAt)
	})

	deduped := out[:0]
	for _, grant := range out {
		if len(deduped) == 0 || !grantEqual(deduped[len(deduped)-1], grant) {
			deduped = append(deduped, grant)
		}
	}
	return deduped
}

func proofsEqual(a, b KeepaliveProof) bool {
	if a.Session != b.Session || a.Sequence != b.Sequence {
		return false
	}
	return sameGrantSet(a.Grants, b.Grants)
}

func sameGrantSet(a, b []DirectionGrant) bool {
	if len(a) != len(b) {
		return false
	}
	used := make([]bool, len(b))
	for _, left := range a {
		found := false
		for j, right := range b {
			if !used[j] && grantEqual(left, right) {
				used[j] = true
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}

func mergeDirections(existing, incoming []Direction) ([]Direction, bool) {
	out := append([]Direction(nil), existing...)
	added := false
	for _, direction := range incoming {
		if !directionListContains(out, direction) {
			out = append(out, direction)
			added = true
		}
	}
	return out, added
}

func directionListContains(directions []Direction, wanted Direction) bool {
	for _, direction := range directions {
		if direction == wanted {
			return true
		}
	}
	return false
}

func mergeGrants(existing, incoming []DirectionGrant) ([]DirectionGrant, bool) {
	out := append([]DirectionGrant(nil), existing...)
	added := false
	for _, grant := range incoming {
		if !grantListContains(out, grant) {
			out = append(out, grant)
			added = true
		}
	}
	return out, added
}

func grantListContains(grants []DirectionGrant, wanted DirectionGrant) bool {
	for _, grant := range grants {
		if grantEqual(grant, wanted) {
			return true
		}
	}
	return false
}

func grantEqual(a, b DirectionGrant) bool {
	return a.Direction == b.Direction &&
		a.Window.NotBefore.Equal(b.Window.NotBefore) &&
		a.Window.ExpiresAt.Equal(b.Window.ExpiresAt)
}

func grantsForDirection(proof KeepaliveProof, direction Direction) []DirectionGrant {
	var out []DirectionGrant
	for _, grant := range proof.Grants {
		if grant.Direction == direction {
			out = append(out, grant)
		}
	}
	return out
}

func sortedSequences(proofs map[uint64]KeepaliveProof) []uint64 {
	out := make([]uint64, 0, len(proofs))
	for sequence := range proofs {
		out = append(out, sequence)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}
