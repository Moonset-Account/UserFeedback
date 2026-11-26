package attribution

import (
	"testing"
	"time"
)

// testClock is a manually advanced clock for deterministic interleaving.
type testClock struct {
	t time.Time
}

func (c *testClock) now() time.Time { return c.t }

func newTestEngine() (*Engine, *testClock) {
	c := &testClock{t: time.Date(2026, 11, 27, 9, 0, 0, 0, time.UTC)}
	return NewEngineWithClock(c.now), c
}

func principal(p PersonID, name AccountName, at time.Time) Principal {
	return Principal{PersonID: p, AccountName: name, IssuedAt: at}
}

func event(id EventID, p PersonID, at time.Time) DeactivationEvent {
	return DeactivationEvent{ID: id, PersonID: p, At: at, NoticeAt: at}
}

func mustSession(t *testing.T, e *Engine, id SessionID) *Session {
	t.Helper()
	s, err := e.Session(id)
	if err != nil {
		t.Fatalf("Session(%q): unexpected error %v", id, err)
	}
	return s
}

func wantErrIs(t *testing.T, got, want error) {
	t.Helper()
	if got == nil {
		t.Fatalf("expected error %v, got nil", want)
	}
	if got != want {
		t.Fatalf("expected error %v, got %v", want, got)
	}
}

func countKinds(decisions []Decision) map[DecisionKind]int {
	out := map[DecisionKind]int{}
	for _, d := range decisions {
		out[d.Kind]++
	}
	return out
}

// A single-operator session is disconnected wholesale when its only person is
// deactivated, and the evidence is attached to the suspended slot.
func TestOpenSessionAndBasicDeactivation(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t

	s, decisions, err := e.OpenSession("s1", principal("P1", "alice", t0))
	if err != nil {
		t.Fatalf("OpenSession: %v", err)
	}
	if s.State != SessionAlive || len(s.Slots) != 1 || s.CurrentSlot() == nil {
		t.Fatalf("new session not alive with one foreground slot: %+v", s)
	}

	c.t = t0.Add(time.Hour)
	decisions, err = e.ApplyDeactivation(event("E1", "P1", c.t))
	if err != nil {
		t.Fatalf("ApplyDeactivation: %v", err)
	}
	kinds := countKinds(decisions)
	if kinds[DecisionSlotSuspended] != 1 || kinds[DecisionSessionTerminated] != 1 {
		t.Fatalf("want 1 suspend + 1 terminate, got %+v (decisions=%+v)", kinds, decisions)
	}

	s = mustSession(t, e, "s1")
	if s.State != SessionTerminated {
		t.Fatalf("session state = %v, want terminated", s.State)
	}
	if s.CurrentSlot() != nil {
		t.Fatalf("terminated session must have no foreground slot")
	}
	slot := s.Slots[0]
	if slot.State != SlotSuspended || slot.Evidence == nil {
		t.Fatalf("slot not suspended with evidence: %+v", slot)
	}
	if slot.Evidence.EventID != "E1" || slot.Evidence.PersonID != "P1" || slot.Evidence.ObservationCount != 1 {
		t.Fatalf("unexpected evidence: %+v", slot.Evidence)
	}
	if !e.IsDeactivated("P1") {
		t.Fatalf("P1 should be deactivated")
	}
	if e.IsDeactivated("P2") {
		t.Fatalf("P2 must not be deactivated")
	}
}

// On a shared terminal, deactivating one operator suspends only that
// operator's slot; the other operator's slot is retained and moves to the
// foreground. The session terminates only once the last slot goes.
func TestSharedTerminalRetainsOtherSlots(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t

	if _, _, err := e.OpenSession("s1", principal("P1", "alice", t0)); err != nil {
		t.Fatalf("OpenSession: %v", err)
	}
	c.t = t0.Add(30 * time.Minute)
	if _, decisions, err := e.SwitchOperator("s1", SwitchRecord{
		SessionID: "s1", FromPerson: "P1", ToPerson: "P2", ToAccount: "bob", At: c.t,
	}); err != nil {
		t.Fatalf("SwitchOperator: %v", err)
	} else if len(decisions) != 0 {
		t.Fatalf("switch while everyone is active must decide nothing, got %+v", decisions)
	}

	s := mustSession(t, e, "s1")
	if len(s.ActiveSlots()) != 2 || s.CurrentSlot().PersonID != "P2" {
		t.Fatalf("after switch want 2 active slots with P2 foreground, got %+v", s)
	}

	// Deactivate the original operator P1 (currently in background).
	c.t = t0.Add(time.Hour)
	decisions, err := e.ApplyDeactivation(event("E1", "P1", c.t))
	if err != nil {
		t.Fatalf("ApplyDeactivation: %v", err)
	}
	if len(decisions) != 1 || decisions[0].Kind != DecisionSlotSuspended {
		t.Fatalf("want exactly one slot-suspension, got %+v", decisions)
	}
	s = mustSession(t, e, "s1")
	if s.State != SessionAlive {
		t.Fatalf("session should stay alive on P2's slot, got %v", s.State)
	}
	if s.CurrentSlot() == nil || s.CurrentSlot().PersonID != "P2" || s.CurrentSlot().State != SlotActive {
		t.Fatalf("foreground must move to P2's active slot, got %+v", s.CurrentSlot())
	}
	if len(s.ActiveSlots()) != 1 || len(s.SuspendedSlots()) != 1 {
		t.Fatalf("want 1 active and 1 suspended slot, got %+v", s.Slots)
	}
	if s.SuspendedSlots()[0].PersonID != "P1" {
		t.Fatalf("suspended slot must belong to P1")
	}
	if len(s.SwitchLog) != 1 || s.SwitchLog[0].ToPerson != "P2" {
		t.Fatalf("switch record not retained: %+v", s.SwitchLog)
	}

	// Deactivate the remaining operator: now the session must terminate.
	c.t = t0.Add(2 * time.Hour)
	decisions, err = e.ApplyDeactivation(event("E2", "P2", c.t))
	if err != nil {
		t.Fatalf("ApplyDeactivation: %v", err)
	}
	kinds := countKinds(decisions)
	if kinds[DecisionSlotSuspended] != 1 || kinds[DecisionSessionTerminated] != 1 {
		t.Fatalf("want suspend+terminate for last slot, got %+v", decisions)
	}
	s = mustSession(t, e, "s1")
	if s.State != SessionTerminated || len(s.ActiveSlots()) != 0 || s.CurrentSlot() != nil {
		t.Fatalf("session must be terminated with no active/foreground slot: %+v", s)
	}
}

// Recycled account names are disambiguated by the stable natural-person ID:
// an old employee's deactivation must not hit the new employee holding the
// same account name, and the old disconnected session stays disconnected.
func TestAccountNameRecyclingDistinguishesPersons(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t

	// Old employee P1 owns account "ops".
	if _, _, err := e.OpenSession("old", principal("P1", "ops", t0)); err != nil {
		t.Fatalf("OpenSession old: %v", err)
	}
	c.t = t0.Add(time.Hour)
	if _, err := e.ApplyDeactivation(event("E1", "P1", c.t)); err != nil {
		t.Fatalf("ApplyDeactivation P1: %v", err)
	}
	old := mustSession(t, e, "old")
	if old.State != SessionTerminated {
		t.Fatalf("old session should be terminated")
	}

	// Account "ops" is recycled to new employee P3.
	c.t = t0.Add(2 * time.Hour)
	if _, decisions, err := e.OpenSession("new", principal("P3", "ops", c.t)); err != nil {
		t.Fatalf("OpenSession new: %v", err)
	} else if len(decisions) != 0 {
		t.Fatalf("new employee with recycled name must not inherit deactivation, got %+v", decisions)
	}
	newS := mustSession(t, e, "new")
	if newS.State != SessionAlive || newS.Principal.AccountName != "ops" {
		t.Fatalf("new session should be alive under recycled account name: %+v", newS)
	}

	// Another notice about the OLD person must not touch the new person's slot.
	c.t = t0.Add(3 * time.Hour)
	decisions, err := e.ApplyDeactivation(event("E2", "P1", c.t))
	if err != nil {
		t.Fatalf("duplicate-person notice: %v", err)
	}
	for _, d := range decisions {
		if d.SessionID == "new" {
			t.Fatalf("new employee's session must be unaffected by P1 notice, got %+v", d)
		}
	}
	newS = mustSession(t, e, "new")
	if newS.State != SessionAlive {
		t.Fatalf("new session must remain alive, got %v", newS.State)
	}
	old = mustSession(t, e, "old")
	if old.Slots[0].Evidence.ObservationCount != 2 {
		t.Fatalf("old slot evidence should record second observation, got %+v", old.Slots[0].Evidence)
	}

	// Deactivating the NEW person by stable ID disconnects the new session.
	c.t = t0.Add(4 * time.Hour)
	if _, err := e.ApplyDeactivation(event("E3", "P3", c.t)); err != nil {
		t.Fatalf("ApplyDeactivation P3: %v", err)
	}
	newS = mustSession(t, e, "new")
	if newS.State != SessionTerminated {
		t.Fatalf("new session should terminate when P3 is deactivated, got %v", newS.State)
	}
}

// Replayed (duplicate) notice IDs refresh visible evidence and never reopen a
// suspended slot or terminated session.
func TestDuplicateNoticeUpdatesEvidenceWithoutReopen(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t

	if _, _, err := e.OpenSession("s1", principal("P1", "alice", t0)); err != nil {
		t.Fatalf("OpenSession: %v", err)
	}
	c.t = t0.Add(time.Hour)
	first := event("E1", "P1", c.t)
	if _, err := e.ApplyDeactivation(first); err != nil {
		t.Fatalf("first notice: %v", err)
	}

	c.t = t0.Add(2 * time.Hour)
	replay := event("E1", "P1", first.At)
	replay.NoticeAt = c.t
	decisions, err := e.ApplyDeactivation(replay)
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	for _, d := range decisions {
		if d.Kind != DecisionEvidenceUpdated {
			t.Fatalf("replay must only update evidence, got %+v", d)
		}
	}
	s := mustSession(t, e, "s1")
	ev := s.Slots[0].Evidence
	if s.State != SessionTerminated || s.Slots[0].State != SlotSuspended {
		t.Fatalf("duplicate notice must not reopen anything: %+v", s)
	}
	if ev.ObservationCount != 2 || !ev.LastNoticedAt.Equal(c.t) {
		t.Fatalf("evidence not refreshed by replay: %+v", ev)
	}

	// A terminated session cannot be extended by further switches.
	if _, _, err := e.SwitchOperator("s1", SwitchRecord{
		SessionID: "s1", ToPerson: "P9", At: c.t,
	}); err != ErrUnknownSession {
		t.Fatalf("switch on terminated session should fail ErrUnknownSession, got %v", err)
	}
	// Even a brand-new session for the deactivated person is born terminated.
	_, born, err := e.OpenSession("s2", principal("P1", "alice", c.t))
	if err != nil {
		t.Fatalf("OpenSession for deactivated person: %v", err)
	}
	if len(born) == 0 || mustSession(t, e, "s2").State != SessionTerminated {
		t.Fatalf("new session for deactivated person must be born terminated, decisions=%+v", born)
	}
}

// Correction notices update the visible evidence (timestamp + correction
// chain + observation count) while states remain unchanged.
func TestCorrectionNoticeUpdatesEvidence(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t

	if _, _, err := e.OpenSession("s1", principal("P1", "alice", t0)); err != nil {
		t.Fatalf("OpenSession: %v", err)
	}
	c.t = t0.Add(time.Hour)
	original := event("E1", "P1", c.t)
	if _, err := e.ApplyDeactivation(original); err != nil {
		t.Fatalf("original notice: %v", err)
	}

	c.t = t0.Add(2 * time.Hour)
	correctedAt := t0.Add(30 * time.Minute)
	correction := DeactivationEvent{
		ID: "E2", PersonID: "P1", At: correctedAt, NoticeAt: c.t, CorrectionOf: "E1",
	}
	decisions, err := e.ApplyDeactivation(correction)
	if err != nil {
		t.Fatalf("correction: %v", err)
	}
	for _, d := range decisions {
		if d.Kind != DecisionEvidenceUpdated {
			t.Fatalf("correction must only update evidence, got %+v", d)
		}
		if d.Evidence == nil || len(d.Evidence.Corrections) != 1 {
			t.Fatalf("correction decision must carry one correction record, got %+v", d)
		}
	}
	s := mustSession(t, e, "s1")
	if s.State != SessionTerminated || s.Slots[0].State != SlotSuspended {
		t.Fatalf("correction must not reopen the session/slot")
	}
	ev := s.Slots[0].Evidence
	if !ev.DeactivatedAt.Equal(correctedAt) {
		t.Fatalf("corrected deactivation time not visible: %+v", ev)
	}
	if ev.ObservationCount != 2 || len(ev.Corrections) != 1 {
		t.Fatalf("evidence chain not updated: %+v", ev)
	}
	if ev.Corrections[0].OriginalEventID != "E1" || ev.Corrections[0].CorrectedBy != "E2" {
		t.Fatalf("correction record mismatch: %+v", ev.Corrections[0])
	}
}

// A correction can arrive before the original notice; the original arriving
// later is absorbed as another observation and nothing reopens.
func TestCorrectionBeforeOriginalNotice(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t

	if _, _, err := e.OpenSession("s1", principal("P1", "alice", t0)); err != nil {
		t.Fatalf("OpenSession: %v", err)
	}

	// Correction first.
	c.t = t0.Add(time.Hour)
	correction := DeactivationEvent{
		ID: "E9", PersonID: "P1", At: t0.Add(-time.Minute), NoticeAt: c.t, CorrectionOf: "E1",
	}
	decisions, err := e.ApplyDeactivation(correction)
	if err != nil {
		t.Fatalf("early correction: %v", err)
	}
	kinds := countKinds(decisions)
	if kinds[DecisionSlotSuspended] != 1 || kinds[DecisionSessionTerminated] != 1 {
		t.Fatalf("early correction still establishes deactivation, got %+v", decisions)
	}
	s := mustSession(t, e, "s1")
	if len(s.Slots[0].Evidence.Corrections) != 1 {
		t.Fatalf("correction record missing on slot: %+v", s.Slots[0].Evidence)
	}

	// Original notice arrives late: no new suspension, count goes up.
	c.t = t0.Add(2 * time.Hour)
	decisions, err = e.ApplyDeactivation(event("E1", "P1", t0.Add(-time.Minute)))
	if err != nil {
		t.Fatalf("late original: %v", err)
	}
	for _, d := range decisions {
		if d.Kind != DecisionEvidenceUpdated {
			t.Fatalf("late original must be absorbed as observation, got %+v", d)
		}
	}
	s = mustSession(t, e, "s1")
	if s.State != SessionTerminated {
		t.Fatalf("late original must not reopen session")
	}
	if s.Slots[0].Evidence.ObservationCount != 2 {
		t.Fatalf("want observation count 2, got %+v", s.Slots[0].Evidence)
	}
}

// A notice whose deactivation timestamp predates the slot start is flagged
// retroactive in the evidence; a later-dated notice is not.
func TestBackdatedNoticeFlagsRetroactive(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t

	if _, _, err := e.OpenSession("s1", principal("P1", "alice", t0)); err != nil {
		t.Fatalf("OpenSession: %v", err)
	}
	c.t = t0.Add(2 * time.Hour)
	backdated := DeactivationEvent{
		ID: "E1", PersonID: "P1", At: t0.Add(-time.Hour), NoticeAt: c.t,
	}
	if _, err := e.ApplyDeactivation(backdated); err != nil {
		t.Fatalf("backdated notice: %v", err)
	}
	s := mustSession(t, e, "s1")
	if !s.Slots[0].Evidence.Retroactive {
		t.Fatalf("notice predating slot start must be marked retroactive: %+v", s.Slots[0].Evidence)
	}

	// Fresh session, notice dated after the slot start: not retroactive.
	e2, c2 := newTestEngine()
	if _, _, err := e2.OpenSession("s2", principal("P2", "bob", c2.t)); err != nil {
		t.Fatalf("OpenSession: %v", err)
	}
	c2.t = c2.t.Add(2 * time.Hour)
	later := DeactivationEvent{
		ID: "E2", PersonID: "P2", At: c2.t.Add(-30 * time.Minute), NoticeAt: c2.t,
	}
	if _, err := e2.ApplyDeactivation(later); err != nil {
		t.Fatalf("later notice: %v", err)
	}
	s2 := mustSession(t, e2, "s2")
	if s2.Slots[0].Evidence.Retroactive {
		t.Fatalf("notice dated after slot start must not be retroactive: %+v", s2.Slots[0].Evidence)
	}
}

// A session opened for an already-deactivated person is suspended at birth
// and immediately terminated, with evidence attached.
func TestSessionForDeactivatedPersonBornTerminated(t *testing.T) {
	e, c := newTestEngine()
	if _, err := e.ApplyDeactivation(event("E1", "P1", c.t)); err != nil {
		t.Fatalf("ApplyDeactivation: %v", err)
	}
	c.t = c.t.Add(time.Hour)
	s, decisions, err := e.OpenSession("s1", principal("P1", "alice", c.t))
	if err != nil {
		t.Fatalf("OpenSession: %v", err)
	}
	kinds := countKinds(decisions)
	if kinds[DecisionSlotSuspended] != 1 || kinds[DecisionSessionTerminated] != 1 {
		t.Fatalf("new session for deactivated person must be born terminated, got %+v", decisions)
	}
	if s.State != SessionTerminated || s.Slots[0].Evidence == nil || s.CurrentSlot() != nil {
		t.Fatalf("born-terminated session mismatch: %+v", s)
	}
	if !s.Slots[0].Evidence.Retroactive {
		t.Fatalf("slot started after deactivation should be retroactive")
	}
}

// Switching to an already-deactivated operator creates a slot suspended at
// birth, while the other operator's active slot keeps the session alive and
// reclaims the foreground.
func TestSwitchToDeactivatedOperatorSuspendedAtBirth(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t

	if _, err := e.ApplyDeactivation(event("E0", "P2", t0.Add(-time.Hour))); err != nil {
		t.Fatalf("pre-deactivate P2: %v", err)
	}
	if _, _, err := e.OpenSession("s1", principal("P1", "alice", t0)); err != nil {
		t.Fatalf("OpenSession: %v", err)
	}
	c.t = t0.Add(time.Hour)
	_, decisions, err := e.SwitchOperator("s1", SwitchRecord{
		SessionID: "s1", FromPerson: "P1", ToPerson: "P2", ToAccount: "bob", At: c.t,
	})
	if err != nil {
		t.Fatalf("SwitchOperator: %v", err)
	}
	if len(decisions) != 1 || decisions[0].Kind != DecisionSlotSuspended {
		t.Fatalf("new slot for deactivated operator must be suspended at birth, got %+v", decisions)
	}
	s := mustSession(t, e, "s1")
	if s.State != SessionAlive {
		t.Fatalf("session must stay alive on P1's slot, got %v", s.State)
	}
	if s.CurrentSlot() == nil || s.CurrentSlot().PersonID != "P1" {
		t.Fatalf("foreground must fall back to P1, got %+v", s.CurrentSlot())
	}
	if s.Slots[1].State != SlotSuspended || s.Slots[1].Evidence == nil {
		t.Fatalf("P2's new slot must be suspended with evidence")
	}
}

// Interleaved sessions and operators: one deactivation notice fans out to
// every matching slot across sessions, leaves unrelated sessions untouched,
// and decisions come back in deterministic session/slot order.
func TestInterleavedSessionsFanOutAndOrdering(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t

	// s1: P1 then switched to P2.
	if _, _, err := e.OpenSession("s1", principal("P1", "alice", t0)); err != nil {
		t.Fatal(err)
	}
	c.t = t0.Add(15 * time.Minute)
	if _, _, err := e.SwitchOperator("s1", SwitchRecord{
		SessionID: "s1", ToPerson: "P2", ToAccount: "bob", At: c.t,
	}); err != nil {
		t.Fatal(err)
	}
	// s2: P3 only.
	c.t = t0.Add(30 * time.Minute)
	if _, _, err := e.OpenSession("s2", principal("P3", "carol", c.t)); err != nil {
		t.Fatal(err)
	}
	// s3: P1 again, another terminal.
	c.t = t0.Add(45 * time.Minute)
	if _, _, err := e.OpenSession("s3", principal("P1", "alice2", c.t)); err != nil {
		t.Fatal(err)
	}

	c.t = t0.Add(time.Hour)
	decisions, err := e.ApplyDeactivation(event("E1", "P1", c.t))
	if err != nil {
		t.Fatalf("ApplyDeactivation P1: %v", err)
	}
	// Expect: s1 slot-0 suspended (session alive via P2), s3 slot suspended
	// then terminated. Unrelated s2 untouched. Order is session creation
	// order with terminate immediately following its suspension.
	want := []struct {
		kind DecisionKind
		sid  SessionID
		slot SlotID
	}{
		{DecisionSlotSuspended, "s1", "slot-0"},
		{DecisionSlotSuspended, "s3", "slot-0"},
		{DecisionSessionTerminated, "s3", "slot-0"},
	}
	if len(decisions) != len(want) {
		t.Fatalf("decision count = %d, want %d: %+v", len(decisions), len(want), decisions)
	}
	for i, w := range want {
		if decisions[i].Kind != w.kind || decisions[i].SessionID != w.sid || decisions[i].SlotID != w.slot {
			t.Fatalf("decision %d = %+v, want %+v", i, decisions[i], w)
		}
	}

	s1 := mustSession(t, e, "s1")
	s2 := mustSession(t, e, "s2")
	s3 := mustSession(t, e, "s3")
	if s1.State != SessionAlive || s1.CurrentSlot().PersonID != "P2" {
		t.Fatalf("s1 should survive with P2 foreground: %+v", s1)
	}
	if s2.State != SessionAlive {
		t.Fatalf("unrelated s2 must be untouched: %+v", s2)
	}
	if s3.State != SessionTerminated {
		t.Fatalf("single-operator s3 must terminate: %+v", s3)
	}

	// Finish the remaining sessions independently.
	c.t = t0.Add(2 * time.Hour)
	if _, err := e.ApplyDeactivation(event("E2", "P2", c.t)); err != nil {
		t.Fatal(err)
	}
	c.t = t0.Add(3 * time.Hour)
	if _, err := e.ApplyDeactivation(event("E3", "P3", c.t)); err != nil {
		t.Fatal(err)
	}
	if mustSession(t, e, "s1").State != SessionTerminated ||
		mustSession(t, e, "s2").State != SessionTerminated {
		t.Fatalf("s1 and s2 must both be terminated eventually")
	}
	if len(e.Sessions()) != 3 {
		t.Fatalf("Sessions() must retain all three sessions in order")
	}
	if e.Sessions()[0].ID != "s1" || e.Sessions()[1].ID != "s2" || e.Sessions()[2].ID != "s3" {
		t.Fatalf("Sessions() ordering broken: %+v", e.Sessions())
	}
}

// Validation and lookup error paths.
func TestValidationAndLookupErrors(t *testing.T) {
	e, c := newTestEngine()

	if _, err := e.ApplyDeactivation(DeactivationEvent{}); err != ErrInvalidEvent {
		t.Fatalf("empty event: got %v, want ErrInvalidEvent", err)
	}
	if _, err := e.ApplyDeactivation(DeactivationEvent{ID: "E1", At: c.t}); err != ErrInvalidEvent {
		t.Fatalf("missing person: got %v, want ErrInvalidEvent", err)
	}
	if _, err := e.ApplyDeactivation(DeactivationEvent{ID: "E1", PersonID: "P1"}); err != ErrInvalidEvent {
		t.Fatalf("zero timestamp: got %v, want ErrInvalidEvent", err)
	}
	if _, _, err := e.OpenSession("s1", Principal{AccountName: "x"}); err != ErrEmptyPrincipal {
		t.Fatalf("empty principal: got %v, want ErrEmptyPrincipal", err)
	}
	if _, _, err := e.OpenSession("", principal("P1", "x", c.t)); err != ErrUnknownSession {
		t.Fatalf("empty session id: got %v, want ErrUnknownSession", err)
	}
	if _, _, err := e.SwitchOperator("missing", SwitchRecord{ToPerson: "P2", At: c.t}); err != ErrUnknownSession {
		t.Fatalf("switch unknown session: got %v, want ErrUnknownSession", err)
	}
	if _, err := e.Session("missing"); err != ErrUnknownSession {
		t.Fatalf("lookup unknown session: got %v, want ErrUnknownSession", err)
	}

	if _, _, err := e.OpenSession("s1", principal("P1", "alice", c.t)); err != nil {
		t.Fatal(err)
	}
	if _, _, err := e.SwitchOperator("s1", SwitchRecord{
		SessionID: "other", ToPerson: "P2", At: c.t,
	}); err != ErrInconsistentSwitch {
		t.Fatalf("inconsistent switch: got %v, want ErrInconsistentSwitch", err)
	}
	if _, _, err := e.SwitchOperator("s1", SwitchRecord{At: c.t}); err != ErrEmptyPrincipal {
		t.Fatalf("switch without target person: got %v, want ErrEmptyPrincipal", err)
	}

	// Reopening the same session ID is rejected.
	if _, _, err := e.OpenSession("s1", principal("P1", "alice", c.t)); err != ErrUnknownSession {
		t.Fatalf("duplicate OpenSession: got %v, want ErrUnknownSession", err)
	}
}

// Returned sessions are defensive copies: callers cannot mutate engine state.
func TestReturnedSessionsAreCopies(t *testing.T) {
	e, c := newTestEngine()
	if _, _, err := e.OpenSession("s1", principal("P1", "alice", c.t)); err != nil {
		t.Fatal(err)
	}
	s := mustSession(t, e, "s1")
	s.State = SessionTerminated
	s.Slots[0].State = SlotSuspended
	s.Slots[0].PersonID = "PX"

	again := mustSession(t, e, "s1")
	if again.State != SessionAlive || again.Slots[0].State != SlotActive || again.Slots[0].PersonID != "P1" {
		t.Fatalf("engine state leaked through returned copy: %+v", again)
	}
}

// Notices about a person with no matching slot still produce a visible
// evidence-update decision; a later session for that person is born terminated.
func TestNoticeWithoutMatchingSessionStillRecorded(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t

	first := event("E1", "P1", t0)
	decisions, err := e.ApplyDeactivation(first)
	if err != nil {
		t.Fatal(err)
	}
	if len(decisions) != 1 || decisions[0].Kind != DecisionEvidenceUpdated ||
		decisions[0].Evidence == nil || decisions[0].Evidence.ObservationCount != 1 {
		t.Fatalf("first notice with no sessions should record evidence, got %+v", decisions)
	}

	// Replayed event ID, still no sessions: fallback evidence decision.
	c.t = t0.Add(time.Hour)
	replay := event("E1", "P1", t0)
	replay.NoticeAt = c.t
	decisions, err = e.ApplyDeactivation(replay)
	if err != nil {
		t.Fatal(err)
	}
	if len(decisions) != 1 || decisions[0].Kind != DecisionEvidenceUpdated ||
		decisions[0].Evidence.ObservationCount != 2 {
		t.Fatalf("replay with no sessions should refresh count, got %+v", decisions)
	}

	// Correction with no matching slot: correction chain + fallback decision.
	c.t = t0.Add(2 * time.Hour)
	correction := DeactivationEvent{
		ID: "E2", PersonID: "P1", At: t0.Add(-time.Minute), NoticeAt: c.t, CorrectionOf: "E1",
	}
	decisions, err = e.ApplyDeactivation(correction)
	if err != nil {
		t.Fatal(err)
	}
	if len(decisions) != 1 || decisions[0].Kind != DecisionEvidenceUpdated ||
		len(decisions[0].Evidence.Corrections) != 1 || decisions[0].Evidence.ObservationCount != 3 {
		t.Fatalf("correction with no sessions should extend chain, got %+v", decisions)
	}
	if !e.IsDeactivated("P1") {
		t.Fatalf("P1 must remain marked deactivated")
	}
}

// Query helpers and state text stay consistent across the lifecycle.
func TestSlotHelpersAndStateText(t *testing.T) {
	if got := (SlotState(99)).String(); got != "unknown" {
		t.Fatalf("unknown slot state text = %q", got)
	}
	if got := (SessionState(99)).String(); got != "unknown" {
		t.Fatalf("unknown session state text = %q", got)
	}
	if SlotActive.String() != "active" || SlotSuspended.String() != "suspended" {
		t.Fatalf("slot state text mismatch")
	}
	if SessionAlive.String() != "alive" || SessionTerminated.String() != "terminated" {
		t.Fatalf("session state text mismatch")
	}
	if DecisionSlotSuspended.String() != "slot-suspended" ||
		DecisionSessionTerminated.String() != "session-terminated" ||
		DecisionEvidenceUpdated.String() != "evidence-updated" ||
		(DecisionKind(99)).String() != "unknown" {
		t.Fatalf("decision kind text mismatch")
	}

	e, c := newTestEngine()
	s, _, err := e.OpenSession("s1", principal("P1", "alice", c.t))
	if err != nil {
		t.Fatal(err)
	}
	if s.CurrentSlot() == nil || s.CurrentSlot().ID != "slot-0" {
		t.Fatalf("initial foreground slot mismatch: %+v", s.CurrentSlot())
	}
	if len(s.ActiveSlots()) != 1 || len(s.SuspendedSlots()) != 0 {
		t.Fatalf("initial slot helpers mismatch")
	}

	c.t = c.t.Add(time.Hour)
	if _, err := e.ApplyDeactivation(event("E1", "P1", c.t)); err != nil {
		t.Fatal(err)
	}
	s = mustSession(t, e, "s1")
	if s.CurrentSlot() != nil {
		t.Fatalf("terminated session foreground must be nil")
	}
	if len(s.ActiveSlots()) != 0 || len(s.SuspendedSlots()) != 1 {
		t.Fatalf("post-termination slot helpers mismatch")
	}
}

// A second original-style notice for an already deactivated person who has no
// current slots emits the fallback evidence-update decision rather than
// silently dropping the observation.
func TestRepeatedOriginalNoticeNoSlotsFallback(t *testing.T) {
	e, c := newTestEngine()
	t0 := c.t
	if _, err := e.ApplyDeactivation(event("E1", "P1", t0)); err != nil {
		t.Fatal(err)
	}
	c.t = t0.Add(time.Hour)
	decisions, err := e.ApplyDeactivation(event("E8", "P1", t0))
	if err != nil {
		t.Fatal(err)
	}
	if len(decisions) != 1 || decisions[0].Kind != DecisionEvidenceUpdated {
		t.Fatalf("second original notice without slots should fall back to evidence update, got %+v", decisions)
	}
	if decisions[0].Evidence.ObservationCount != 2 {
		t.Fatalf("observation count should be 2, got %d", decisions[0].Evidence.ObservationCount)
	}
}
