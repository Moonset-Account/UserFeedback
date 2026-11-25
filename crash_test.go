package pinstate_test

import (
	"errors"
	"strconv"
	"sync"
	"testing"

	"pinstate"
)

var errSimulatedPowerCut = errors.New("simulated power cut")

// boundaryStore cuts power at a count of completed dual-page writes.
//
// boundary=0 means cut immediately before the first page write, without
// persisting it. boundary=N>0 means persist the Nth single-page write, then
// report power loss before RAM state can be considered reliable. The cut is
// one-shot for boundary 0; positive boundaries are reached only once by the
// corresponding write number.
type boundaryStore struct {
	*pinstate.MemoryPageStore

	mu       sync.Mutex
	writes   int
	boundary int
	before   bool
	pages    []int
}

func newBoundaryStore(boundary int) *boundaryStore {
	return &boundaryStore{
		MemoryPageStore: pinstate.NewMemoryPageStore(),
		boundary:        boundary,
	}
}

func (s *boundaryStore) WritePage(pageID int, data []byte) error {
	s.mu.Lock()
	if s.boundary == 0 && !s.before {
		s.before = true
		s.mu.Unlock()
		return errSimulatedPowerCut
	}
	nextWrite := s.writes + 1
	s.mu.Unlock()

	if err := s.MemoryPageStore.WritePage(pageID, data); err != nil {
		return err
	}

	s.mu.Lock()
	s.writes = nextWrite
	s.pages = append(s.pages, pageID)
	cutAfter := s.writes == s.boundary
	s.mu.Unlock()

	if cutAfter {
		return errSimulatedPowerCut
	}
	return nil
}

func (s *boundaryStore) writePages() []int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]int(nil), s.pages...)
}

func openCrashMachine(t *testing.T, store pinstate.PageReadWriter) *pinstate.StateMachine {
	t.Helper()
	m, err := pinstate.Open(store, pinstate.StaticPINVerifier(testCorrectPIN))
	if err != nil {
		t.Fatalf("restart Open() error = %v", err)
	}
	return m
}

func TestAllSinglePageWriteBoundariesLastWrongThenCorrect(t *testing.T) {
	// Four unique requests, each commits one checksummed A/B page snapshot.
	const totalWrites = 4

	for boundary := 0; boundary <= totalWrites; boundary++ {
		t.Run(boundaryName(boundary), func(t *testing.T) {
			store := newBoundaryStore(boundary)
			m := openCrashMachine(t, store)

			requests := []pinstate.Request{
				req("crash-session", 1, "wrong-pin-1"),
				req("crash-session", 2, "wrong-pin-2"),
				req("crash-session", 3, "wrong-pin-3"),  // last wrong attempt
				req("crash-session", 4, testCorrectPIN), // immediately following correct attempt
			}
			wantStatuses := []pinstate.Status{
				pinstate.StatusBadPIN,
				pinstate.StatusBadPIN,
				pinstate.StatusLocked,
				pinstate.StatusLocked,
			}
			wantFailures := []int{1, 2, 3, 3}

			var decisions []pinstate.Decision
			sawCut := false

			for requestIndex, request := range requests {
				for {
					decision, err := m.Verify(request)
					if errors.Is(err, errSimulatedPowerCut) {
						sawCut = true

						// Physically restart: discard the StateMachine/RAM and
						// recover only from persistent pages.
						m = openCrashMachine(t, store)
						status, statusErr := m.Status()
						if statusErr != nil {
							t.Fatalf("Status() during restart error = %v", statusErr)
						}

						// After b completed writes, the durable failure prefix is
						// 0,1,2,3,3 for b=0..4.
						wantDurableFailures := []int{0, 1, 2, 3, 3}[boundary]
						if status.Failures != wantDurableFailures {
							t.Fatalf("boundary %d: durable failures after cut = %d, want %d",
								boundary, status.Failures, wantDurableFailures)
						}
						if status.Locked != (boundary >= 3) {
							t.Fatalf("boundary %d: locked=%v, want %v",
								boundary, status.Locked, boundary >= 3)
						}
						continue
					}
					if err != nil {
						t.Fatalf("Verify(%+v) error = %v", request, err)
					}

					decisions = append(decisions, decision)
					if boundary > 0 && requestIndex == boundary-1 {
						if !decision.Replayed {
							t.Fatalf("boundary %d: request %d must be idempotent replay after restart",
								boundary, requestIndex+1)
						}
					} else if decision.Replayed {
						t.Fatalf("boundary %d: unexpected replay for request %d",
							boundary, requestIndex+1)
					}
					break
				}
			}

			if boundary == 0 && sawCut {
				// boundary 0 still exercises the pre-write power cut.
			}
			if !sawCut {
				t.Fatalf("boundary %d did not trigger a power cut", boundary)
			}
			if len(decisions) != len(requests) {
				t.Fatalf("got %d decisions, want %d", len(decisions), len(requests))
			}

			for i, decision := range decisions {
				if decision.Status != wantStatuses[i] || decision.Failures != wantFailures[i] {
					t.Fatalf("decision %d = (%s,%d), want (%s,%d); all=%+v",
						i+1, decision.Status, decision.Failures,
						wantStatuses[i], wantFailures[i], decisions)
				}
			}

			status, err := m.Status()
			if err != nil {
				t.Fatalf("final Status() error = %v", err)
			}
			if !status.Locked || status.Failures != 3 || status.AttemptsRemaining != 0 {
				t.Fatalf("card did not end locked with three failures: %+v", status)
			}
			if status.Generation != totalWrites {
				t.Fatalf("generation = %d, want %d", status.Generation, totalWrites)
			}
			if status.ActivePage != 1 {
				t.Fatalf("active page = %d, want page B (1) after four alternating writes", status.ActivePage)
			}
			if pages := store.writePages(); len(pages) != totalWrites {
				t.Fatalf("durable page writes = %v (%d), want %d", pages, len(pages), totalWrites)
			} else {
				wantPages := []int{0, 1, 0, 1}
				for i := range wantPages {
					if pages[i] != wantPages[i] {
						t.Fatalf("write page sequence = %v, want %v", pages, wantPages)
					}
				}
			}

			// A true process restart after completion still keeps lock state.
			m = openCrashMachine(t, store)

			// Switching to a new card session preserves card-level state.
			d, err := m.Verify(req("new-session-after-restart", 1, "another-wrong-pin"))
			requireDecision(t, d, err, pinstate.StatusLocked, 3, 0, true)

			d, err = m.Verify(req("new-session-after-restart", 2, testCorrectPIN))
			requireDecision(t, d, err, pinstate.StatusLocked, 3, 0, true)

			// Many later sessions/attempts cannot create a fourth failure count.
			for i := uint64(1); i <= 20; i++ {
				session := "bulk-session"
				d, err := m.Verify(req(session, i, "guess"))
				if err != nil {
					t.Fatalf("bulk Verify(%d) error = %v", i, err)
				}
				if d.Status != pinstate.StatusLocked || d.Failures != 3 {
					t.Fatalf("bulk attempt %d = %+v; quota was overused", i, d)
				}
			}

			finalStatus, err := m.Status()
			if err != nil {
				t.Fatalf("final reopened Status() error = %v", err)
			}
			if !finalStatus.Locked || finalStatus.Failures != 3 {
				t.Fatalf("final status = %+v, want locked with failures=3", finalStatus)
			}
		})
	}
}

func TestChangedContentAtEveryDurableAttemptBoundaryConflicts(t *testing.T) {
	requests := []pinstate.Request{
		req("conflict-boundary", 1, "wrong-pin-1"),
		req("conflict-boundary", 2, "wrong-pin-2"),
		req("conflict-boundary", 3, "wrong-pin-3"),
		req("conflict-boundary", 4, testCorrectPIN),
	}
	wantStatuses := []pinstate.Status{
		pinstate.StatusBadPIN,
		pinstate.StatusBadPIN,
		pinstate.StatusLocked,
		pinstate.StatusLocked,
	}

	for boundary := 1; boundary <= 4; boundary++ {
		t.Run(boundaryName(boundary), func(t *testing.T) {
			store := newBoundaryStore(boundary)
			m := openCrashMachine(t, store)

			var cutRequest pinstate.Request
			for _, request := range requests {
				_, err := m.Verify(request)
				if errors.Is(err, errSimulatedPowerCut) {
					cutRequest = request
					break
				}
				if err != nil {
					t.Fatalf("Verify() error = %v", err)
				}
			}
			if cutRequest.Attempt == 0 {
				t.Fatal("power cut did not occur")
			}

			m = openCrashMachine(t, store)

			changed := cutRequest
			changed.PIN = cutRequest.PIN + "-changed-content"
			conflict, err := m.Verify(changed)
			if !errors.Is(err, pinstate.ErrAttemptConflict) {
				t.Fatalf("changed reused attempt at boundary %d: err=%v decision=%+v", boundary, err, conflict)
			}

			replay, err := m.Verify(cutRequest)
			if err != nil {
				t.Fatalf("original request replay error = %v", err)
			}
			if !replay.Replayed || replay.Status != wantStatuses[boundary-1] {
				t.Fatalf("original replay = %+v, want status %s and Replayed=true", replay, wantStatuses[boundary-1])
			}
		})
	}
}

func TestDualPageRecoversValidPageAndFailsClosedWhenBothCorrupt(t *testing.T) {
	store := pinstate.NewMemoryPageStore()
	m := openCrashMachine(t, store)

	if _, err := m.Verify(req("s", 1, "wrong-1")); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Verify(req("s", 2, "wrong-2")); err != nil {
		t.Fatal(err)
	}

	// Page A has generation 1; page B has the newer generation 2.
	corruptOnePage(t, store, 0)

	recovered := openCrashMachine(t, store)
	status, err := recovered.Status()
	if err != nil {
		t.Fatalf("Status() after one corrupt page error = %v", err)
	}
	if status.Failures != 2 || status.Locked {
		t.Fatalf("recovery with one corrupt page gave %+v, want failures=2", status)
	}

	// Corrupt the only newer valid page. With no authentic state, opening must
	// fail closed rather than silently resetting the PIN counter.
	corruptOnePage(t, store, 1)
	if _, err := pinstate.Open(store, pinstate.StaticPINVerifier(testCorrectPIN)); !errors.Is(err, pinstate.ErrNoRecoverableState) {
		t.Fatalf("Open() with both pages corrupt error = %v, want ErrNoRecoverableState", err)
	}
}

func corruptOnePage(t *testing.T, store *pinstate.MemoryPageStore, pageID int) {
	t.Helper()
	raw, err := store.ReadPage(pageID)
	if err != nil {
		t.Fatalf("ReadPage(%d) error = %v", pageID, err)
	}
	if len(raw) < 8 {
		t.Fatalf("page %d is too short to corrupt: %d bytes", pageID, len(raw))
	}
	raw[len(raw)-4] ^= 0xFF
	if err := store.WritePage(pageID, raw); err != nil {
		t.Fatalf("corrupt WritePage(%d) error = %v", pageID, err)
	}
}

func boundaryName(boundary int) string {
	if boundary == 0 {
		return "before-first-page-write"
	}
	return "after-page-write-" + strconv.Itoa(boundary)
}
