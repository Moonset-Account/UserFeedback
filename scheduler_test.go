package scheduler

import (
	"testing"
)

// mustSubmit fails the test when a job is rejected.
func mustSubmit(t *testing.T, s *Scheduler, j Job) {
	t.Helper()
	if err := s.Submit(j); err != nil {
		t.Fatalf("Submit(%s) unexpected error: %v", j.ID, err)
	}
}

func assertResult(t *testing.T, s *Scheduler, id string, wantStart, wantFinish int64) {
	t.Helper()
	got, ok := s.ResultOf(id)
	if !ok {
		t.Fatalf("job %s has no recorded result", id)
	}
	if got.Start != wantStart || got.Finish != wantFinish {
		t.Fatalf("job %s = [start %d, finish %d], want [start %d, finish %d]",
			id, got.Start, got.Finish, wantStart, wantFinish)
	}
}

// TestNormalStartAndReservation covers the headline flow: a head job blocked
// for slots gets a reservation derived from the latest required completion,
// and a short later job backfills into the free slots.
func TestNormalStartAndReservation(t *testing.T) {
	// 10 slots.
	s, err := New(10)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	mustSubmit(t, s, Job{ID: "A", Arrival: 0, Slots: 6, Runtime: 10})
	mustSubmit(t, s, Job{ID: "B", Arrival: 0, Slots: 6, Runtime: 10})
	mustSubmit(t, s, Job{ID: "C", Arrival: 0, Slots: 4, Runtime: 3})

	if _, ok := s.Step(); !ok {
		t.Fatal("first Step reported no event")
	}
	if s.Now() != 0 {
		t.Fatalf("Now = %d, want 0", s.Now())
	}
	// A starts (6 slots); B is head and waits; C backfills with the other 4.
	if s.RunningCount() != 2 {
		t.Fatalf("RunningCount = %d, want 2", s.RunningCount())
	}
	if id, at, ok := s.Reservation(); !ok || id != "B" || at != 10 {
		t.Fatalf("Reservation = (%q,%d,%v), want (B,10,true)", id, at, ok)
	}

	// C finishes at 3 and its slots are returned. B still cannot fit.
	if _, ok := s.Step(); !ok {
		t.Fatal("second Step reported no event")
	}
	if s.Now() != 3 || s.RunningCount() != 1 {
		t.Fatalf("at time 3: now=%d running=%d", s.Now(), s.RunningCount())
	}
	assertResult(t, s, "C", 0, 3)
	if id, at, ok := s.Reservation(); !ok || id != "B" || at != 10 {
		t.Fatalf("Reservation after C finished = (%q,%d,%v), want (B,10,true)", id, at, ok)
	}

	s.Run()
	assertResult(t, s, "A", 0, 10)
	assertResult(t, s, "B", 10, 20)
	assertResult(t, s, "C", 0, 3)
	if !s.Idle() {
		t.Fatal("scheduler should be idle after Run")
	}
	if len(s.Results()) != 3 {
		t.Fatalf("Results len = %d, want 3", len(s.Results()))
	}
}

// TestReservationIsLatestRequiredEnd checks that when several running jobs must
// finish before enough slots accumulate, the reservation is the *latest* end
// time among those jobs (not simply the earliest finish).
func TestReservationIsLatestRequiredEnd(t *testing.T) {
	// 10 slots: jobs using 4, 3, 3 slots ending at 5, 8, 12.
	s, _ := New(10)
	mustSubmit(t, s, Job{ID: "p4", Arrival: 0, Slots: 4, Runtime: 5})
	mustSubmit(t, s, Job{ID: "p3a", Arrival: 0, Slots: 3, Runtime: 8})
	mustSubmit(t, s, Job{ID: "p3b", Arrival: 0, Slots: 3, Runtime: 12})
	mustSubmit(t, s, Job{ID: "H", Arrival: 1, Slots: 7, Runtime: 2})

	s.Step() // t=0: p4, p3a, p3b start
	if s.RunningCount() != 3 {
		t.Fatalf("RunningCount = %d, want 3", s.RunningCount())
	}
	s.Step() // t=1: H arrives and becomes waiting head.
	// Free = 0; H needs 7: p4 (4) ends 5, then p3a (3) ends 8 -> reservation 8.
	if id, at, ok := s.Reservation(); !ok || id != "H" || at != 8 {
		t.Fatalf("Reservation = (%q,%d,%v), want (H,8,true)", id, at, ok)
	}

	s.Run()
	// H starts at 8 once p4 and p3a have released 7 slots.
	assertResult(t, s, "H", 8, 10)
	assertResult(t, s, "p4", 0, 5)
	assertResult(t, s, "p3a", 0, 8)
	assertResult(t, s, "p3b", 0, 12)
}

// TestBackfillCrossingReservationAllowed is the rule called out in the brief:
// a later long job may cross the reservation time as long as the other slots
// freeing up by then still satisfy the head.
func TestBackfillCrossingReservationAllowed(t *testing.T) {
	// 10 slots.
	s, _ := New(10)
	mustSubmit(t, s, Job{ID: "A", Arrival: 0, Slots: 7, Runtime: 10})
	mustSubmit(t, s, Job{ID: "H", Arrival: 1, Slots: 7, Runtime: 2})
	// B needs the 3 free slots and runs past H's reservation (10).
	mustSubmit(t, s, Job{ID: "B", Arrival: 1, Slots: 3, Runtime: 20})

	s.Step() // t=0: A starts, 3 free
	s.Step() // t=1: H waits (reservation 10); B must be allowed to backfill.

	if s.RunningCount() != 2 {
		t.Fatalf("RunningCount = %d, want 2 (A and backfilled B)", s.RunningCount())
	}
	if id, at, ok := s.Reservation(); !ok || id != "H" || at != 10 {
		t.Fatalf("Reservation = (%q,%d,%v), want (H,10,true)", id, at, ok)
	}

	s.Run()
	assertResult(t, s, "H", 10, 12) // A's 7 slots are enough for H
	assertResult(t, s, "B", 1, 21)  // B crosses t=10 undisturbed
	assertResult(t, s, "A", 0, 10)
}

// TestBackfillCrossingReservationDenied is the symmetric boundary case:
// crossing the reservation is rejected precisely when the slots freeing up at
// the reservation would no longer cover the head.
func TestBackfillCrossingReservationDenied(t *testing.T) {
	// 10 slots.
	s, _ := New(10)
	mustSubmit(t, s, Job{ID: "A", Arrival: 0, Slots: 6, Runtime: 10})
	mustSubmit(t, s, Job{ID: "H", Arrival: 1, Slots: 8, Runtime: 2})
	// B wants 4 of the 4 free slots and runs past 10. At 10 only A's 6 slots
	// return, so H would have just 6 < 8: B must be refused.
	mustSubmit(t, s, Job{ID: "B", Arrival: 1, Slots: 4, Runtime: 20})

	s.Step() // t=0: A starts
	s.Step() // t=1: H waits, B is rejected

	if s.RunningCount() != 1 {
		t.Fatalf("RunningCount = %d, want 1 (only A)", s.RunningCount())
	}
	if s.PendingCount() != 2 {
		t.Fatalf("PendingCount = %d, want 2 (H and B still queued)", s.PendingCount())
	}
	if id, at, ok := s.Reservation(); !ok || id != "H" || at != 10 {
		t.Fatalf("Reservation = (%q,%d,%v), want (H,10,true)", id, at, ok)
	}

	s.Run()
	// At 10 A finishes; H (8 slots) starts, B still waits.
	assertResult(t, s, "H", 10, 12)
	// B starts at 12 and takes 4 of the 10 free slots.
	assertResult(t, s, "B", 12, 32)
}

// TestEarlyFinishAdvancesHead verifies that an actual runtime shorter than the
// upper bound lets the head start earlier, because the reservation is
// recomputed from jobs actually still running.
func TestEarlyFinishAdvancesHead(t *testing.T) {
	// 10 slots. A estimates 10 but really finishes at 4.
	s, _ := New(10)
	mustSubmit(t, s, Job{ID: "A", Arrival: 0, Slots: 7, Runtime: 10, ActualRuntime: 4})
	mustSubmit(t, s, Job{ID: "H", Arrival: 0, Slots: 7, Runtime: 3})

	s.Step() // t=0: A starts; H waits with reservation 10 (based on the bound)
	if id, at, ok := s.Reservation(); !ok || id != "H" || at != 10 {
		t.Fatalf("Reservation = (%q,%d,%v), want (H,10,true)", id, at, ok)
	}
	s.Step() // t=4: A actually finishes; H starts immediately.
	if s.Now() != 4 {
		t.Fatalf("Now = %d, want 4", s.Now())
	}
	if _, _, ok := s.Reservation(); ok {
		t.Fatal("reservation should clear once the head starts early")
	}

	s.Run()
	assertResult(t, s, "A", 0, 4)
	assertResult(t, s, "H", 4, 7)
}

// TestSameTimestampReturnThenSchedule ensures all completions at a time release
// their slots before any job is dispatched at that same time.
func TestSameTimestampReturnThenSchedule(t *testing.T) {
	// 10 slots. Two 5-slot jobs finish together at 6 and a waiting 10-slot job
	// must start exactly at 6.
	s, _ := New(10)
	mustSubmit(t, s, Job{ID: "X", Arrival: 0, Slots: 5, Runtime: 6})
	mustSubmit(t, s, Job{ID: "Y", Arrival: 0, Slots: 5, Runtime: 6})
	mustSubmit(t, s, Job{ID: "Big", Arrival: 1, Slots: 10, Runtime: 4})

	s.Step() // t=0: X, Y start
	s.Step() // t=1: Big arrives and waits
	s.Step() // t=6: both finish first, then Big starts.

	assertResult(t, s, "X", 0, 6)
	assertResult(t, s, "Y", 0, 6)
	if s.RunningCount() != 1 || s.FreeSlots() != 0 {
		t.Fatalf("at t=6 want Big alone running with 0 free slots, got running=%d free=%d",
			s.RunningCount(), s.FreeSlots())
	}
	s.Run()
	assertResult(t, s, "Big", 6, 10)
}

// TestBackfillEndingExactlyAtReservation is a boundary test: a backfill whose
// estimated end equals the reservation releases its slots in time and is
// allowed; one time unit longer is rejected.
func TestBackfillEndingExactlyAtReservation(t *testing.T) {
	// A takes 6 until 10. H needs 8, so at 10 only A's 6 return and the
	// backfill's slots must also be free.
	s, _ := New(10)
	mustSubmit(t, s, Job{ID: "A", Arrival: 0, Slots: 6, Runtime: 10})
	mustSubmit(t, s, Job{ID: "H", Arrival: 1, Slots: 8, Runtime: 2})
	mustSubmit(t, s, Job{ID: "Fit", Arrival: 1, Slots: 4, Runtime: 9}) // 1 -> 10
	mustSubmit(t, s, Job{ID: "TooLong", Arrival: 1, Slots: 1, Runtime: 10})

	s.Step() // t=0
	s.Step() // t=1
	// Fit ends exactly at 10: allowed. TooLong ends at 11: rejected.
	if s.RunningCount() != 2 {
		t.Fatalf("RunningCount = %d, want 2 (A and Fit)", s.RunningCount())
	}
	if s.PendingCount() != 2 {
		t.Fatalf("PendingCount = %d, want 2 (H and TooLong)", s.PendingCount())
	}

	s.Run()
	assertResult(t, s, "Fit", 1, 10)
	assertResult(t, s, "H", 10, 12)
	// At t=10 H takes 8 slots; 2 remain, so the 1-slot TooLong starts at once.
	assertResult(t, s, "TooLong", 10, 20)
}

// TestArrivalWhileHeadWaits interleaves a new arrival into an established wait.
// The newcomer joins the backfill queue without disturbing the head's
// reservation, and a zero-free-slot arrival simply waits.
func TestArrivalWhileHeadWaits(t *testing.T) {
	// 10 slots fully occupied by A; H waits; late L arrives at 5 and also
	// waits because no slots are free.
	s, _ := New(10)
	mustSubmit(t, s, Job{ID: "A", Arrival: 0, Slots: 10, Runtime: 10})
	mustSubmit(t, s, Job{ID: "H", Arrival: 0, Slots: 10, Runtime: 2})
	mustSubmit(t, s, Job{ID: "L", Arrival: 5, Slots: 3, Runtime: 2})

	s.Step() // t=0: A starts, H waits, reservation 10
	if id, at, ok := s.Reservation(); !ok || id != "H" || at != 10 {
		t.Fatalf("Reservation = (%q,%d,%v), want (H,10,true)", id, at, ok)
	}
	s.Step() // t=5: L arrives; nothing is free so it stays queued.
	if s.PendingCount() != 2 || s.RunningCount() != 1 {
		t.Fatalf("at t=5 pending=%d running=%d, want 2 and 1", s.PendingCount(), s.RunningCount())
	}

	s.Run()
	assertResult(t, s, "A", 0, 10)
	assertResult(t, s, "H", 10, 12) // head order preserved ahead of L
	assertResult(t, s, "L", 12, 14)
}

// TestBackfillQueueOrder checks that backfills are attempted in queue order and
// a job that cannot fit is skipped while a later, smaller job still backfills.
func TestBackfillQueueOrder(t *testing.T) {
	// 10 slots: A holds 6, 4 free. H waits (needs 6).
	s, _ := New(10)
	mustSubmit(t, s, Job{ID: "A", Arrival: 0, Slots: 6, Runtime: 10})
	mustSubmit(t, s, Job{ID: "H", Arrival: 1, Slots: 6, Runtime: 2})
	mustSubmit(t, s, Job{ID: "Big", Arrival: 1, Slots: 5, Runtime: 2})   // cannot fit in 4
	mustSubmit(t, s, Job{ID: "Small", Arrival: 1, Slots: 3, Runtime: 2}) // fits and safe

	s.Step() // t=0
	s.Step() // t=1
	if s.RunningCount() != 2 {
		t.Fatalf("RunningCount = %d, want 2 (A and Small)", s.RunningCount())
	}
	if s.PendingCount() != 2 {
		t.Fatalf("PendingCount = %d, want 2 (H and Big)", s.PendingCount())
	}

	s.Run()
	// Small ends at 3; A ends at 10; H starts at 10; Big after H.
	assertResult(t, s, "Small", 1, 3)
	assertResult(t, s, "H", 10, 12)
	assertResult(t, s, "Big", 12, 14)
}

// TestWholeClusterJobBoundary is the boundary where one job consumes every
// slot; no backfill can coexist with it.
func TestWholeClusterJobBoundary(t *testing.T) {
	s, _ := New(4)
	mustSubmit(t, s, Job{ID: "Full", Arrival: 0, Slots: 4, Runtime: 5})
	mustSubmit(t, s, Job{ID: "Next", Arrival: 0, Slots: 1, Runtime: 1})

	s.Step()
	if s.FreeSlots() != 0 || s.RunningCount() != 1 {
		t.Fatalf("want Full alone using all slots, got free=%d running=%d",
			s.FreeSlots(), s.RunningCount())
	}
	if id, at, ok := s.Reservation(); !ok || id != "Next" || at != 5 {
		t.Fatalf("Reservation = (%q,%d,%v), want (Next,5,true)", id, at, ok)
	}
	s.Run()
	assertResult(t, s, "Full", 0, 5)
	assertResult(t, s, "Next", 5, 6)
}

// TestValidation exercises the input boundary checks.
func TestValidation(t *testing.T) {
	if _, err := New(0); err == nil {
		t.Fatal("New(0) should fail")
	}
	s, err := New(5)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	cases := []Job{
		{ID: "", Arrival: 0, Slots: 1, Runtime: 1},
		{ID: "zero-slot", Arrival: 0, Slots: 0, Runtime: 1},
		{ID: "too-big", Arrival: 0, Slots: 6, Runtime: 1},
		{ID: "bad-runtime", Arrival: 0, Slots: 1, Runtime: 0},
		{ID: "bad-arrival", Arrival: -1, Slots: 1, Runtime: 1},
		{ID: "over-bound", Arrival: 0, Slots: 1, Runtime: 2, ActualRuntime: 3},
	}
	for _, j := range cases {
		if err := s.Submit(j); err == nil {
			t.Fatalf("Submit(%+v) should have failed", j)
		}
	}
	mustSubmit(t, s, Job{ID: "dup", Arrival: 0, Slots: 1, Runtime: 1})
	if err := s.Submit(Job{ID: "dup", Arrival: 0, Slots: 1, Runtime: 1}); err == nil {
		t.Fatal("duplicate job id should be rejected")
	}
}

// TestStepStopsWhenDrained checks the clock cannot advance past the last event
// and Results stays stable afterwards.
func TestStepStopsWhenDrained(t *testing.T) {
	s, _ := New(2)
	mustSubmit(t, s, Job{ID: "J", Arrival: 3, Slots: 2, Runtime: 4})

	if _, ok := s.Step(); !ok || s.Now() != 3 {
		t.Fatalf("first Step: now=%d", s.Now())
	}
	if _, ok := s.Step(); !ok || s.Now() != 7 {
		t.Fatalf("second Step: now=%d, want 7", s.Now())
	}
	if _, ok := s.Step(); ok {
		t.Fatal("Step after drain should report no event")
	}
	if !s.Idle() {
		t.Fatal("scheduler should be idle")
	}
	assertResult(t, s, "J", 3, 7)
}
