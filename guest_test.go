package guesttimer_test

import (
	"math"
	"testing"
	"time"

	"guesttimer"
)

const (
	baseWall = int64(1_700_000_000_000_000_000) // arbitrary epoch nanos
	baseMono = int64(7_000_000_000_000)         // arbitrary monotonic nanos
)

// tickByID returns the tick for id, or nil if the timer did not fire.
func tickByID(ticks []guesttimer.Tick, id string) *guesttimer.Tick {
	for i := range ticks {
		if ticks[i].ID == id {
			return &ticks[i]
		}
	}
	return nil
}

func requireTick(t *testing.T, ticks []guesttimer.Tick, id string, wantCount, wantDeadline int64) {
	t.Helper()
	tk := tickByID(ticks, id)
	if tk == nil {
		t.Fatalf("timer %q did not fire; got ticks %+v", id, ticks)
	}
	if tk.Count != wantCount {
		t.Fatalf("timer %q Count = %d, want %d", id, tk.Count, wantCount)
	}
	if tk.Deadline != wantDeadline {
		t.Fatalf("timer %q Deadline = %d, want %d", id, tk.Deadline, wantDeadline)
	}
}

func requireNoTick(t *testing.T, ticks []guesttimer.Tick, id string) {
	t.Helper()
	if tk := tickByID(ticks, id); tk != nil {
		t.Fatalf("timer %q fired unexpectedly: %+v", id, *tk)
	}
}

func newGuestWithBoth(t *testing.T, period time.Duration) (*guesttimer.Guest, *guesttimer.ManualHost, int64) {
	t.Helper()
	h := guesttimer.NewManualHost(baseMono, baseWall)
	g := guesttimer.NewGuest(h)
	if err := g.AddRunTimer("run", period); err != nil {
		t.Fatalf("AddRunTimer: %v", err)
	}
	if err := g.AddCalendarTimer("cal", period); err != nil {
		t.Fatalf("AddCalendarTimer: %v", err)
	}
	return g, h, baseWall + int64(period)
}

// --- acceptable path -------------------------------------------------------

func TestTimersFollowSeparateClocks(t *testing.T) {
	period := 10 * time.Millisecond
	g, h, firstDeadline := newGuestWithBoth(t, period)

	// 15 ms of host time: both clocks advance, both timers cross exactly one
	// period, and the next deadline stays on the original phase.
	ticks, err := g.AdvanceHost(15 * time.Millisecond)
	if err != nil {
		t.Fatalf("AdvanceHost: %v", err)
	}
	requireTick(t, ticks, "run", 1, 10_000_000)
	requireTick(t, ticks, "cal", 1, firstDeadline)

	if got := g.RunNanos(); got != 15_000_000 {
		t.Fatalf("RunNanos = %d, want 15000000", got)
	}
	if got := g.WallNanos(); got != baseWall+15_000_000 {
		t.Fatalf("WallNanos = %d, want %d", got, baseWall+15_000_000)
	}

	// Next deadlines are one original period after the reported ones.
	for _, ts := range g.Timers() {
		switch ts.ID {
		case "run":
			if ts.Next != 20_000_000 {
				t.Fatalf("run next = %d, want 20000000", ts.Next)
			}
		case "cal":
			if ts.Next != baseWall+20_000_000 {
				t.Fatalf("cal next = %d, want %d", ts.Next, baseWall+20_000_000)
			}
		}
	}
	_ = h
}

func TestOnTickCallbackReceivesEachExpiry(t *testing.T) {
	g, _, _ := newGuestWithBoth(t, 10*time.Millisecond)

	var got []guesttimer.Tick
	g.OnTick(func(tk guesttimer.Tick) { got = append(got, tk) })

	if _, err := g.AdvanceHost(25 * time.Millisecond); err != nil {
		t.Fatalf("AdvanceHost: %v", err)
	}
	// 25 ms crosses deadlines at 10 ms and 20 ms: one coalesced tick, Count 2.
	if len(got) != 2 {
		t.Fatalf("callback fired %d times, want 2: %+v", len(got), got)
	}
	requireTick(t, got, "run", 2, 10_000_000)
	requireTick(t, got, "cal", 2, baseWall+10_000_000)
}

// --- pause / resume --------------------------------------------------------

func TestPauseFreezesRunClockButCalendarKeepsMoving(t *testing.T) {
	g, h, _ := newGuestWithBoth(t, 10*time.Millisecond)

	if _, err := g.AdvanceHost(5 * time.Millisecond); err != nil {
		t.Fatalf("AdvanceHost: %v", err)
	}
	g.Pause()

	// 100 ms of host time passes while the guest is paused.
	h.Advance(100 * time.Millisecond)
	ticks := g.Poll()

	// The run clock is frozen at 5 ms and cannot cross its 10 ms deadline.
	requireNoTick(t, ticks, "run")
	if got := g.RunNanos(); got != 5_000_000 {
		t.Fatalf("paused RunNanos = %d, want 5000000", got)
	}
	// The calendar clock keeps following host wall time and reports the
	// expiries that happened during the pause, coalesced into one tick whose
	// deadline is the first missed one.
	requireTick(t, ticks, "cal", 10, baseWall+10_000_000)

	if g.Running() {
		t.Fatalf("guest reports running while paused")
	}
}

func TestResumeCoalescesMissedCalendarExpiriesAndKeepsPhase(t *testing.T) {
	period := 10 * time.Millisecond
	g, h, first := newGuestWithBoth(t, period)

	if _, err := g.AdvanceHost(3 * time.Millisecond); err != nil {
		t.Fatalf("AdvanceHost: %v", err)
	}
	g.Pause()

	// Jump the host wall forward by 46 ms while paused (monotonic also moves,
	// but that must not be charged to the run clock).
	h.Advance(46 * time.Millisecond)

	ticks := g.Resume()
	// Deadlines at 10,20,30,40 ms of the calendar clock were crossed.
	requireTick(t, ticks, "cal", 4, first)
	requireNoTick(t, ticks, "run")

	// Phase preserved: next calendar deadline is 50 ms into the original
	// timeline, not "one period after now" (which would be 60 ms).
	for _, ts := range g.Timers() {
		if ts.ID == "cal" && ts.Next != baseWall+50_000_000 {
			t.Fatalf("cal next = %d, want %d", ts.Next, baseWall+50_000_000)
		}
		if ts.ID == "run" && ts.Next != 10_000_000 {
			t.Fatalf("run next = %d, want 10000000", ts.Next)
		}
	}

	// Run clock restarted from where it was, not from the resume moment.
	if got := g.RunNanos(); got != 3_000_000 {
		t.Fatalf("run clock after resume = %d, want 3000000", got)
	}

	// The very next 7 ms of running time must hit the original run deadline.
	ticks, err := g.AdvanceHost(7 * time.Millisecond)
	if err != nil {
		t.Fatalf("AdvanceHost: %v", err)
	}
	requireTick(t, ticks, "run", 1, 10_000_000)
}

func TestResumeWhileRunningIsNoOp(t *testing.T) {
	g, _, _ := newGuestWithBoth(t, 10*time.Millisecond)
	if ticks := g.Resume(); len(ticks) != 0 {
		t.Fatalf("Resume on running guest returned %+v", ticks)
	}
	if !g.Running() {
		t.Fatalf("guest stopped running after no-op Resume")
	}
}

// --- backward wall clock ---------------------------------------------------

func TestReportedCalendarTicksAreNotRepeatedAfterBackwardJump(t *testing.T) {
	period := 10 * time.Millisecond
	g, h, _ := newGuestWithBoth(t, period)

	// Report the 10 ms and 20 ms calendar expiries.
	ticks := g.Poll()
	requireNoTick(t, ticks, "cal")
	h.Advance(25 * time.Millisecond)
	ticks = g.Poll()
	requireTick(t, ticks, "cal", 2, baseWall+10_000_000)

	// Host wall clock is corrected backwards by 20 ms; now reads 5 ms.
	h.JumpWall(-20 * time.Millisecond)

	// The already-reported ticks must not be emitted again.
	ticks = g.Poll()
	requireNoTick(t, ticks, "cal")
	requireNoTick(t, ticks, "run")

	// Wall corrections do not disturb the run clock.
	if got := g.RunNanos(); got != 25_000_000 {
		t.Fatalf("RunNanos after wall jump = %d, want 25000000", got)
	}
	if got := g.WallNanos(); got != baseWall+5_000_000 {
		t.Fatalf("WallNanos after jump = %d, want %d", got, baseWall+5_000_000)
	}

	// Creeping back up to the last reported boundary must not re-fire; only
	// crossing the next unreported deadline (30 ms) does.
	h.Advance(24 * time.Millisecond) // wall now 29 ms
	ticks = g.Poll()
	requireNoTick(t, ticks, "cal")

	h.Advance(1 * time.Millisecond) // wall now exactly 30 ms
	ticks = g.Poll()
	requireTick(t, ticks, "cal", 1, baseWall+30_000_000)
}

func TestBackwardWallJumpDuringPauseDoesNotRefireOnResume(t *testing.T) {
	g, h, _ := newGuestWithBoth(t, 10*time.Millisecond)

	h.Advance(15 * time.Millisecond)
	ticks := g.Poll()
	requireTick(t, ticks, "cal", 1, baseWall+10_000_000)

	g.Pause()
	h.JumpWall(-30 * time.Millisecond) // wall now 15-30 = -15 ms relative to base
	ticks = g.Resume()
	requireNoTick(t, ticks, "cal")

	if got := g.WallNanos(); got != baseWall-15_000_000 {
		t.Fatalf("WallNanos = %d, want %d", got, baseWall-15_000_000)
	}
}

// --- migration -------------------------------------------------------------

func TestMigrateToHostWithDifferentMonotonicOrigin(t *testing.T) {
	period := 10 * time.Millisecond
	src := guesttimer.NewManualHost(baseMono, baseWall)
	g := guesttimer.NewGuest(src)
	if err := g.AddRunTimer("run", period); err != nil {
		t.Fatal(err)
	}
	if err := g.AddCalendarTimer("cal", period); err != nil {
		t.Fatal(err)
	}

	src.Advance(15 * time.Millisecond)
	g.Poll()

	// Destination host has a wildly different monotonic origin and an
	// unrelated wall clock offset.
	dst := guesttimer.NewManualHost(-42_000_000_000, 123_456_789)
	migrated, err := g.Migrate(dst)
	if err != nil {
		t.Fatalf("Migrate: %v", err)
	}

	// Guest clocks are carried over from guest state, not from dst's origin.
	if got := migrated.RunNanos(); got != 15_000_000 {
		t.Fatalf("migrated RunNanos = %d, want 15000000", got)
	}
	if got := migrated.WallNanos(); got != baseWall+15_000_000 {
		t.Fatalf("migrated WallNanos = %d, want %d", got, baseWall+15_000_000)
	}

	// Next deadlines survive migration unchanged.
	for _, ts := range migrated.Timers() {
		switch ts.ID {
		case "run":
			if ts.Next != 20_000_000 {
				t.Fatalf("run next = %d, want 20000000", ts.Next)
			}
		case "cal":
			if ts.Next != baseWall+20_000_000 {
				t.Fatalf("cal next = %d, want %d", ts.Next, baseWall+20_000_000)
			}
		}
	}

	// 5 ms more on the destination host reaches both original deadlines; the
	// destination monotonic value itself (a negative nanosecond count) must not
	// be mistaken for elapsed guest time.
	ticks, err := migrated.AdvanceHost(5 * time.Millisecond)
	if err != nil {
		t.Fatalf("AdvanceHost: %v", err)
	}
	requireTick(t, ticks, "run", 1, 20_000_000)
	requireTick(t, ticks, "cal", 1, baseWall+20_000_000)
}

func TestSaveRestorePreservesPausedState(t *testing.T) {
	g, h, _ := newGuestWithBoth(t, 10*time.Millisecond)
	h.Advance(4 * time.Millisecond)
	g.Poll()
	g.Pause()

	st := g.Save()
	if !st.Paused {
		t.Fatalf("saved state not marked paused")
	}

	dst := guesttimer.NewManualHost(999, 111)
	restored, err := guesttimer.NewGuestFromState(dst, st)
	if err != nil {
		t.Fatalf("NewGuestFromState: %v", err)
	}
	if restored.Running() {
		t.Fatalf("restored guest should still be paused")
	}

	// While paused the run clock stays frozen across destination host movement.
	// No poll happens here, so the accumulated calendar expiries are still
	// pending when the guest resumes.
	dst.Advance(50 * time.Millisecond)
	if got := restored.RunNanos(); got != 4_000_000 {
		t.Fatalf("paused restored RunNanos = %d, want 4000000", got)
	}

	// Resume delivers only the calendar expiries that accumulated while paused.
	ticks := restored.Resume()
	requireTick(t, ticks, "cal", 5, baseWall+10_000_000)
	requireNoTick(t, ticks, "run")
}

func TestMigrateSourceIsUnchanged(t *testing.T) {
	g, h, _ := newGuestWithBoth(t, 10*time.Millisecond)
	h.Advance(5 * time.Millisecond)
	g.Poll()

	before := g.Save()
	if _, err := g.Migrate(guesttimer.NewManualHost(0, 0)); err != nil {
		t.Fatalf("Migrate: %v", err)
	}
	after := g.Save()
	if before.RunNanos != after.RunNanos || before.WallNanos != after.WallNanos || before.Paused != after.Paused {
		t.Fatalf("migration mutated source: before %+v after %+v", before, after)
	}
}

func TestRestoreDoesNotAdoptDestinationWallOffset(t *testing.T) {
	g, _, _ := newGuestWithBoth(t, 10*time.Millisecond)
	if _, err := g.AdvanceHost(4 * time.Millisecond); err != nil {
		t.Fatal(err)
	}
	g.Poll() // reports nothing; both deadlines are still ahead

	st := g.Save()

	// Destination wall clock is one hour ahead of the source. Restoring must
	// keep the guest calendar at the saved value and not treat the offset as
	// elapsed guest time.
	dst := guesttimer.NewManualHost(88, baseWall+int64(time.Hour))
	restored, err := guesttimer.NewGuestFromState(dst, st)
	if err != nil {
		t.Fatalf("NewGuestFromState: %v", err)
	}
	ticks := restored.Poll()
	if len(ticks) != 0 {
		t.Fatalf("restore fired timers from destination wall offset: %+v", ticks)
	}
	if got := restored.WallNanos(); got != baseWall+4_000_000 {
		t.Fatalf("restored WallNanos = %d, want %d", got, baseWall+4_000_000)
	}

	// Only 6 ms of real elapsed destination time remain before the next
	// calendar deadline in guest state.
	ticks, err = restored.AdvanceHost(6 * time.Millisecond)
	if err != nil {
		t.Fatalf("AdvanceHost: %v", err)
	}
	requireTick(t, ticks, "cal", 1, baseWall+10_000_000)
	requireTick(t, ticks, "run", 1, 10_000_000)
}

// --- rejected / edge inputs ------------------------------------------------

func TestTimerAddedWhilePausedStartsFromPausedClock(t *testing.T) {
	g, h, _ := newGuestWithBoth(t, 10*time.Millisecond)
	h.Advance(5 * time.Millisecond)
	g.Pause()

	h.Advance(100 * time.Millisecond)

	// A run timer registered while paused begins one period after the frozen
	// run reading (5 ms), so it is due at 15 ms of run time, not immediately.
	if err := g.AddRunTimer("late-run", 10*time.Millisecond); err != nil {
		t.Fatalf("AddRunTimer: %v", err)
	}
	ticks := g.Poll()
	requireNoTick(t, ticks, "late-run")

	// Resuming does not invent run time for the paused interval either.
	ticks = g.Resume()
	requireNoTick(t, ticks, "late-run")
	requireNoTick(t, ticks, "run")
}

func TestBackwardJumpLargerThanAllHistoryDoesNotRefire(t *testing.T) {
	g, h, _ := newGuestWithBoth(t, 10*time.Millisecond)

	h.Advance(35 * time.Millisecond)
	ticks := g.Poll()
	requireTick(t, ticks, "cal", 3, baseWall+10_000_000)

	// Rewind the calendar clock before the moment the guest was created.
	const reportedAt = baseWall + 35_000_000
	h.JumpWall(time.Duration(1 - reportedAt))
	if got := g.WallNanos(); got != 1 {
		t.Fatalf("WallNanos = %d, want 1", got)
	}
	ticks = g.Poll()
	requireNoTick(t, ticks, "cal")

	// Advancing back to the last reported boundary is still silent; only the
	// next unreported deadline fires, exactly once.
	h.JumpWall(time.Duration(baseWall + 40_000_000 - 2)) // wall now 40 ms - 1 ns
	ticks = g.Poll()
	requireNoTick(t, ticks, "cal")
	h.JumpWall(1)
	ticks = g.Poll()
	requireTick(t, ticks, "cal", 1, baseWall+40_000_000)
}

func TestRejectsInvalidTimerRegistration(t *testing.T) {
	g, _, _ := newGuestWithBoth(t, 10*time.Millisecond)

	if err := g.AddTimer("", guesttimer.KindRun, time.Second); err == nil {
		t.Fatalf("empty id accepted")
	}
	if err := g.AddTimer("neg", guesttimer.KindRun, -time.Second); err == nil {
		t.Fatalf("negative period accepted")
	}
	if err := g.AddTimer("zero", guesttimer.KindRun, 0); err == nil {
		t.Fatalf("zero period accepted")
	}
	if err := g.AddTimer("badkind", guesttimer.Kind(9), time.Second); err == nil {
		t.Fatalf("invalid kind accepted")
	}
	if err := g.AddRunTimer("run", time.Second); err == nil {
		t.Fatalf("duplicate id accepted")
	}
}

func TestRejectsInvalidRestoreState(t *testing.T) {
	h := guesttimer.NewManualHost(0, 0)
	cases := []struct {
		name string
		st   guesttimer.GuestState
	}{
		{"zero period", guesttimer.GuestState{TimerStates: []guesttimer.TimerState{{ID: "a", Kind: guesttimer.KindRun, Period: 0}}}},
		{"negative period", guesttimer.GuestState{TimerStates: []guesttimer.TimerState{{ID: "a", Kind: guesttimer.KindRun, Period: -1}}}},
		{"bad kind", guesttimer.GuestState{TimerStates: []guesttimer.TimerState{{ID: "a", Kind: guesttimer.Kind(3), Period: 1}}}},
		{"duplicate id", guesttimer.GuestState{TimerStates: []guesttimer.TimerState{
			{ID: "a", Kind: guesttimer.KindRun, Period: 1},
			{ID: "a", Kind: guesttimer.KindCalendar, Period: 1},
		}}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := guesttimer.NewGuestFromState(h, tc.st); err == nil {
				t.Fatalf("invalid state accepted: %+v", tc.st)
			}
		})
	}
}

func TestAdvanceRejectsNonAdvanceableHost(t *testing.T) {
	g := guesttimer.NewGuest(guesttimer.SystemHost{})
	if _, err := g.AdvanceHost(time.Millisecond); err == nil {
		t.Fatalf("AdvanceHost accepted a non-advanceable host")
	}
}

func TestManualHostIgnoresBackwardMonotonicAdvance(t *testing.T) {
	h := guesttimer.NewManualHost(100, 200)
	h.Advance(-50 * time.Nanosecond)
	if h.Monotonic() != 100 || h.Wall() != 200 {
		t.Fatalf("Advance with negative duration changed clocks: mono=%d wall=%d", h.Monotonic(), h.Wall())
	}
	if math.Abs(float64(h.Monotonic())-100) > 0 {
		t.Fatalf("unexpected monotonic drift")
	}
}

func TestZeroPeriodTimerPanicGuard(t *testing.T) {
	// Save/restore round trip of a guest with no timers must not panic and must
	// preserve both guest clocks.
	h := guesttimer.NewManualHost(10, 20)
	g := guesttimer.NewGuest(h)
	h.Advance(time.Second)

	st := g.Save()
	if st.RunNanos != int64(time.Second) || st.WallNanos != 20+int64(time.Second) {
		t.Fatalf("unexpected state %+v", st)
	}
	restored, err := guesttimer.NewGuestFromState(h, st)
	if err != nil {
		t.Fatalf("NewGuestFromState: %v", err)
	}
	if ticks := restored.Poll(); len(ticks) != 0 {
		t.Fatalf("timerless guest produced ticks: %+v", ticks)
	}
}
