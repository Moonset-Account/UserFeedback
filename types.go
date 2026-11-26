// Package attribution implements an identity session attribution engine.
//
// It decides which established sessions must be disconnected after a
// directory deactivation notice is received. Sessions on a shared terminal
// keep one work slot per operator, so deactivating one natural person only
// suspends the slots owned by that person while other operators' slots may
// remain in use. Recycled account names are disambiguated by the stable
// natural-person identifier rather than the account name.
package attribution

import (
	"errors"
	"time"
)

// Sentinel errors returned by the engine. Callers should compare with errors.Is.
var (
	// ErrInvalidEvent is returned when a deactivation event is nil, has no
	// stable person identifier, or does not carry a timestamp.
	ErrInvalidEvent = errors.New("attribution: invalid deactivation event")
	// ErrUnknownSession is returned when an operation references a session
	// that was never opened or has already been removed.
	ErrUnknownSession = errors.New("attribution: unknown session")
	// ErrEmptyPrincipal is returned when a session is opened without an
	// authenticated login principal.
	ErrEmptyPrincipal = errors.New("attribution: empty login principal")
	// ErrInconsistentSwitch is returned when a switch record does not belong
	// to the target session.
	ErrInconsistentSwitch = errors.New("attribution: switch record belongs to another session")
)

// PersonID is the stable identifier of a natural person. It survives account
// name recycling: a reused account name belonging to a new employee has a
// different PersonID.
type PersonID string

// AccountName is a directory account name, which may be recycled and
// reassigned to another natural person over time.
type AccountName string

// SessionID identifies an established terminal session.
type SessionID string

// EventID identifies a directory deactivation notice.
type EventID string

// SlotID identifies a per-operator work slot inside a shared session.
type SlotID string

// SlotState describes whether a work slot can still be used.
type SlotState int

const (
	// SlotActive means the slot can still be resumed and used.
	SlotActive SlotState = iota
	// SlotSuspended means the slot's owner was deactivated and the slot can
	// no longer be continued.
	SlotSuspended
)

// String returns a stable textual representation of the slot state.
func (s SlotState) String() string {
	switch s {
	case SlotActive:
		return "active"
	case SlotSuspended:
		return "suspended"
	default:
		return "unknown"
	}
}

// SessionState describes whether a session can continue.
type SessionState int

const (
	// SessionAlive means at least one work slot in the session is active.
	SessionAlive SessionState = iota
	// SessionTerminated means every work slot is suspended and the session
	// must be disconnected. A terminated session can never be reopened.
	SessionTerminated
)

// String returns a stable textual representation of the session state.
func (s SessionState) String() string {
	switch s {
	case SessionAlive:
		return "alive"
	case SessionTerminated:
		return "terminated"
	default:
		return "unknown"
	}
}

// Principal is the authenticated login principal bound to a session at
// issuance time. PersonID is the stable natural-person anchor; AccountName
// alone must never be used for deactivation matching because names recycle.
type Principal struct {
	PersonID    PersonID
	AccountName AccountName
	IssuedAt    time.Time
}

// WorkSlot is one operator's work slot inside a session. A shared terminal
// accumulates one slot per operator via switch operations.
type WorkSlot struct {
	ID       SlotID
	PersonID PersonID
	// AccountName is informational only: matching always uses PersonID.
	AccountName AccountName
	State       SlotState
	StartedAt   time.Time
	// Evidence records the deactivation notice that suspended the slot.
	// It is nil while the slot is active.
	Evidence *DisconnectEvidence
}

// SwitchRecord is an audited operator switch performed on a shared terminal.
type SwitchRecord struct {
	SessionID  SessionID
	FromPerson PersonID
	ToPerson   PersonID
	ToAccount  AccountName
	At         time.Time
}

// Session is an established terminal session with its per-operator work slots.
type Session struct {
	ID        SessionID
	Principal Principal
	// Slots holds every work slot, in creation order. Slots[0] is the slot
	// created for the original login principal at issuance.
	Slots []*WorkSlot
	// CurrentSlotID points at the slot currently in the foreground. It is
	// empty once the session has been terminated.
	CurrentSlotID SlotID
	State         SessionState
	// SwitchLog retains every operator switch recorded against the session.
	SwitchLog []SwitchRecord
}

// CurrentSlot returns the foreground work slot, or nil if the session is
// terminated and has no foreground slot.
func (s *Session) CurrentSlot() *WorkSlot {
	if s.State == SessionTerminated || s.CurrentSlotID == "" {
		return nil
	}
	for _, slot := range s.Slots {
		if slot.ID == s.CurrentSlotID {
			return slot
		}
	}
	return nil
}

// ActiveSlots returns the slots that can still be continued, in creation order.
func (s *Session) ActiveSlots() []*WorkSlot {
	var out []*WorkSlot
	for _, slot := range s.Slots {
		if slot.State == SlotActive {
			out = append(out, slot)
		}
	}
	return out
}

// SuspendedSlots returns the slots that were suspended by deactivation
// notices, in creation order.
func (s *Session) SuspendedSlots() []*WorkSlot {
	var out []*WorkSlot
	for _, slot := range s.Slots {
		if slot.State == SlotSuspended {
			out = append(out, slot)
		}
	}
	return out
}

// DeactivationEvent is a directory notice announcing that a natural person's
// directory access has been stopped.
type DeactivationEvent struct {
	ID       EventID
	PersonID PersonID
	At       time.Time
	// NoticeAt is when the directory emitted/we received the notice. When
	// zero, At is used. Out-of-order delivery is modeled when NoticeAt is
	// later than At.
	NoticeAt time.Time
	// CorrectionOf, when non-empty, marks this notice as a correction of an
	// earlier event with that ID. Corrections update the visible disconnect
	// evidence but never reopen an already disconnected session or slot.
	CorrectionOf EventID
}

// Correction records how an original deactivation notice was corrected.
type Correction struct {
	OriginalEventID EventID
	CorrectedBy     EventID
	At              time.Time
}

// DisconnectEvidence is the visible, auditable proof attached to a suspended
// slot (and to the session that was terminated because of it).
type DisconnectEvidence struct {
	EventID        EventID
	PersonID       PersonID
	DeactivatedAt  time.Time
	FirstNoticedAt time.Time
	LastNoticedAt  time.Time
	// ObservationCount counts the notices applied to this evidence chain,
	// including the original and every duplicate/correction.
	ObservationCount int
	Corrections      []Correction
	// Retroactive is true when the notice was first applied after a session
	// or slot already existed (normal ordering), but records that the
	// deactivation timestamp predates the current session/slot start. It
	// makes backdated application visible in the evidence.
	Retroactive bool
}
