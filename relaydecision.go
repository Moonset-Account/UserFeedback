// Package relaydecision implements the outbound transmission decision module
// for a local mail relay adapter.
//
// The engine consumes four pieces of input for every queued transmission:
//
//   - the destination domain's encryption requirement (DomainPolicy),
//   - the prioritized candidate relay list,
//   - session handshake outcomes reported back by the local adapter, and
//   - the retry deadline of the queued transmission.
//
// Each Evaluate call returns exactly one of: the next candidate relay, a
// deferral (with the earliest re-evaluation time), or termination (with the
// governing reason). The module never opens network connections itself; it
// only records what the local adapter reports and decides what the adapter
// should do next.
package relaydecision

import (
	"errors"
	"sort"
	"sync"
	"time"
)

// TLSLevel is the encryption requirement of a destination domain.
type TLSLevel int

const (
	// TLSNone means encryption is not required; plaintext transmission is fine.
	TLSNone TLSLevel = iota
	// TLSOpportunistic means encryption is used when a candidate supports it;
	// plaintext fallback is allowed when no encrypting candidate is available.
	TLSOpportunistic
	// TLSRequired means every selected candidate must support encryption.
	// The requirement is never lowered, neither on the preferred relay nor on
	// any fallback candidate.
	TLSRequired
)

// String returns a stable textual name for the TLS requirement level.
func (l TLSLevel) String() string {
	switch l {
	case TLSNone:
		return "none"
	case TLSOpportunistic:
		return "opportunistic"
	case TLSRequired:
		return "required"
	default:
		return "unknown"
	}
}

// Reason explains why a decision was made. Reason values are stable strings so
// local adapters can branch on them without depending on Go internals.
type Reason string

const (
	// Next-candidate reasons.
	ReasonFirstCandidate    Reason = "first_candidate"
	ReasonFallbackCandidate Reason = "fallback_candidate"
	ReasonPolicyRecheck     Reason = "policy_recheck_reselect"
	ReasonRetryRound        Reason = "retry_round_started"

	// Deferral reasons.
	ReasonHandshakeInFlight Reason = "handshake_in_flight"
	ReasonBackoffNotElapsed Reason = "backoff_not_elapsed"
	ReasonRoundExhausted    Reason = "all_candidates_exhausted_round"

	// Termination reasons.
	ReasonEstablished         Reason = "transmission_established"
	ReasonDeadlineExceeded    Reason = "retry_deadline_exceeded"
	ReasonNoEligibleCandidate Reason = "no_eligible_candidate"

	// Attempt-record reasons (why a candidate could not be/remain selected).
	ReasonRelayNoTLS                 Reason = "relay_lacks_required_tls"
	ReasonIdentityVerificationFailed Reason = "identity_verification_failed"
	ReasonConnectFailed              Reason = "connect_failure"
	ReasonPlaintextFallback          Reason = "plaintext_fallback_allowed"
)

// Kind classifies a decision.
type Kind int

const (
	// KindNext instructs the adapter to try the returned relay.
	KindNext Kind = iota
	// KindDefer instructs the adapter to wait until Decision.NotBefore.
	KindDefer
	// KindTerminate means no further attempt will ever be scheduled.
	KindTerminate
)

// HandshakeOutcome is the result the local adapter reports for a candidate it
// was told to try.
type HandshakeOutcome int

const (
	// HandshakeSuccess means the session was established (including a passed
	// identity check when TLS was negotiated).
	HandshakeSuccess HandshakeOutcome = iota
	// HandshakeIdentityFailure means the candidate offered TLS but certificate
	// or identity verification failed. The requirement is not downgraded and
	// the same candidate is not retried within the round.
	HandshakeIdentityFailure
	// HandshakeTLSUnsupported means the candidate could not provide TLS at
	// handshake time even though it advertised the capability.
	HandshakeTLSUnsupported
	// HandshakeConnectFailure is a generic connectivity / protocol failure.
	HandshakeConnectFailure
)

// Static validation and state errors.
var (
	ErrEmptyID                = errors.New("relaydecision: transmission id is empty")
	ErrEmptyDomain            = errors.New("relaydecision: destination domain is empty")
	ErrNoRelays               = errors.New("relaydecision: candidate relay list is empty")
	ErrInvalidPriority        = errors.New("relaydecision: relay priority must be positive")
	ErrDuplicateRelay         = errors.New("relaydecision: duplicate relay name in candidate list")
	ErrInvalidDeadline        = errors.New("relaydecision: retry deadline must be in the future")
	ErrDomainPolicyNotFound   = errors.New("relaydecision: no domain policy registered")
	ErrDuplicateTransmission  = errors.New("relaydecision: transmission id already enqueued")
	ErrUnknownTransmission    = errors.New("relaydecision: unknown transmission id")
	ErrNoActiveCandidate      = errors.New("relaydecision: no active candidate awaiting a handshake report")
	ErrUnknownOutcome         = errors.New("relaydecision: unknown handshake outcome")
	ErrInvalidTLSLevel        = errors.New("relaydecision: invalid TLS level in domain policy")
	ErrPolicyVersionRegressed = errors.New("relaydecision: domain policy version must not move backwards")
)

// Relay is a candidate outbound relay. Lower Priority values are preferred;
// ties are broken by relay name for deterministic ordering.
type Relay struct {
	Name        string
	Priority    int
	SupportsTLS bool
}

// DomainPolicy is the encryption requirement of a destination domain. Version
// must increase every time the policy is replaced so queued transmissions can
// detect that a recheck is due.
type DomainPolicy struct {
	Domain  string
	TLS     TLSLevel
	Version int64
}

// Attempt records what happened to a candidate during one selection.
type Attempt struct {
	RelayName string
	Round     int
	Outcome   HandshakeOutcome
	Reason    Reason
	Detail    string
	At        time.Time
}

// EnqueueRequest describes a newly queued outbound transmission.
type EnqueueRequest struct {
	ID       string
	Domain   string
	Relays   []Relay
	Deadline time.Time
}

// Transmission is an external snapshot of a queued transmission's state.
type Transmission struct {
	ID            string
	Domain        string
	Relays        []Relay
	Deadline      time.Time
	PolicyVersion int64
	TLSAtEnqueue  TLSLevel
	Round         int
	Established   bool
	Terminal      Reason
	Attempts      []Attempt
}

// Decision is the single instruction returned for one Evaluate call.
type Decision struct {
	Kind Kind
	// Relay is non-nil only for KindNext. It points at a copy owned by the
	// caller, so mutating it cannot corrupt engine state.
	Relay *Relay
	// Reason is the machine-readable decision code.
	Reason Reason
	// Basis is a human-readable description of the rule that produced the
	// decision, for adapter logs.
	Basis string
	// NotBefore is the earliest sensible re-evaluation time for KindDefer.
	NotBefore time.Time
	// Deadline is the retry deadline of the transmission.
	Deadline time.Time
	// Round is the current attempt round (0-based).
	Round int
	// Attempts is a copy of the attempt history accumulated so far.
	Attempts []Attempt
}

// Engine holds domain policies and the state of all queued transmissions. It
// is safe for concurrent use.
type Engine struct {
	mu          sync.Mutex
	policies    map[string]DomainPolicy
	txs         map[string]*txState
	now         func() time.Time
	backoffBase time.Duration
	backoffMax  time.Duration
}

// txState is the mutable, internal state of one transmission.
type txState struct {
	id            string
	domain        string
	relays        []Relay
	deadline      time.Time
	policyVersion int64
	tlsAtEnqueue  TLSLevel
	round         int
	cursor        int
	active        bool
	established   bool
	terminal      Reason
	recheck       bool
	nextAttemptAt time.Time
	attempts      []Attempt
}

// NewEngine creates an engine using the wall clock with default backoff
// parameters (30s base, 10m cap).
func NewEngine() *Engine {
	return NewEngineWithClock(time.Now, 30*time.Second, 10*time.Minute)
}

// NewEngineWithClock creates an engine with an injectable clock and backoff
// parameters, primarily for deterministic testing.
func NewEngineWithClock(now func() time.Time, backoffBase, backoffMax time.Duration) *Engine {
	if now == nil {
		now = time.Now
	}
	if backoffBase <= 0 {
		backoffBase = 30 * time.Second
	}
	if backoffMax < backoffBase {
		backoffMax = backoffBase
	}
	return &Engine{
		policies:    make(map[string]DomainPolicy),
		txs:         make(map[string]*txState),
		now:         now,
		backoffBase: backoffBase,
		backoffMax:  backoffMax,
	}
}

// ApplyPolicy stores or replaces a domain policy. A replacement must carry a
// version greater than or equal to the stored one. Queued, not-yet-established
// transmissions pick the change up on their next Evaluate call.
func (e *Engine) ApplyPolicy(p DomainPolicy) error {
	if p.Domain == "" {
		return ErrEmptyDomain
	}
	if p.TLS != TLSNone && p.TLS != TLSOpportunistic && p.TLS != TLSRequired {
		return ErrInvalidTLSLevel
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if old, ok := e.policies[p.Domain]; ok && p.Version < old.Version {
		return ErrPolicyVersionRegressed
	}
	e.policies[p.Domain] = p
	return nil
}

// Enqueue adds a transmission to the queue.
func (e *Engine) Enqueue(req EnqueueRequest) error {
	if req.ID == "" {
		return ErrEmptyID
	}
	if req.Domain == "" {
		return ErrEmptyDomain
	}
	if len(req.Relays) == 0 {
		return ErrNoRelays
	}
	names := make(map[string]struct{}, len(req.Relays))
	for _, r := range req.Relays {
		if r.Priority <= 0 {
			return ErrInvalidPriority
		}
		if r.Name == "" {
			return ErrEmptyID
		}
		if _, dup := names[r.Name]; dup {
			return ErrDuplicateRelay
		}
		names[r.Name] = struct{}{}
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if _, ok := e.txs[req.ID]; ok {
		return ErrDuplicateTransmission
	}
	pol, ok := e.policies[req.Domain]
	if !ok {
		return ErrDomainPolicyNotFound
	}
	if !req.Deadline.After(e.now()) {
		return ErrInvalidDeadline
	}
	relays := make([]Relay, len(req.Relays))
	copy(relays, req.Relays)
	sort.SliceStable(relays, func(i, j int) bool {
		if relays[i].Priority != relays[j].Priority {
			return relays[i].Priority < relays[j].Priority
		}
		return relays[i].Name < relays[j].Name
	})
	e.txs[req.ID] = &txState{
		id:            req.ID,
		domain:        req.Domain,
		relays:        relays,
		deadline:      req.Deadline,
		policyVersion: pol.Version,
		tlsAtEnqueue:  pol.TLS,
	}
	return nil
}

// PendingRechecks returns the IDs of queued transmissions whose stored policy
// version is stale relative to the current domain policy. Established and
// terminal transmissions are excluded.
func (e *Engine) PendingRechecks() []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	var ids []string
	for id, st := range e.txs {
		if st.established || st.terminal != "" {
			continue
		}
		if pol, ok := e.policies[st.domain]; ok && pol.Version != st.policyVersion {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	return ids
}

// Snapshot returns a detached copy of a transmission's current state.
func (e *Engine) Snapshot(id string) (Transmission, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	st, ok := e.txs[id]
	if !ok {
		return Transmission{}, ErrUnknownTransmission
	}
	relays := make([]Relay, len(st.relays))
	copy(relays, st.relays)
	return Transmission{
		ID:            st.id,
		Domain:        st.domain,
		Relays:        relays,
		Deadline:      st.deadline,
		PolicyVersion: st.policyVersion,
		TLSAtEnqueue:  st.tlsAtEnqueue,
		Round:         st.round,
		Established:   st.established,
		Terminal:      st.terminal,
		Attempts:      copyAttempts(st.attempts),
	}, nil
}

// Evaluate returns the single next instruction for the given transmission:
// try the next candidate, defer, or terminate.
func (e *Engine) Evaluate(id string) (Decision, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	st, ok := e.txs[id]
	if !ok {
		return Decision{}, ErrUnknownTransmission
	}
	now := e.now()

	if st.established {
		return st.decisionTerminate(ReasonEstablished,
			"transmission already established; no further candidate selection"), nil
	}
	if st.terminal != "" {
		return st.decisionTerminate(st.terminal, "transmission is terminal"), nil
	}
	if st.active {
		// The adapter is currently handshaking the selected candidate; policy
		// recheck waits until that attempt is reported back.
		return st.decisionDefer(ReasonHandshakeInFlight, now,
			"handshake with selected candidate in flight; report its outcome first"), nil
	}

	// Re-check the domain policy. Any version change (including a tightening
	// such as opportunistic -> required) forces candidate selection to restart
	// from the highest-priority relay under the new requirement.
	if pol, ok := e.policies[st.domain]; ok && pol.Version != st.policyVersion {
		st.policyVersion = pol.Version
		st.round = 0
		st.cursor = 0
		st.attempts = nil
		st.nextAttemptAt = time.Time{}
		st.recheck = true
	}

	// The retry deadline is a hard boundary, including equality.
	if !now.Before(st.deadline) {
		st.terminal = ReasonDeadlineExceeded
		return st.decisionTerminate(ReasonDeadlineExceeded,
			"retry deadline reached without an established session"), nil
	}
	if !st.nextAttemptAt.IsZero() && now.Before(st.nextAttemptAt) {
		return st.decisionDefer(ReasonBackoffNotElapsed, now,
			"backoff window from the exhausted candidate round is still open"), nil
	}

	return e.decide(st, now), nil
}

// decide walks the candidate list from the current cursor and returns the next
// instruction. The caller must hold the engine mutex.
func (e *Engine) decide(st *txState, now time.Time) Decision {
	pol := e.policies[st.domain]
	level := pol.TLS

	for st.cursor < len(st.relays) {
		relay := st.relays[st.cursor]
		if level == TLSRequired && !relay.SupportsTLS {
			// Required domain: an unencrypted candidate is skipped outright.
			// The same encryption requirement still applies to every later
			// candidate; nothing is downgraded.
			st.attempts = append(st.attempts, Attempt{
				RelayName: relay.Name,
				Round:     st.round,
				Outcome:   HandshakeTLSUnsupported,
				Reason:    ReasonRelayNoTLS,
				Detail:    "candidate cannot satisfy required TLS for domain",
				At:        now,
			})
			st.cursor++
			continue
		}

		reason := ReasonFirstCandidate
		switch {
		case st.recheck:
			reason = ReasonPolicyRecheck
			st.recheck = false
		case st.round > 0 && st.cursor == 0:
			reason = ReasonRetryRound
		case st.cursor > 0 || len(st.attempts) > 0:
			reason = ReasonFallbackCandidate
		}
		st.active = true
		basis := "selecting highest-priority eligible candidate"
		if reason == ReasonFallbackCandidate {
			basis = "previous candidate could not complete the session; moving to the next candidate without lowering the domain requirement"
		}
		if reason == ReasonPolicyRecheck {
			basis = "domain policy changed; re-selecting from the top of the candidate list under TLS level " + level.String()
		}
		if reason == ReasonRetryRound {
			basis = "backoff elapsed; starting a new candidate round under the unchanged domain requirement"
		}
		return st.decisionNext(relay, reason, basis)
	}

	// The whole list was walked this round. If no relay in the list can ever
	// satisfy the current requirement (only possible for TLS-required domains
	// once every TLS-capable relay is gone), waiting cannot help: terminate.
	eligibleExists := false
	for _, relay := range st.relays {
		if level != TLSRequired || relay.SupportsTLS {
			eligibleExists = true
			break
		}
	}
	if !eligibleExists {
		st.terminal = ReasonNoEligibleCandidate
		return st.decisionTerminate(ReasonNoEligibleCandidate,
			"no candidate can satisfy the required TLS level of the domain")
	}

	// Otherwise schedule another round with exponential backoff, but never
	// schedule a retry that would start at or past the deadline.
	st.round++
	st.cursor = 0
	backoff := e.backoffFor(st.round)
	st.nextAttemptAt = now.Add(backoff)
	if !st.nextAttemptAt.Before(st.deadline) {
		st.terminal = ReasonDeadlineExceeded
		return st.decisionTerminate(ReasonDeadlineExceeded,
			"next retry round would start at or after the retry deadline")
	}
	return st.decisionDefer(ReasonRoundExhausted, now,
		"every candidate was tried in this round; backing off before the next round")
}

// ReportHandshake records the outcome the local adapter observed for the
// candidate it was last told to try.
func (e *Engine) ReportHandshake(id string, outcome HandshakeOutcome, detail string) error {
	if outcome < HandshakeSuccess || outcome > HandshakeConnectFailure {
		return ErrUnknownOutcome
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	st, ok := e.txs[id]
	if !ok {
		return ErrUnknownTransmission
	}
	if st.established || st.terminal != "" || !st.active {
		return ErrNoActiveCandidate
	}
	relay := st.relays[st.cursor]
	now := e.now()

	switch outcome {
	case HandshakeSuccess:
		st.attempts = append(st.attempts, Attempt{
			RelayName: relay.Name,
			Round:     st.round,
			Outcome:   HandshakeSuccess,
			Reason:    ReasonEstablished,
			Detail:    detail,
			At:        now,
		})
		st.established = true
		st.active = false
		return nil
	case HandshakeIdentityFailure:
		// TLS was offered but identity verification failed. Record the reason
		// and move on; the encryption requirement is not lowered on this
		// candidate and the candidate is not retried in this round.
		st.attempts = append(st.attempts, Attempt{
			RelayName: relay.Name,
			Round:     st.round,
			Outcome:   HandshakeIdentityFailure,
			Reason:    ReasonIdentityVerificationFailed,
			Detail:    detail,
			At:        now,
		})
		st.cursor++
		st.active = false
		return nil
	case HandshakeTLSUnsupported:
		reason := ReasonPlaintextFallback
		detailText := detail
		if e.policies[st.domain].TLS == TLSRequired {
			// Capability mismatch on a required domain: record and reject this
			// candidate without downgrading the requirement.
			reason = ReasonRelayNoTLS
			if detailText == "" {
				detailText = "selected candidate failed to provide TLS at handshake time"
			}
		} else if detailText == "" {
			detailText = "TLS unavailable; domain policy permits plaintext fallback"
		}
		st.attempts = append(st.attempts, Attempt{
			RelayName: relay.Name,
			Round:     st.round,
			Outcome:   HandshakeTLSUnsupported,
			Reason:    reason,
			Detail:    detailText,
			At:        now,
		})
		st.cursor++
		st.active = false
		return nil
	default: // HandshakeConnectFailure
		st.attempts = append(st.attempts, Attempt{
			RelayName: relay.Name,
			Round:     st.round,
			Outcome:   HandshakeConnectFailure,
			Reason:    ReasonConnectFailed,
			Detail:    detail,
			At:        now,
		})
		st.cursor++
		st.active = false
		return nil
	}
}

// backoffFor returns the exponential backoff before round n (n >= 1), capped
// at the configured maximum.
func (e *Engine) backoffFor(round int) time.Duration {
	d := e.backoffBase
	for i := 1; i < round; i++ {
		d *= 2
		if d >= e.backoffMax {
			return e.backoffMax
		}
	}
	if d > e.backoffMax {
		return e.backoffMax
	}
	return d
}

func copyAttempts(in []Attempt) []Attempt {
	if len(in) == 0 {
		return nil
	}
	out := make([]Attempt, len(in))
	copy(out, in)
	return out
}

func (st *txState) decisionNext(relay Relay, reason Reason, basis string) Decision {
	cp := relay
	return Decision{
		Kind:     KindNext,
		Relay:    &cp,
		Reason:   reason,
		Basis:    basis,
		Deadline: st.deadline,
		Round:    st.round,
		Attempts: copyAttempts(st.attempts),
	}
}

func (st *txState) decisionDefer(reason Reason, now time.Time, basis string) Decision {
	return Decision{
		Kind:      KindDefer,
		Reason:    reason,
		Basis:     basis,
		NotBefore: st.nextAttemptAt,
		Deadline:  st.deadline,
		Round:     st.round,
		Attempts:  copyAttempts(st.attempts),
	}
}

func (st *txState) decisionTerminate(reason Reason, basis string) Decision {
	return Decision{
		Kind:     KindTerminate,
		Reason:   reason,
		Basis:    basis,
		Deadline: st.deadline,
		Round:    st.round,
		Attempts: copyAttempts(st.attempts),
	}
}
