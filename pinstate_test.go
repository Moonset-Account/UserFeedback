package pinstate_test

import (
	"errors"
	"testing"

	"pinstate"
)

const testCorrectPIN = "123456"

func newMachine(t *testing.T) (*pinstate.StateMachine, *pinstate.MemoryPageStore) {
	t.Helper()
	store := pinstate.NewMemoryPageStore()
	machine, err := pinstate.New(store, pinstate.StaticPINVerifier(testCorrectPIN))
	if err != nil {
		t.Fatalf("New() error = %v", err)
	}
	return machine, store
}

func req(session string, attempt uint64, pin string) pinstate.Request {
	return pinstate.Request{SessionID: session, Attempt: attempt, PIN: pin}
}

func requireDecision(t *testing.T, d pinstate.Decision, err error, want pinstate.Status, failures, remaining int, locked bool) {
	t.Helper()
	if err != nil {
		t.Fatalf("Verify() unexpected error = %v", err)
	}
	if d.Status != want {
		t.Fatalf("status = %q, want %q (decision=%+v)", d.Status, want, d)
	}
	if d.Failures != failures {
		t.Fatalf("failures = %d, want %d", d.Failures, failures)
	}
	if d.AttemptsRemaining != remaining {
		t.Fatalf("remaining = %d, want %d", d.AttemptsRemaining, remaining)
	}
	if d.Locked != locked {
		t.Fatalf("locked = %v, want %v", d.Locked, locked)
	}
}

func TestCorrectPINResetsConsecutiveFailures(t *testing.T) {
	m, _ := newMachine(t)

	d, err := m.Verify(req("s1", 1, "000000"))
	requireDecision(t, d, err, pinstate.StatusBadPIN, 1, 2, false)

	d, err = m.Verify(req("s1", 2, testCorrectPIN))
	requireDecision(t, d, err, pinstate.StatusOK, 0, 3, false)
	if !d.Allowed() {
		t.Fatal("OK decision must allow access")
	}

	d, err = m.Verify(req("s1", 3, "000000"))
	requireDecision(t, d, err, pinstate.StatusBadPIN, 1, 2, false)

	d, err = m.Verify(req("s1", 4, "000000"))
	requireDecision(t, d, err, pinstate.StatusBadPIN, 2, 1, false)

	d, err = m.Verify(req("s1", 5, testCorrectPIN))
	requireDecision(t, d, err, pinstate.StatusOK, 0, 3, false)
}

func TestThreeWrongPINsLockAndCorrectPINDoesNotClear(t *testing.T) {
	m, store := newMachine(t)

	requireDecision(t, mustVerify(t, m, req("s1", 1, "wrong-1")), nil, pinstate.StatusBadPIN, 1, 2, false)
	requireDecision(t, mustVerify(t, m, req("s1", 2, "wrong-2")), nil, pinstate.StatusBadPIN, 2, 1, false)
	requireDecision(t, mustVerify(t, m, req("s1", 3, "wrong-3")), nil, pinstate.StatusLocked, 3, 0, true)

	// The immediately following correct PIN must not reset a locked card.
	d, err := m.Verify(req("s1", 4, testCorrectPIN))
	requireDecision(t, d, err, pinstate.StatusLocked, 3, 0, true)

	// Reopen from pages and verify persistence.
	reopened, err := pinstate.Open(store, pinstate.StaticPINVerifier(testCorrectPIN))
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	d, err = reopened.Verify(req("s1", 5, testCorrectPIN))
	requireDecision(t, d, err, pinstate.StatusLocked, 3, 0, true)

	status, err := reopened.Status()
	if err != nil {
		t.Fatalf("Status() error = %v", err)
	}
	if !status.Locked || status.Failures != 3 || status.AttemptsRemaining != 0 {
		t.Fatalf("bad persisted status: %+v", status)
	}
}

func mustVerify(t *testing.T, m *pinstate.StateMachine, r pinstate.Request) pinstate.Decision {
	t.Helper()
	d, err := m.Verify(r)
	if err != nil {
		t.Fatalf("Verify(%+v) error = %v", r, err)
	}
	return d
}

func TestSameRequestReplayAndChangedContentConflict(t *testing.T) {
	m, store := newMachine(t)
	original := req("session", 7, "original-pin")

	first, err := m.Verify(original)
	if err != nil {
		t.Fatalf("first Verify() error = %v", err)
	}
	if first.Replayed {
		t.Fatal("first decision must not be marked replay")
	}

	// In-process retry returns the same durable decision.
	retry, err := m.Verify(original)
	if err != nil {
		t.Fatalf("retry Verify() error = %v", err)
	}
	if !retry.Replayed || retry.Status != first.Status || retry.Failures != first.Failures {
		t.Fatalf("bad replay: first=%+v retry=%+v", first, retry)
	}

	// Changed PIN/content under the same session+attempt must be rejected.
	changed := original
	changed.PIN = "different-pin"
	conflict, err := m.Verify(changed)
	if !errors.Is(err, pinstate.ErrAttemptConflict) {
		t.Fatalf("changed request error = %v, want ErrAttemptConflict", err)
	}
	if conflict.Status != pinstate.StatusConflict {
		t.Fatalf("conflict status = %q, want CONFLICT", conflict.Status)
	}

	// Restart from persistent pages: both replay and conflict survive.
	reopened, err := pinstate.Open(store, pinstate.StaticPINVerifier(testCorrectPIN))
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	retryAfterRestart, err := reopened.Verify(original)
	if err != nil {
		t.Fatalf("retry after restart error = %v", err)
	}
	if !retryAfterRestart.Replayed || retryAfterRestart.Status != first.Status {
		t.Fatalf("bad replay after restart: %+v", retryAfterRestart)
	}

	_, err = reopened.Verify(changed)
	if !errors.Is(err, pinstate.ErrAttemptConflict) {
		t.Fatalf("changed request after restart error = %v, want ErrAttemptConflict", err)
	}

	// The conflict must not overwrite the original decision.
	originalAfterConflict, err := reopened.Verify(original)
	if err != nil {
		t.Fatalf("original after conflict error = %v", err)
	}
	if originalAfterConflict.Status != first.Status || !originalAfterConflict.Replayed {
		t.Fatalf("original decision changed after conflict: %+v", originalAfterConflict)
	}
}

func TestMonotonicAttemptRejectsUnknownOldNumber(t *testing.T) {
	m, _ := newMachine(t)

	// Sparse monotonic numbers are allowed; 10 becomes the high-water mark.
	if _, err := m.Verify(req("s", 10, "wrong")); err != nil {
		t.Fatalf("attempt 10 error = %v", err)
	}
	if _, err := m.Verify(req("s", 11, testCorrectPIN)); err != nil {
		t.Fatalf("attempt 11 error = %v", err)
	}

	_, err := m.Verify(req("s", 9, "wrong"))
	if !errors.Is(err, pinstate.ErrStaleAttempt) {
		t.Fatalf("old unknown attempt error = %v, want ErrStaleAttempt", err)
	}

	// Zero is invalid.
	_, err = m.Verify(req("s", 0, "wrong"))
	if !errors.Is(err, pinstate.ErrInvalidRequest) {
		t.Fatalf("zero attempt error = %v, want ErrInvalidRequest", err)
	}

	// Empty session is invalid.
	_, err = m.Verify(req("", 1, "wrong"))
	if !errors.Is(err, pinstate.ErrInvalidRequest) {
		t.Fatalf("empty session error = %v, want ErrInvalidRequest", err)
	}
}

func TestSessionSwitchRetainsCardFailuresAndLock(t *testing.T) {
	m, store := newMachine(t)

	interleaved := []pinstate.Request{
		req("session-1", 1, "wrong"),
		req("session-2", 1, "wrong"),
		req("session-1", 2, "wrong"),
	}
	wantFailures := []int{1, 2, 3}
	wantStatus := []pinstate.Status{pinstate.StatusBadPIN, pinstate.StatusBadPIN, pinstate.StatusLocked}
	for i, r := range interleaved {
		d, err := m.Verify(r)
		if err != nil {
			t.Fatalf("Verify(%+v) error = %v", r, err)
		}
		if d.Status != wantStatus[i] || d.Failures != wantFailures[i] {
			t.Fatalf("request %d: got (%s,%d), want (%s,%d)", i, d.Status, d.Failures, wantStatus[i], wantFailures[i])
		}
	}

	// A fresh session has its own attempt sequence but shares card lock state.
	d, err := m.Verify(req("session-3", 1, testCorrectPIN))
	requireDecision(t, d, err, pinstate.StatusLocked, 3, 0, true)

	// Old session requests still replay their own decisions.
	d, err = m.Verify(interleaved[0])
	if err != nil || !d.Replayed || d.Status != pinstate.StatusBadPIN {
		t.Fatalf("session-1 replay failed: d=%+v err=%v", d, err)
	}

	reopened, err := pinstate.Open(store, pinstate.StaticPINVerifier(testCorrectPIN))
	if err != nil {
		t.Fatalf("Open() error = %v", err)
	}
	d, err = reopened.Verify(req("session-4-after-power-loss", 1, testCorrectPIN))
	requireDecision(t, d, err, pinstate.StatusLocked, 3, 0, true)
}

func TestConfigurationMismatchIsRejected(t *testing.T) {
	m, store := newMachine(t) // blank store defaults to three failures
	if _, err := m.Verify(req("init", 1, "wrong")); err != nil {
		t.Fatal(err)
	}

	_, err := pinstate.Open(store, pinstate.StaticPINVerifier(testCorrectPIN), pinstate.WithMaxFailures(5))
	if !errors.Is(err, pinstate.ErrConfigMismatch) {
		t.Fatalf("max mismatch error = %v, want ErrConfigMismatch", err)
	}

	// Same configuration opens successfully.
	if _, err := pinstate.Open(store, pinstate.StaticPINVerifier(testCorrectPIN), pinstate.WithMaxFailures(3)); err != nil {
		t.Fatalf("matching configuration error = %v", err)
	}
}
