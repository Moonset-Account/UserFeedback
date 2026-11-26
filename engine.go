package attribution

import (
	"time"
)

// DecisionKind enumerates the kinds of changes produced by an engine
// operation. Decisions are returned in a deterministic order:
// SlotSuspended entries (by session ID, then slot index) first, then
// SessionTerminated entries, then EvidenceUpdated entries.
type DecisionKind int

const (
	// DecisionSlotSuspended: one work slot was set to SlotSuspended.
	DecisionSlotSuspended DecisionKind = iota
	// DecisionSessionTerminated: a session lost its last active slot and was
	// disconnected. Terminated sessions can never be reopened.
	DecisionSessionTerminated
	// DecisionEvidenceUpdated: a duplicate or correction notice updated the
	// visible evidence of an already suspended slot/terminated session.
	DecisionEvidenceUpdated
)

// String returns a stable textual representation of the decision kind.
func (k DecisionKind) String() string {
	switch k {
	case DecisionSlotSuspended:
		return "slot-suspended"
	case DecisionSessionTerminated:
		return "session-terminated"
	case DecisionEvidenceUpdated:
		return "evidence-updated"
	default:
		return "unknown"
	}
}

// Decision describes one effect of an engine operation.
type Decision struct {
	Kind      DecisionKind
	SessionID SessionID
	SlotID    SlotID // empty for session-level decisions without a specific slot
	PersonID  PersonID
	EventID   EventID
	Evidence  *DisconnectEvidence // snapshot pointer; nil when not applicable
}

// Engine holds established sessions, the deactivation ledger and a clock.
// It is safe for single-goroutine use; concurrent callers must synchronize.
type Engine struct {
	now func() time.Time

	sessions map[SessionID]*Session
	events   map[EventID]*DeactivationEvent
	// deactivated maps a natural person to the head evidence chain of their
	// deactivation. Presence means the person is currently deactivated.
	deactivated map[PersonID]*DisconnectEvidence
	// evidenceByEvent locates the evidence chain an original event belongs to.
	evidenceByEvent map[EventID]*DisconnectEvidence
	// sessionOrder keeps insertion order so decision ordering is stable.
	sessionOrder []SessionID
}

// NewEngine creates an engine using the real wall clock.
func NewEngine() *Engine {
	return NewEngineWithClock(time.Now)
}

// NewEngineWithClock creates an engine whose "now" is supplied by clockFn.
// Injecting a fixed clock makes interleaved and backdated scenarios
// deterministically testable.
func NewEngineWithClock(clockFn func() time.Time) *Engine {
	if clockFn == nil {
		clockFn = time.Now
	}
	return &Engine{
		now:             clockFn,
		sessions:        map[SessionID]*Session{},
		events:          map[EventID]*DeactivationEvent{},
		deactivated:     map[PersonID]*DisconnectEvidence{},
		evidenceByEvent: map[EventID]*DisconnectEvidence{},
	}
}

// OpenSession establishes a session for an already authenticated principal.
// If the principal's natural person is already deactivated, the initial slot
// is suspended at birth and the session is terminated immediately: an
// existing deactivation cannot be escaped by opening a new session.
func (e *Engine) OpenSession(id SessionID, p Principal) (*Session, []Decision, error) {
	if id == "" {
		return nil, nil, ErrUnknownSession
	}
	if _, exists := e.sessions[id]; exists {
		return nil, nil, ErrUnknownSession
	}
	if p.PersonID == "" {
		return nil, nil, ErrEmptyPrincipal
	}
	if p.IssuedAt.IsZero() {
		p.IssuedAt = e.now()
	}

	session := &Session{
		ID:        id,
		Principal: p,
		State:     SessionAlive,
	}
	initial := &WorkSlot{
		ID:          SlotID("slot-0"),
		PersonID:    p.PersonID,
		AccountName: p.AccountName,
		State:       SlotActive,
		StartedAt:   p.IssuedAt,
	}
	session.Slots = []*WorkSlot{initial}
	session.CurrentSlotID = initial.ID

	e.sessions[id] = session
	e.sessionOrder = append(e.sessionOrder, id)

	var decisions []Decision
	if evidence, off := e.deactivated[p.PersonID]; off {
		decisions = append(decisions, e.suspendSlot(session, initial, evidence, e.now())...)
	}
	return session, decisions, nil
}

// SwitchOperator records an operator switch on a shared terminal. The current
// slot is preserved and a new work slot is created for the incoming operator,
// which becomes the foreground slot. If the incoming operator is already
// deactivated, the new slot is suspended at creation; the session is only
// terminated if that leaves no active slot.
func (e *Engine) SwitchOperator(sessionID SessionID, rec SwitchRecord) (*Session, []Decision, error) {
	session, ok := e.sessions[sessionID]
	if !ok {
		return nil, nil, ErrUnknownSession
	}
	if session.State == SessionTerminated {
		return nil, nil, ErrUnknownSession
	}
	if rec.SessionID != "" && rec.SessionID != sessionID {
		return nil, nil, ErrInconsistentSwitch
	}
	if rec.ToPerson == "" {
		return nil, nil, ErrEmptyPrincipal
	}
	if rec.At.IsZero() {
		rec.At = e.now()
	}
	rec.SessionID = sessionID
	session.SwitchLog = append(session.SwitchLog, rec)

	slot := &WorkSlot{
		ID:          SlotID("slot-" + itoa(len(session.Slots))),
		PersonID:    rec.ToPerson,
		AccountName: rec.ToAccount,
		State:       SlotActive,
		StartedAt:   rec.At,
	}
	session.Slots = append(session.Slots, slot)
	session.CurrentSlotID = slot.ID

	var decisions []Decision
	if evidence, off := e.deactivated[rec.ToPerson]; off {
		decisions = append(decisions, e.suspendSlot(session, slot, evidence, e.now())...)
	}
	return session, decisions, nil
}

// Session returns a copy of the tracked session, or ErrUnknownSession.
func (e *Engine) Session(id SessionID) (*Session, error) {
	s, ok := e.sessions[id]
	if !ok {
		return nil, ErrUnknownSession
	}
	return cloneSession(s), nil
}

// Sessions returns copies of all tracked sessions in creation order.
func (e *Engine) Sessions() []*Session {
	out := make([]*Session, 0, len(e.sessionOrder))
	for _, id := range e.sessionOrder {
		out = append(out, cloneSession(e.sessions[id]))
	}
	return out
}

// IsDeactivated reports whether the natural person is currently deactivated.
func (e *Engine) IsDeactivated(p PersonID) bool {
	_, ok := e.deactivated[p]
	return ok
}

// ApplyDeactivation applies a directory deactivation notice.
//
// Every active work slot owned by the deactivated natural person (matched by
// stable PersonID, never by recycled account name) is suspended. Sessions
// whose last active slot disappears are terminated and can never be reopened.
// Duplicate notices increment the observation count and refresh the last
// noticed timestamp; correction notices also append a correction record. Both
// update the visible disconnect evidence but never change a suspended or
// terminated state back to active.
func (e *Engine) ApplyDeactivation(ev DeactivationEvent) ([]Decision, error) {
	if err := validateEvent(ev); err != nil {
		return nil, err
	}
	if ev.NoticeAt.IsZero() {
		ev.NoticeAt = ev.At
	}
	noticeAt := ev.NoticeAt
	if noticeAt.IsZero() {
		noticeAt = e.now()
	}

	// A replayed event ID is a pure duplicate: refresh the chain and return
	// evidence-update decisions, never re-suspending or reopening anything.
	if _, seen := e.events[ev.ID]; seen {
		return e.applyDuplicate(ev, noticeAt), nil
	}
	e.events[ev.ID] = &ev

	if ev.CorrectionOf != "" {
		return e.applyCorrection(ev, noticeAt), nil
	}

	// Original (non-correction) notice for this person.
	evidence, alreadyOff := e.deactivated[ev.PersonID]
	if !alreadyOff {
		evidence = &DisconnectEvidence{
			EventID:          ev.ID,
			PersonID:         ev.PersonID,
			DeactivatedAt:    ev.At,
			FirstNoticedAt:   noticeAt,
			LastNoticedAt:    noticeAt,
			ObservationCount: 1,
		}
		e.deactivated[ev.PersonID] = evidence
	} else {
		// Person already deactivated: another original-style notice (for
		// example the original arriving after an earlier correction) is an
		// additional observation on the existing chain.
		evidence.ObservationCount++
		evidence.LastNoticedAt = noticeAt
	}
	e.evidenceByEvent[ev.ID] = evidence

	var decisions []Decision
	for _, sid := range e.sessionOrder {
		session := e.sessions[sid]
		for _, slot := range session.Slots {
			if slot.PersonID != ev.PersonID {
				continue
			}
			switch slot.State {
			case SlotActive:
				decisions = append(decisions, e.suspendSlot(session, slot, evidence, noticeAt)...)
			case SlotSuspended:
				// Already disconnected: refresh the visible evidence only.
				// This can never reopen the slot or its session.
				refreshSlotEvidence(slot, evidence)
				decisions = append(decisions, Decision{
					Kind:      DecisionEvidenceUpdated,
					SessionID: session.ID,
					SlotID:    slot.ID,
					PersonID:  ev.PersonID,
					EventID:   ev.ID,
					Evidence:  cloneEvidence(slot.Evidence),
				})
			}
		}
	}
	if len(decisions) == 0 {
		// No slot matched (for example the notice predates every session, or
		// all matching slots are already gone): still surface a visible
		// evidence-update decision so the observation is never silent.
		decisions = append(decisions, Decision{
			Kind:     DecisionEvidenceUpdated,
			PersonID: ev.PersonID,
			EventID:  ev.ID,
			Evidence: cloneEvidence(evidence),
		})
	}
	return decisions, nil
}

// applyDuplicate handles a replayed event ID.
func (e *Engine) applyDuplicate(ev DeactivationEvent, noticeAt time.Time) []Decision {
	evidence, ok := e.evidenceByEvent[ev.ID]
	if !ok {
		evidence = e.deactivated[ev.PersonID]
	}
	evidence.ObservationCount++
	evidence.LastNoticedAt = noticeAt

	var decisions []Decision
	for _, sid := range e.sessionOrder {
		session := e.sessions[sid]
		for _, slot := range session.Slots {
			if slot.PersonID != ev.PersonID || slot.Evidence == nil {
				continue
			}
			if !sameEvidenceChain(slot.Evidence, evidence) {
				continue
			}
			refreshSlotEvidence(slot, evidence)
			decisions = append(decisions, Decision{
				Kind:      DecisionEvidenceUpdated,
				SessionID: session.ID,
				SlotID:    slot.ID,
				PersonID:  ev.PersonID,
				EventID:   ev.ID,
				Evidence:  cloneEvidence(slot.Evidence),
			})
		}
	}
	if len(decisions) == 0 {
		decisions = append(decisions, Decision{
			Kind:     DecisionEvidenceUpdated,
			PersonID: ev.PersonID,
			EventID:  ev.ID,
			Evidence: cloneEvidence(evidence),
		})
	}
	return decisions
}

// applyCorrection handles an event carrying CorrectionOf. The original notice
// need not have been seen yet; if it arrives later it is treated as a
// duplicate observation. Evidence is updated, states never revert.
func (e *Engine) applyCorrection(ev DeactivationEvent, noticeAt time.Time) []Decision {
	original, originalSeen := e.events[ev.CorrectionOf]

	evidence, chainExists := e.deactivated[ev.PersonID]
	if !chainExists {
		// Correction before any original notice: it still establishes that
		// the person is deactivated, anchored to the corrected event ID.
		evidence = &DisconnectEvidence{
			EventID:          ev.ID,
			PersonID:         ev.PersonID,
			DeactivatedAt:    ev.At,
			FirstNoticedAt:   noticeAt,
			LastNoticedAt:    noticeAt,
			ObservationCount: 1,
		}
		e.deactivated[ev.PersonID] = evidence
	} else {
		evidence.ObservationCount++
		evidence.LastNoticedAt = noticeAt
		if !ev.At.IsZero() {
			evidence.DeactivatedAt = ev.At
		}
	}
	e.evidenceByEvent[ev.ID] = evidence

	correction := Correction{
		OriginalEventID: ev.CorrectionOf,
		CorrectedBy:     ev.ID,
		At:              noticeAt,
	}
	evidence.Corrections = append(evidence.Corrections, correction)
	if originalSeen {
		e.evidenceByEvent[original.ID] = evidence
	}

	var decisions []Decision
	// Suspend anything that still matches and is active (possible only when
	// the correction is the first notice seen for the person).
	for _, sid := range e.sessionOrder {
		session := e.sessions[sid]
		for _, slot := range session.Slots {
			if slot.PersonID != ev.PersonID || slot.State != SlotActive {
				continue
			}
			decisions = append(decisions, e.suspendSlot(session, slot, evidence, noticeAt)...)
		}
	}

	// Update the visible evidence on every already-suspended slot in the chain.
	for _, sid := range e.sessionOrder {
		session := e.sessions[sid]
		for _, slot := range session.Slots {
			if slot.PersonID != ev.PersonID || slot.Evidence == nil {
				continue
			}
			if !sameEvidenceChain(slot.Evidence, evidence) {
				continue
			}
			refreshSlotEvidence(slot, evidence)
			decisions = append(decisions, Decision{
				Kind:      DecisionEvidenceUpdated,
				SessionID: session.ID,
				SlotID:    slot.ID,
				PersonID:  ev.PersonID,
				EventID:   ev.ID,
				Evidence:  cloneEvidence(slot.Evidence),
			})
		}
	}
	if len(decisions) == 0 {
		decisions = append(decisions, Decision{
			Kind:     DecisionEvidenceUpdated,
			PersonID: ev.PersonID,
			EventID:  ev.ID,
			Evidence: cloneEvidence(evidence),
		})
	}
	return decisions
}

// suspendSlot marks a slot suspended, reconciles the foreground slot and
// terminates the session when no active slot remains. Returns one
// SlotSuspended decision plus, when applicable, one SessionTerminated decision.
func (e *Engine) suspendSlot(session *Session, slot *WorkSlot, evidence *DisconnectEvidence, noticedAt time.Time) []Decision {
	chain := cloneEvidence(evidence)
	if slot.StartedAt.After(chain.DeactivatedAt) || slot.StartedAt.Equal(chain.DeactivatedAt) {
		chain.Retroactive = true
	}
	slot.State = SlotSuspended
	slot.Evidence = chain

	decisions := []Decision{{
		Kind:      DecisionSlotSuspended,
		SessionID: session.ID,
		SlotID:    slot.ID,
		PersonID:  slot.PersonID,
		EventID:   chain.EventID,
		Evidence:  cloneEvidence(chain),
	}}

	if session.CurrentSlotID == slot.ID {
		session.CurrentSlotID = ""
		if next := firstActiveSlot(session); next != nil {
			session.CurrentSlotID = next.ID
		}
	}
	if len(session.ActiveSlots()) == 0 {
		session.State = SessionTerminated
		session.CurrentSlotID = ""
		decisions = append(decisions, Decision{
			Kind:      DecisionSessionTerminated,
			SessionID: session.ID,
			SlotID:    slot.ID,
			PersonID:  slot.PersonID,
			EventID:   chain.EventID,
			Evidence:  cloneEvidence(chain),
		})
	}
	return decisions
}

func validateEvent(ev DeactivationEvent) error {
	if ev.PersonID == "" || ev.ID == "" {
		return ErrInvalidEvent
	}
	if ev.At.IsZero() {
		return ErrInvalidEvent
	}
	return nil
}

func firstActiveSlot(s *Session) *WorkSlot {
	for _, slot := range s.Slots {
		if slot.State == SlotActive {
			return slot
		}
	}
	return nil
}

func sameEvidenceChain(a, b *DisconnectEvidence) bool {
	if a == nil || b == nil {
		return false
	}
	return a.PersonID == b.PersonID && a.FirstNoticedAt.Equal(b.FirstNoticedAt)
}

// refreshSlotEvidence pulls the latest chain fields onto a suspended slot
// while preserving the slot-specific Retroactive marker. A suspended slot is
// never reactivated by this refresh.
func refreshSlotEvidence(slot *WorkSlot, chain *DisconnectEvidence) {
	retro := slot.Evidence.Retroactive
	updated := cloneEvidence(chain)
	updated.Retroactive = retro
	slot.Evidence = updated
}

func cloneEvidence(in *DisconnectEvidence) *DisconnectEvidence {
	if in == nil {
		return nil
	}
	out := *in
	if in.Corrections != nil {
		out.Corrections = append([]Correction(nil), in.Corrections...)
	}
	return &out
}

func cloneSession(in *Session) *Session {
	out := *in
	out.Slots = make([]*WorkSlot, len(in.Slots))
	for i, slot := range in.Slots {
		cp := *slot
		cp.Evidence = cloneEvidence(slot.Evidence)
		out.Slots[i] = &cp
	}
	if in.SwitchLog != nil {
		out.SwitchLog = append([]SwitchRecord(nil), in.SwitchLog...)
	}
	return &out
}

// itoa is a tiny allocation-free integer-to-string helper (strconv is fine too;
// this keeps slot ID construction local).
func itoa(n int) string {
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
