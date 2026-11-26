// Package relaygate decides whether already-verified relay records may be
// delivered downstream.
//
// The package does not verify signatures, certificates, peer identity, or
// trusted time. Callers must supply only evidence that has already passed
// those checks and must set Record.TrustedAt from a trusted ingress clock.
package relaygate

import (
	"errors"
	"fmt"
	"time"
)

// Direction is one independently admitted relay direction.
type Direction string

const (
	// Uplink is the direction from the protected peer toward the relay.
	Uplink Direction = "uplink"
	// Downlink is the direction from the relay toward the protected peer.
	Downlink Direction = "downlink"
)

// Valid reports whether d is one of the supported directions.
func (d Direction) Valid() bool {
	return d == Uplink || d == Downlink
}

// SessionKey is the immutable session coordinate formed by the connection
// generation and the peer's boot/start number.
type SessionKey struct {
	ConnectionGeneration uint64 `json:"connection_generation"`
	PeerStartNumber      uint64 `json:"peer_start_number"`
}

// String returns a stable, human-readable owner key.
func (s SessionKey) String() string {
	return fmt.Sprintf("session(gen=%d,peer_start=%d)", s.ConnectionGeneration, s.PeerStartNumber)
}

// LeaseWindow is a trusted half-open interval [NotBefore, ExpiresAt).
// ExpiresAt is exclusive: a record whose TrustedAt equals ExpiresAt is not
// inside the lease.
type LeaseWindow struct {
	NotBefore time.Time `json:"not_before"`
	ExpiresAt time.Time `json:"expires_at"`
}

// Contains reports whether t is in the half-open window.
func (w LeaseWindow) Contains(t time.Time) bool {
	if t.IsZero() {
		return false
	}
	return !t.Before(w.NotBefore) && t.Before(w.ExpiresAt)
}

// Validate checks that both timestamps exist and that the interval is
// non-empty.
func (w LeaseWindow) Validate() error {
	if w.NotBefore.IsZero() || w.ExpiresAt.IsZero() {
		return errors.New("lease window requires non-zero NotBefore and ExpiresAt")
	}
	if !w.NotBefore.Before(w.ExpiresAt) {
		return errors.New("lease window NotBefore must be before ExpiresAt")
	}
	return nil
}

// DirectionGrant is a direction and the exact lease window explicitly listed
// for that direction in a keepalive proof.
type DirectionGrant struct {
	Direction Direction   `json:"direction"`
	Window    LeaseWindow `json:"window"`
}

// KeepaliveProof is an already-verified keepalive attestation.
//
// Sequence identifies the proof within Session. Proofs with the same Session
// and Sequence are immutable: a duplicate cannot change or extend the lease.
type KeepaliveProof struct {
	Session  SessionKey       `json:"session"`
	Sequence uint64           `json:"sequence"`
	Grants   []DirectionGrant `json:"grants"`
}

// Validate checks the structural form of a proof. An empty grant list is
// accepted because it explicitly authorizes no direction.
func (p KeepaliveProof) Validate() error {
	var errs []error
	for i, grant := range p.Grants {
		if !grant.Direction.Valid() {
			errs = append(errs, fmt.Errorf("grants[%d]: invalid direction %q", i, grant.Direction))
		}
		if err := grant.Window.Validate(); err != nil {
			errs = append(errs, fmt.Errorf("grants[%d]: %w", i, err))
		}
	}
	return errors.Join(errs...)
}

// WakeConfirmation is a delayed wake/takeover acknowledgement.
//
// Directions selects directions named by the referenced proof; the exact
// windows always come from that proof. Grants can additionally name exact
// direction/window pairs. A selected scope is effective only when the same
// direction and window are explicitly present in the referenced keepalive
// proof. Empty Directions and Grants confirm nothing; they never mean “all”.
type WakeConfirmation struct {
	Session           SessionKey       `json:"session"`
	KeepaliveSequence uint64           `json:"keepalive_sequence"`
	Directions        []Direction      `json:"directions,omitempty"`
	Grants            []DirectionGrant `json:"grants,omitempty"`
}

// Validate checks confirmation scopes without requiring the referenced proof
// to be present yet.
func (c WakeConfirmation) Validate() error {
	var errs []error
	for i, direction := range c.Directions {
		if !direction.Valid() {
			errs = append(errs, fmt.Errorf("directions[%d]: invalid direction %q", i, direction))
		}
	}
	for i, grant := range c.Grants {
		if !grant.Direction.Valid() {
			errs = append(errs, fmt.Errorf("grants[%d]: invalid direction %q", i, grant.Direction))
		}
		if err := grant.Window.Validate(); err != nil {
			errs = append(errs, fmt.Errorf("grants[%d]: %w", i, err))
		}
	}
	return errors.Join(errs...)
}

// ConfirmDirections builds a confirmation containing the exact proof grants
// for the named directions. It is the safe way to acknowledge a normal proof.
// Unknown directions produce no grants rather than broadening scope.
func ConfirmDirections(proof KeepaliveProof, directions ...Direction) WakeConfirmation {
	wanted := make(map[Direction]struct{}, len(directions))
	for _, direction := range directions {
		wanted[direction] = struct{}{}
	}

	confirmation := WakeConfirmation{
		Session:           proof.Session,
		KeepaliveSequence: proof.Sequence,
	}
	for _, grant := range proof.Grants {
		if _, ok := wanted[grant.Direction]; ok {
			confirmation.Grants = append(confirmation.Grants, grant)
		}
	}
	return confirmation
}

// Record is the verified ingress metadata for one relay record.
type Record struct {
	// ID is an optional correlation key. When non-empty, the latest verdict is
	// retained and refreshed after later evidence submissions. Empty-ID
	// records are still evaluated and returned by Admit, but are not retained.
	ID string `json:"id,omitempty"`

	// Session is the verified connection generation and peer start number.
	Session SessionKey `json:"session"`

	// Direction is the relay direction the record arrived on.
	Direction Direction `json:"direction"`

	// TrustedAt is a trusted ingress timestamp. It must not be copied from an
	// untrusted record payload.
	TrustedAt time.Time `json:"trusted_at"`

	// KeepaliveSequence optionally binds the record to one exact proof
	// sequence. When nil, the adjudicator may select another eligible proof.
	KeepaliveSequence *uint64 `json:"keepalive_sequence,omitempty"`
}

// SequencePtr is a small helper for setting Record.KeepaliveSequence.
func SequencePtr(sequence uint64) *uint64 {
	return &sequence
}

// Status is the current admission state of one record.
type Status string

const (
	// StatusAdmitted means the record can be handed downstream now.
	StatusAdmitted Status = "admitted"

	// StatusPending means no hard failure is known, but required evidence is
	// absent or not yet in scope. Later verified evidence may change the
	// verdict through Adjudicator.Verdict or submit results.
	StatusPending Status = "pending"

	// StatusDenied means known evidence violates a concrete rule, such as an
	// invalid record or a lease window that does not contain TrustedAt.
	StatusDenied Status = "denied"

	// StatusConflict means the request conflicts with immutable stored state,
	// such as two different records reusing one ID.
	StatusConflict Status = "conflict"
)

// AttributionBasis describes how the owner field was established.
type AttributionBasis string

const (
	BasisConfirmedLease   AttributionBasis = "confirmed_lease"
	BasisUnconfirmedLease AttributionBasis = "unconfirmed_lease"
	BasisCandidateLease   AttributionBasis = "candidate_lease"
	BasisSessionClaim     AttributionBasis = "session_claim"
	BasisNone             AttributionBasis = "none"
)

// Attribution is the proposed or established owner of a record.
type Attribution struct {
	Session           SessionKey       `json:"session"`
	OwnerKey          string           `json:"owner_key"`
	Direction         Direction        `json:"direction"`
	KeepaliveSequence *uint64          `json:"keepalive_sequence,omitempty"`
	Window            *LeaseWindow     `json:"window,omitempty"`
	Basis             AttributionBasis `json:"basis"`
}

// NeedCode identifies a missing or non-matching piece of evidence.
type NeedCode string

const (
	NeedTrustedTime       NeedCode = "missing_trusted_time"
	NeedKeepaliveProof    NeedCode = "missing_keepalive_proof"
	NeedDirectionGrant    NeedCode = "missing_direction_grant"
	NeedWakeConfirmation  NeedCode = "missing_wake_confirmation"
	NeedConfirmationScope NeedCode = "confirmation_does_not_cover_exact_direction_and_window"
	NeedValidWindowAtTime NeedCode = "no_lease_window_covers_trusted_time"
)

// MissingEvidence describes evidence required to admit a record.
type MissingEvidence struct {
	Code              NeedCode     `json:"code"`
	Description       string       `json:"description"`
	Session           SessionKey   `json:"session"`
	KeepaliveSequence *uint64      `json:"keepalive_sequence,omitempty"`
	Direction         Direction    `json:"direction,omitempty"`
	Window            *LeaseWindow `json:"window,omitempty"`
}

// IssueCode identifies a concrete validation or scope violation.
type IssueCode string

const (
	IssueUnknownDirection   IssueCode = "unknown_direction"
	IssueMissingTrustedTime IssueCode = "missing_trusted_time"
	IssueLeaseNotStarted    IssueCode = "lease_not_started"
	IssueLeaseExpired       IssueCode = "lease_expired"
	IssueGenerationMismatch IssueCode = "connection_generation_mismatch"
	IssuePeerStartMismatch  IssueCode = "peer_start_number_mismatch"
	IssueDuplicateRecordID  IssueCode = "duplicate_record_id_conflict"
)

// Issue describes a hard or diagnostic rule violation.
type Issue struct {
	Code           IssueCode    `json:"code"`
	Description    string       `json:"description"`
	Session        SessionKey   `json:"session"`
	RelatedSession *SessionKey  `json:"related_session,omitempty"`
	Sequence       *uint64      `json:"sequence,omitempty"`
	Direction      Direction    `json:"direction,omitempty"`
	Window         *LeaseWindow `json:"window,omitempty"`
}

// Verdict is the decision and audit explanation for one record.
type Verdict struct {
	RecordID        string            `json:"record_id"`
	Status          Status            `json:"status"`
	Deliverable     bool              `json:"deliverable"`
	Attribution     *Attribution      `json:"attribution,omitempty"`
	MissingEvidence []MissingEvidence `json:"missing_evidence,omitempty"`
	Issues          []Issue           `json:"issues,omitempty"`
}
