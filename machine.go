package pinstate

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
)

// Status is the externally observable result of a PIN verification request.
type Status string

const (
	// StatusOK means the PIN was correct and the counter was reset.
	StatusOK Status = "OK"
	// StatusBadPIN means the PIN was wrong and the card is not locked yet.
	StatusBadPIN Status = "BAD_PIN"
	// StatusLocked means no further PIN verification is accepted.
	StatusLocked Status = "LOCKED"
	// StatusConflict means an attempt number was reused with different content.
	StatusConflict Status = "CONFLICT"
	// StatusRejected means a stale or otherwise non-retryable attempt was sent.
	StatusRejected Status = "REJECTED"
)

var (
	ErrInvalidRequest  = errors.New("pinstate: invalid request")
	ErrAttemptConflict = errors.New("pinstate: attempt number reused with different request content")
	ErrStaleAttempt    = errors.New("pinstate: attempt number is older than session high-water mark")
	ErrConfigMismatch  = errors.New("pinstate: configuration does not match persistent state")
	ErrFatal           = errors.New("pinstate: machine needs recovery")
)

// Request is one reader-to-state-machine verification call.
//
// SessionID identifies a card session. Attempt must be monotonically
// non-decreasing/new within that session. The exact same Request may be
// retried; a different PIN or content under an already-used attempt is
// rejected.
type Request struct {
	SessionID string
	Attempt   uint64
	PIN       string
}

// Decision is the durable, idempotent result of a request.
type Decision struct {
	Status            Status `json:"status"`
	SessionID         string `json:"session_id"`
	Attempt           uint64 `json:"attempt"`
	Failures          int    `json:"failures"`
	MaxFailures       int    `json:"max_failures"`
	AttemptsRemaining int    `json:"attempts_remaining"`
	Locked            bool   `json:"locked"`
	Replayed          bool   `json:"replayed"`
}

// Allowed reports whether the decision grants access.
func (d Decision) Allowed() bool { return d.Status == StatusOK }

// CardStatus is the durable card-level retry state. It is independent of any
// particular reader session.
type CardStatus struct {
	CardID            string `json:"card_id"`
	Failures          int    `json:"failures"`
	MaxFailures       int    `json:"max_failures"`
	AttemptsRemaining int    `json:"attempts_remaining"`
	Locked            bool   `json:"locked"`
	Generation        uint64 `json:"generation"`
	ActivePage        int    `json:"active_page"`
}

// PINVerifier keeps PIN checking behind a small local interface. Production
// code can back it with a secure element or constant-time comparator.
type PINVerifier interface {
	VerifyPIN(pin string) bool
}

type PINVerifierFunc func(pin string) bool

func (f PINVerifierFunc) VerifyPIN(pin string) bool { return f(pin) }

// StaticPINVerifier compares against a local expected PIN using constant time.
// It is intended for tests and demos.
func StaticPINVerifier(expected string) PINVerifier {
	want := []byte(expected)
	return PINVerifierFunc(func(got string) bool {
		return subtle.ConstantTimeCompare([]byte(got), want) == 1
	})
}

type options struct {
	cardID      string
	maxFailures int
}

type Option func(*options)

// WithCardID records an optional card identifier in the persistent pages.
// If a non-empty ID is supplied on reopen and differs from storage, Open fails
// rather than operating on the wrong card.
func WithCardID(cardID string) Option {
	return func(o *options) { o.cardID = cardID }
}

// WithMaxFailures configures the number of consecutive wrong PINs that locks
// the card. Zero means "use the persisted value, or 3 for a blank store".
func WithMaxFailures(maxFailures int) Option {
	return func(o *options) { o.maxFailures = maxFailures }
}

type storedState struct {
	CardID      string                    `json:"card_id"`
	MaxFailures int                       `json:"max_failures"`
	Failures    int                       `json:"failures"`
	Locked      bool                      `json:"locked"`
	Sessions    map[string]*storedSession `json:"sessions"`
}

type storedSession struct {
	HighWater uint64                   `json:"high_water"`
	Attempts  map[uint64]storedAttempt `json:"attempts"`
}

type storedAttempt struct {
	Fingerprint string   `json:"fingerprint"`
	Decision    Decision `json:"decision"`
}

func (s *storedState) validate() error {
	if s.MaxFailures < 1 {
		return fmt.Errorf("max_failures must be positive")
	}
	if s.Failures < 0 || s.Failures > s.MaxFailures {
		return fmt.Errorf("failures %d outside [0,%d]", s.Failures, s.MaxFailures)
	}
	if s.Locked && s.Failures < s.MaxFailures {
		return fmt.Errorf("locked card has only %d/%d failures", s.Failures, s.MaxFailures)
	}
	if !s.Locked && s.Failures >= s.MaxFailures {
		return fmt.Errorf("failure threshold reached but locked is false")
	}
	if s.Sessions == nil {
		return fmt.Errorf("sessions map is nil")
	}

	for sessionID, session := range s.Sessions {
		if sessionID == "" || session == nil || session.Attempts == nil {
			return fmt.Errorf("invalid session %q", sessionID)
		}
		for attempt, record := range session.Attempts {
			if attempt == 0 || attempt > session.HighWater {
				return fmt.Errorf("attempt %d outside session high-water %d", attempt, session.HighWater)
			}
			if record.Fingerprint == "" {
				return fmt.Errorf("missing fingerprint for attempt %d", attempt)
			}
			if record.Decision.SessionID != sessionID || record.Decision.Attempt != attempt {
				return fmt.Errorf("decision key mismatch for attempt %d", attempt)
			}
		}
	}
	return nil
}

func newFreshState(cardID string, maxFailures int) storedState {
	if cardID == "" {
		cardID = "card-0"
	}
	if maxFailures <= 0 {
		maxFailures = 3
	}
	return storedState{
		CardID:      cardID,
		MaxFailures: maxFailures,
		Sessions:    make(map[string]*storedSession),
	}
}

func (s *storedState) clone() storedState {
	clone := *s
	clone.Sessions = make(map[string]*storedSession, len(s.Sessions))
	for id, session := range s.Sessions {
		sessionClone := &storedSession{
			HighWater: session.HighWater,
			Attempts:  make(map[uint64]storedAttempt, len(session.Attempts)),
		}
		for attempt, record := range session.Attempts {
			sessionClone.Attempts[attempt] = record
		}
		clone.Sessions[id] = sessionClone
	}
	return clone
}

func requestFingerprint(req Request) string {
	h := sha256.New()
	writeSized(h, []byte("pinstate-request-v1"))
	writeSized(h, []byte(req.SessionID))
	var attempt [8]byte
	bigEndianPutUint64(attempt[:], req.Attempt)
	writeSized(h, attempt[:])
	writeSized(h, []byte(req.PIN))

	var sum [sha256.Size]byte
	copy(sum[:], h.Sum(nil))
	return hex.EncodeToString(sum[:])
}

// StateMachine is a mutex-protected PIN retry state machine backed by two
// alternating checksummed pages.
type StateMachine struct {
	store    PageReadWriter
	verifier PINVerifier

	requestedCardID string
	requestedMax    int

	mu         sync.Mutex
	fatal      error
	state      storedState
	activePage int
	generation uint64
}

// New creates or reopens a state machine. A blank store starts with three
// attempts unless WithMaxFailures changes the threshold.
func New(store PageReadWriter, verifier PINVerifier, opts ...Option) (*StateMachine, error) {
	return Open(store, verifier, opts...)
}

// Open is the explicit reopen entry point.
func Open(store PageReadWriter, verifier PINVerifier, opts ...Option) (*StateMachine, error) {
	if store == nil {
		return nil, errors.New("pinstate: nil PageReadWriter")
	}
	if verifier == nil {
		return nil, errors.New("pinstate: nil PINVerifier")
	}

	cfg := options{}
	for _, opt := range opts {
		opt(&cfg)
	}
	if cfg.maxFailures < 0 {
		return nil, fmt.Errorf("%w: max failures must not be negative", ErrInvalidRequest)
	}

	m := &StateMachine{
		store:           store,
		verifier:        verifier,
		requestedCardID: cfg.cardID,
		requestedMax:    cfg.maxFailures,
		activePage:      -1,
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	if err := m.recoverLocked(); err != nil {
		return nil, err
	}

	if cfg.cardID != "" && cfg.cardID != m.state.CardID {
		return nil, fmt.Errorf("%w: card id %q != %q", ErrConfigMismatch, cfg.cardID, m.state.CardID)
	}
	if cfg.maxFailures > 0 && cfg.maxFailures != m.state.MaxFailures {
		return nil, fmt.Errorf("%w: max failures %d != persisted %d", ErrConfigMismatch, cfg.maxFailures, m.state.MaxFailures)
	}
	return m, nil
}

// NewStateMachine is a descriptive alias for callers that prefer that name.
func NewStateMachine(store PageReadWriter, verifier PINVerifier, opts ...Option) (*StateMachine, error) {
	return New(store, verifier, opts...)
}

func (m *StateMachine) recoverLocked() error {
	type candidate struct {
		envelope pageEnvelope
		state    storedState
	}

	var candidates []candidate
	sawNonBlankInvalidPage := false

	for pageID := 0; pageID < DualPages; pageID++ {
		raw, err := m.store.ReadPage(pageID)
		if err != nil {
			return fmt.Errorf("pinstate: read page %d: %w", pageID, err)
		}

		envelope, state, err := decodePage(raw, pageID)
		if err != nil {
			if errors.Is(err, ErrBlankPage) {
				continue
			}
			sawNonBlankInvalidPage = true
			continue
		}
		candidates = append(candidates, candidate{envelope: envelope, state: *state})
	}

	if len(candidates) == 0 {
		if sawNonBlankInvalidPage {
			m.fatal = ErrNoRecoverableState
			return ErrNoRecoverableState
		}
		maxFailures := m.requestedMax
		if maxFailures == 0 {
			maxFailures = 3
		}
		m.state = newFreshState(m.requestedCardID, maxFailures)
		m.activePage = -1
		m.generation = 0
		m.fatal = nil
		return nil
	}

	best := candidates[0]
	for _, candidate := range candidates[1:] {
		if candidate.envelope.Generation > best.envelope.Generation {
			best = candidate
			continue
		}
		if candidate.envelope.Generation == best.envelope.Generation &&
			candidate.envelope.Checksum != best.envelope.Checksum {
			m.fatal = ErrAmbiguousPages
			return fmt.Errorf("%w: generation %d", ErrAmbiguousPages, best.envelope.Generation)
		}
	}

	m.state = best.state
	m.activePage = best.envelope.PageID
	m.generation = best.envelope.Generation
	m.fatal = nil
	return nil
}

// Status returns a copy of the durable card status.
func (m *StateMachine) Status() (CardStatus, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.fatal != nil {
		return CardStatus{}, m.fatal
	}

	return CardStatus{
		CardID:            m.state.CardID,
		Failures:          m.state.Failures,
		MaxFailures:       m.state.MaxFailures,
		AttemptsRemaining: m.state.MaxFailures - m.state.Failures,
		Locked:            m.state.Locked,
		Generation:        m.generation,
		ActivePage:        m.activePage,
	}, nil
}

// Verify processes or replays one request. Normal PIN outcomes are returned
// with a nil error; invalid reuse and persistence failures are errors.
func (m *StateMachine) Verify(req Request) (Decision, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.fatal != nil {
		return Decision{}, m.fatal
	}
	if req.SessionID == "" {
		return Decision{}, fmt.Errorf("%w: empty session id", ErrInvalidRequest)
	}
	if req.Attempt == 0 {
		return Decision{}, fmt.Errorf("%w: attempt must be positive", ErrInvalidRequest)
	}

	fingerprint := requestFingerprint(req)

	if session := m.state.Sessions[req.SessionID]; session != nil {
		if record, ok := session.Attempts[req.Attempt]; ok {
			if subtle.ConstantTimeCompare([]byte(record.Fingerprint), []byte(fingerprint)) != 1 {
				decision := m.currentDecisionLocked(StatusConflict, req)
				return decision, fmt.Errorf("%w: session=%q attempt=%d", ErrAttemptConflict, req.SessionID, req.Attempt)
			}

			decision := record.Decision
			decision.Replayed = true
			return decision, nil
		}

		if req.Attempt <= session.HighWater {
			decision := m.currentDecisionLocked(StatusRejected, req)
			return decision, fmt.Errorf("%w: session=%q attempt=%d high_water=%d",
				ErrStaleAttempt, req.SessionID, req.Attempt, session.HighWater)
		}
	}

	next := m.state.clone()
	session, ok := next.Sessions[req.SessionID]
	if !ok {
		session = &storedSession{Attempts: make(map[uint64]storedAttempt)}
		next.Sessions[req.SessionID] = session
	}

	decision := decideLocked(&next, m.verifier, req)
	session.HighWater = req.Attempt
	session.Attempts[req.Attempt] = storedAttempt{
		Fingerprint: fingerprint,
		Decision:    decision,
	}

	if err := m.commitLocked(next); err != nil {
		// Do not return decision as a confirmed external answer: durability is
		// unknown. A caller should restart/retry the same request.
		return Decision{}, err
	}
	return decision, nil
}

func (m *StateMachine) currentDecisionLocked(status Status, req Request) Decision {
	remaining := m.state.MaxFailures - m.state.Failures
	if remaining < 0 {
		remaining = 0
	}
	return Decision{
		Status:            status,
		SessionID:         req.SessionID,
		Attempt:           req.Attempt,
		Failures:          m.state.Failures,
		MaxFailures:       m.state.MaxFailures,
		AttemptsRemaining: remaining,
		Locked:            m.state.Locked,
	}
}

func decideLocked(state *storedState, verifier PINVerifier, req Request) Decision {
	base := Decision{
		SessionID:   req.SessionID,
		Attempt:     req.Attempt,
		MaxFailures: state.MaxFailures,
	}

	// A locked card never clears its counter, even for a correct PIN.
	if state.Locked {
		base.Status = StatusLocked
		base.Failures = state.Failures
		base.AttemptsRemaining = 0
		base.Locked = true
		return base
	}

	if verifier.VerifyPIN(req.PIN) {
		state.Failures = 0
		state.Locked = false

		base.Status = StatusOK
		base.Failures = 0
		base.AttemptsRemaining = state.MaxFailures
		return base
	}

	state.Failures++
	if state.Failures >= state.MaxFailures {
		state.Failures = state.MaxFailures
		state.Locked = true

		base.Status = StatusLocked
		base.Failures = state.Failures
		base.AttemptsRemaining = 0
		base.Locked = true
		return base
	}

	base.Status = StatusBadPIN
	base.Failures = state.Failures
	base.AttemptsRemaining = state.MaxFailures - state.Failures
	return base
}

func (m *StateMachine) commitLocked(next storedState) error {
	newGeneration := m.generation + 1
	targetPage := pageA
	if m.activePage == pageA {
		targetPage = pageB
	}

	data, err := encodePage(&next, targetPage, newGeneration)
	if err != nil {
		return err
	}

	writeErr := m.store.WritePage(targetPage, data)
	if writeErr != nil {
		// The page may actually be durable despite an error/power notification.
		// Reload so an in-process retry cannot build on stale state.
		if recoverErr := m.recoverLocked(); recoverErr != nil {
			m.fatal = fmt.Errorf("%w: write error=%v; recovery error=%v", ErrFatal, writeErr, recoverErr)
			return m.fatal
		}
		return fmt.Errorf("pinstate: persist page %d: %w", targetPage, writeErr)
	}

	m.state = next
	m.generation = newGeneration
	m.activePage = targetPage
	m.fatal = nil
	return nil
}

// bigEndianPutUint64 avoids importing encoding/binary only for fingerprint.
func bigEndianPutUint64(b []byte, v uint64) {
	_ = b[7]
	b[0] = byte(v >> 56)
	b[1] = byte(v >> 48)
	b[2] = byte(v >> 40)
	b[3] = byte(v >> 32)
	b[4] = byte(v >> 24)
	b[5] = byte(v >> 16)
	b[6] = byte(v >> 8)
	b[7] = byte(v)
}
