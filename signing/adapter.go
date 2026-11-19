// Package signing provides a slot-aware adapter in front of a signing device
// that exposes a small, fixed number of physical slots (the target device has
// two).
//
// A Session owns one slot from Begin until it is finalized with Sign, or until
// Abort/Close has successfully cleaned the slot. A device reset invalidates
// every authorization created before it; late cleanup notifications from such
// sessions are acknowledged but never touch the slot's current occupant.
package signing

import (
	"errors"
	"fmt"
	"sync"
)

// KeyID identifies a signing key. Only keys passed to New may be used.
type KeyID string

// AuthID is an opaque device-side authorization handle created by Device.Begin.
// The adapter always addresses a device occupancy through its AuthID, never
// through a bare slot number, so a stale handle cannot act on a new occupant.
type AuthID uint64

// SessionID identifies one document signing attempt.
type SessionID uint64

// Errors returned by the adapter. Errors coming from the device are wrapped
// with %w and remain visible through errors.Is.
var (
	ErrKeyNotAllowed  = errors.New("signing: key is not permitted")
	ErrNoFreeSlot     = errors.New("signing: no free signing slot")
	ErrSessionExpired = errors.New("signing: session expired by device reset")
	ErrSessionEnded   = errors.New("signing: session has already ended")
	ErrCleanupPending = errors.New("signing: session cleanup is pending")
	ErrNoFragments    = errors.New("signing: document has no fragments")
	ErrEmptyDocument  = errors.New("signing: document id is empty")
)

// Device is the injectable signing hardware abstraction.
//
// The adapter serializes every call with its own mutex, so implementations do
// not need to be safe for concurrent use by a single Adapter.
type Device interface {
	// Slots reports the number of physical signing slots.
	Slots() int

	// Epoch reports the current device epoch. Every successful Reset starts a
	// new epoch.
	Epoch() uint64

	// Reset clears the device, invalidates every outstanding authorization and
	// returns the new epoch. Signatures already finalized are unaffected.
	Reset() (uint64, error)

	// Begin authorizes key on slot for the given epoch. It fails when the slot
	// is already authorized or the epoch is stale.
	Begin(slot int, key KeyID, epoch uint64) (AuthID, error)

	// Append records the next fragment of an authorization. seq starts at zero
	// and must increase by exactly one per call; append order is signing order.
	Append(auth AuthID, seq int, fragment []byte) error

	// Sign finalizes the document, returns its signature bytes, and releases
	// the slot. The authorization must not be used afterwards.
	Sign(auth AuthID) ([]byte, error)

	// Release cleans a slot after cancellation or failure. The slot may be
	// handed to another document only after Release returns nil.
	Release(auth AuthID) error
}

// Adapter serializes access to a Device. It is safe for concurrent use.
type Adapter struct {
	mu      sync.Mutex
	dev     Device
	allowed map[KeyID]struct{}
	epoch   uint64
	slots   []*occupancy
	nextID  SessionID
}

type occupancy struct {
	session *Session
	token   SessionID
}

// New builds an adapter over device. Only the supplied key IDs may be used by
// sessions; an empty list permits no key at all.
func New(device Device, allowedKeys ...KeyID) *Adapter {
	if device == nil {
		panic("signing: New called with a nil device")
	}
	slotCount := device.Slots()
	if slotCount < 0 {
		slotCount = 0
	}
	allowed := make(map[KeyID]struct{}, len(allowedKeys))
	for _, key := range allowedKeys {
		allowed[key] = struct{}{}
	}
	return &Adapter{
		dev:     device,
		allowed: allowed,
		epoch:   device.Epoch(),
		slots:   make([]*occupancy, slotCount),
	}
}

// FreeSlots reports how many slots are currently available.
func (a *Adapter) FreeSlots() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.syncEpochLocked()
	free := 0
	for _, occ := range a.slots {
		if occ == nil {
			free++
		}
	}
	return free
}

// Epoch reports the device epoch the adapter is currently operating in.
func (a *Adapter) Epoch() uint64 {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.syncEpochLocked()
	return a.epoch
}

// Begin reserves a free slot for one document and authorizes key on the
// device. The slot stays exclusively bound to the returned session until Sign
// succeeds or a cancel/failure has been cleaned up successfully.
//
// The key allow-list is checked before a slot is taken, so a rejected request
// never consumes capacity.
func (a *Adapter) Begin(document string, key KeyID) (*Session, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.syncEpochLocked()

	if document == "" {
		return nil, ErrEmptyDocument
	}
	if _, ok := a.allowed[key]; !ok {
		return nil, fmt.Errorf("%w: %q", ErrKeyNotAllowed, key)
	}

	slot := -1
	for i, occ := range a.slots {
		if occ == nil {
			slot = i
			break
		}
	}
	if slot < 0 {
		return nil, ErrNoFreeSlot
	}

	auth, err := a.dev.Begin(slot, key, a.epoch)
	if err != nil {
		return nil, fmt.Errorf("signing: reserve slot %d: %w", slot, err)
	}

	a.nextID++
	session := &Session{
		adapter:  a,
		id:       a.nextID,
		document: document,
		key:      key,
		slot:     slot,
		token:    a.nextID,
		auth:     auth,
		epoch:    a.epoch,
		state:    stateActive,
	}
	a.slots[slot] = &occupancy{session: session, token: session.token}
	return session, nil
}

// Reset resets the device and expires every in-progress authorization. Slots
// are free immediately; each affected session thereafter rejects Submit and
// Sign with ErrSessionExpired, and its Close is a no-op.
func (a *Adapter) Reset() error {
	a.mu.Lock()
	defer a.mu.Unlock()

	epoch, err := a.dev.Reset()
	if err != nil {
		return fmt.Errorf("signing: reset device: %w", err)
	}
	a.epoch = epoch
	for i, occ := range a.slots {
		if occ == nil {
			continue
		}
		occ.session.state = stateExpired
		occ.session.cause = ErrSessionExpired
		a.slots[i] = nil
	}
	return nil
}

type sessionState uint8

const (
	stateActive sessionState = iota
	stateCleanup
	stateDone
	stateExpired
)

// Session is one document's signing attempt on one exclusively held slot.
type Session struct {
	adapter *Adapter

	// Immutable after Begin.
	id       SessionID
	document string
	key      KeyID
	slot     int
	token    SessionID
	auth     AuthID
	epoch    uint64

	// Guarded by adapter.mu.
	nextSeq       int
	fragmentCount int
	state         sessionState
	cause         error
}

// ID returns the session identifier.
func (s *Session) ID() SessionID { return s.id }

// Document returns the document identifier supplied to Begin.
func (s *Session) Document() string { return s.document }

// Key returns the key authorized for this session.
func (s *Session) Key() KeyID { return s.key }

// Slot returns the physical slot held by this session.
func (s *Session) Slot() int { return s.slot }

// Auth returns the device-side authorization handle backing this session. It
// is exposed for correlation with device logs; it is not a capability the
// caller is expected to hand back to the device.
func (s *Session) Auth() AuthID { return s.auth }

// Fragments reports how many fragments have been accepted so far.
func (s *Session) Fragments() int {
	s.adapter.mu.Lock()
	defer s.adapter.mu.Unlock()
	return s.fragmentCount
}

// Cause reports the cancellation/failure reason recorded for the session, or
// ErrSessionExpired after a device reset. It is nil while the session is
// active and after a clean abort without a cause.
func (s *Session) Cause() error {
	s.adapter.mu.Lock()
	defer s.adapter.mu.Unlock()
	return s.cause
}

// Submit appends the next document fragment in call order. Fragments of one
// session are always numbered 0, 1, 2, ... in that order on the device.
//
// If the device rejects the append, the session becomes dirty and the slot
// stays quarantined until Close succeeds.
func (s *Session) Submit(fragment []byte) error {
	a := s.adapter
	a.mu.Lock()
	defer a.mu.Unlock()

	if err := s.writableLocked(); err != nil {
		return err
	}
	if err := a.dev.Append(s.auth, s.nextSeq, fragment); err != nil {
		s.state = stateCleanup
		s.cause = err
		return fmt.Errorf("signing: append fragment %d on slot %d: %w", s.nextSeq, s.slot, err)
	}
	s.nextSeq++
	s.fragmentCount++
	return nil
}

// Sign finalizes the document on the device and releases the slot. At least
// one fragment must have been accepted. If the device fails to sign, the
// session becomes dirty and the slot stays quarantined until Close succeeds.
func (s *Session) Sign() ([]byte, error) {
	a := s.adapter
	a.mu.Lock()
	defer a.mu.Unlock()

	if err := s.writableLocked(); err != nil {
		return nil, err
	}
	if s.fragmentCount == 0 {
		return nil, ErrNoFragments
	}
	signature, err := a.dev.Sign(s.auth)
	if err != nil {
		s.state = stateCleanup
		s.cause = err
		return nil, fmt.Errorf("signing: finalize slot %d: %w", s.slot, err)
	}
	s.state = stateDone
	a.releaseSlotLocked(s)
	return append([]byte(nil), signature...), nil
}

// Abort cancels a session or retries the cleanup of a failed one. The slot is
// returned to the pool only when the device confirms cleanup. A non-nil cause
// is recorded for observability.
//
// Abort is idempotent: calling it again after a clean end or after the session
// was expired by a device reset is a no-op. In particular, a late close
// notification for an old session can never free the slot now owned by a
// different session.
func (s *Session) Abort(cause error) error {
	a := s.adapter
	a.mu.Lock()
	defer a.mu.Unlock()
	return s.abortLocked(cause)
}

// Close is Abort(nil), for use as a plain cancellation / io.Closer.
func (s *Session) Close() error {
	return s.Abort(nil)
}

func (s *Session) abortLocked(cause error) error {
	s.adapter.syncEpochLocked()
	switch s.state {
	case stateDone, stateExpired:
		// Idempotent late notification: never touch whatever occupies the slot
		// now.
		return nil
	}

	if cause != nil {
		s.cause = cause
	}
	s.state = stateCleanup

	if !s.adapter.ownsSlotLocked(s) {
		// Defensive: the slot was rebound or expired elsewhere. There is
		// nothing of ours left to clean.
		s.state = stateExpired
		return nil
	}

	if err := s.adapter.dev.Release(s.auth); err != nil {
		return fmt.Errorf("signing: cleanup slot %d: %w", s.slot, err)
	}
	s.state = stateDone
	s.adapter.releaseSlotLocked(s)
	return nil
}

func (s *Session) writableLocked() error {
	s.adapter.syncEpochLocked()
	switch s.state {
	case stateActive:
		if s.epoch != s.adapter.epoch {
			return ErrSessionExpired
		}
		return nil
	case stateCleanup:
		return ErrCleanupPending
	case stateExpired:
		return ErrSessionExpired
	default:
		return ErrSessionEnded
	}
}

func (a *Adapter) ownsSlotLocked(s *Session) bool {
	if s.slot < 0 || s.slot >= len(a.slots) {
		return false
	}
	occ := a.slots[s.slot]
	return occ != nil && occ.token == s.token && s.epoch == a.epoch
}

// syncEpochLocked detects a device reset that bypassed Adapter.Reset (for
// example one triggered from the device's own panel) and expires everything
// that was authorized under the old epoch.
func (a *Adapter) syncEpochLocked() {
	current := a.dev.Epoch()
	if current == a.epoch {
		return
	}
	a.epoch = current
	for i, occ := range a.slots {
		if occ == nil {
			continue
		}
		occ.session.state = stateExpired
		occ.session.cause = ErrSessionExpired
		a.slots[i] = nil
	}
}

func (a *Adapter) releaseSlotLocked(s *Session) {
	if s.slot < 0 || s.slot >= len(a.slots) {
		return
	}
	occ := a.slots[s.slot]
	if occ != nil && occ.token == s.token {
		a.slots[s.slot] = nil
	}
}
