package condmodel

import (
	"errors"
	"testing"
)

func mustOK(t *testing.T) func(Result, error) Result {
	t.Helper()
	return func(r Result, err error) Result {
		t.Helper()
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if !r.OK {
			t.Fatalf("result not OK: %+v", r)
		}
		return r
	}
}

func lockN(t *testing.T, s *Scheduler, id ID, n int) {
	t.Helper()
	for i := 0; i < n; i++ {
		r, err := s.Lock(id)
		if err != nil {
			t.Fatalf("Lock(%q) layer %d: %v", id, i+1, err)
		}
		want := StatusAcquired
		if i > 0 {
			want = StatusReentered
		}
		if r.Status != want {
			t.Fatalf("Lock(%q) layer %d status = %q, want %q", id, i+1, r.Status, want)
		}
		if r.Depth != i+1 {
			t.Fatalf("Lock(%q) depth = %d, want %d", id, r.Depth, i+1)
		}
	}
}

func unlockN(t *testing.T, s *Scheduler, id ID, n int) {
	t.Helper()
	for i := n; i > 0; i-- {
		r, err := s.Unlock(id)
		if err != nil {
			t.Fatalf("Unlock(%q) with %d layers remaining: %v", id, i, err)
		}
		if i > 1 {
			if r.Status != StatusStillHeld || r.Depth != i-1 {
				t.Fatalf("Unlock(%q) status=%q depth=%d, want still_held/%d", id, r.Status, r.Depth, i-1)
			}
		} else if r.Status != StatusReleased {
			t.Fatalf("final Unlock(%q) status = %q, want released", id, r.Status)
		}
	}
}

func requireQueueTasks(t *testing.T, snap Snapshot, want ...ID) {
	t.Helper()
	if len(snap.LockQueue) != len(want) {
		t.Fatalf("lock queue = %v, want tasks %v", snap.LockQueue, want)
	}
	for i, id := range want {
		if got := snap.LockQueue[i].Task; got != id {
			t.Fatalf("lock queue[%d] = %q, want %q; full=%+v", i, got, id, snap.LockQueue)
		}
	}
}

func TestReentrantLockCountsEachLayer(t *testing.T) {
	s := NewScheduler()
	const a ID = "a"

	lockN(t, s, a, 3)
	snap := s.Snapshot()
	if snap.Owner != a || snap.Depth != 3 {
		t.Fatalf("owner/depth = %q/%d, want a/3", snap.Owner, snap.Depth)
	}

	unlockN(t, s, a, 3)
	snap = s.Snapshot()
	if snap.Owner != "" || snap.Depth != 0 {
		t.Fatalf("after unlock owner/depth = %q/%d, want empty/0", snap.Owner, snap.Depth)
	}

	_, err := s.Unlock(a)
	if !errors.Is(err, ErrNotOwner) {
		t.Fatalf("unlock unlocked mutex error = %v, want ErrNotOwner", err)
	}
}

func TestWaitReleasesAllLayersAndRestoresDepth(t *testing.T) {
	s := NewScheduler()
	const a ID = "a"

	lockN(t, s, a, 3)
	w := mustOK(t)(s.Wait(a))
	if w.WaitID != 1 || w.Status != StatusWaitStarted || w.ReleasedDepth != 3 {
		t.Fatalf("Wait result = %+v, want wait 1 started and released depth 3", w)
	}

	snap := s.Snapshot()
	if snap.Owner != "" || snap.Depth != 0 {
		t.Fatalf("during wait owner/depth = %q/%d, want empty/0", snap.Owner, snap.Depth)
	}
	if len(snap.CondQueue) != 1 || snap.CondQueue[0].SavedDepth != 3 {
		t.Fatalf("cond queue = %+v, want one waiter with saved depth 3", snap.CondQueue)
	}
	if len(snap.LockQueue) != 0 {
		t.Fatalf("Wait must not enter lock queue before wake: %+v", snap.LockQueue)
	}

	if _, err := s.GrantNext(); !errors.Is(err, ErrQueueEmpty) {
		t.Fatalf("GrantNext before wake error = %v, want ErrQueueEmpty", err)
	}
	if _, err := s.FinishWait(a); !errors.Is(err, ErrWaitNotGranted) {
		t.Fatalf("FinishWait before grant error = %v, want ErrWaitNotGranted", err)
	}

	sig := mustOK(t)(s.Signal())
	if sig.Reason != ReasonSignal || sig.WaitID != 1 || sig.QueuePosition != 1 {
		t.Fatalf("Signal result = %+v, want signal queued wait 1 at position 1", sig)
	}

	info, ok := s.WaitAttempt(1)
	if !ok {
		t.Fatal("WaitAttempt(1) not found")
	}
	if info.Reason != ReasonSignal || info.LockEnqueueCount != 1 {
		t.Fatalf("wait info = %+v, want signal and exactly one lock enqueue", info)
	}

	granted := mustOK(t)(s.GrantNext())
	if granted.Status != StatusWaitGranted || granted.Depth != 3 || granted.Reason != ReasonSignal {
		t.Fatalf("GrantNext = %+v, want granted signal with restored depth 3", granted)
	}
	snap = s.Snapshot()
	if snap.Owner != a || snap.Depth != 3 {
		t.Fatalf("after grant owner/depth = %q/%d, want a/3", snap.Owner, snap.Depth)
	}

	done := mustOK(t)(s.FinishWait(a))
	if done.Status != StatusWaitCompleted || done.Reason != ReasonSignal || done.Depth != 3 {
		t.Fatalf("FinishWait = %+v, want completed signal while holding depth 3", done)
	}
	unlockN(t, s, a, 3)
}

func TestTwoWaitersBroadcastOrderAndCancelWhileHolding(t *testing.T) {
	s := NewScheduler()
	const (
		a ID = "a"
		b ID = "b"
		m ID = "broadcaster"
	)

	// Both waiters independently reach depth three before blocking.
	lockN(t, s, a, 3)
	wa := mustOK(t)(s.Wait(a))

	lockN(t, s, b, 3)
	wb := mustOK(t)(s.Wait(b))

	if got := s.Snapshot().CondQueue; len(got) != 2 || got[0].Task != a || got[1].Task != b {
		t.Fatalf("cond queue before broadcast = %+v, want [a b]", got)
	}

	// The broadcaster normally holds the lock while broadcasting. Broadcast
	// moves current condition waiters to the mutex queue but does not release
	// the broadcaster's lock.
	lockN(t, s, m, 1)
	bc := mustOK(t)(s.Broadcast())
	if len(bc.Woken) != 2 || bc.Woken[0] != a || bc.Woken[1] != b {
		t.Fatalf("Broadcast Woken = %v, want [a b]", bc.Woken)
	}

	snap := s.Snapshot()
	if snap.Owner != m || snap.Depth != 1 {
		t.Fatalf("broadcast changed owner/depth to %q/%d", snap.Owner, snap.Depth)
	}
	if len(snap.CondQueue) != 0 {
		t.Fatalf("cond queue after broadcast = %+v, want empty", snap.CondQueue)
	}
	requireQueueTasks(t, snap, a, b)
	for i, entry := range snap.LockQueue {
		wantWait := wa.WaitID
		if i == 1 {
			wantWait = wb.WaitID
		}
		if entry.WaitID != wantWait || entry.RestoreDepth != 3 {
			t.Fatalf("lock queue[%d] = %+v, want wait %d restore depth 3", i, entry, wantWait)
		}
	}

	for _, id := range []int{wa.WaitID, wb.WaitID} {
		info, ok := s.WaitAttempt(id)
		if !ok {
			t.Fatalf("WaitAttempt(%d) missing", id)
		}
		if info.Reason != ReasonBroadcast || info.LockEnqueueCount != 1 {
			t.Fatalf("wait %d = %+v, want broadcast and one enqueue", id, info)
		}
	}

	unlockN(t, s, m, 1)

	// First queued waiter reacquires and has depth restored before its Wait
	// call is resumed/completed.
	ga := mustOK(t)(s.GrantNext())
	if ga.Task != a || ga.Depth != 3 || ga.Reason != ReasonBroadcast {
		t.Fatalf("first GrantNext = %+v, want a/depth3/broadcast", ga)
	}
	snap = s.Snapshot()
	if snap.Owner != a || snap.Depth != 3 {
		t.Fatalf("after first grant owner/depth = %q/%d, want a/3", snap.Owner, snap.Depth)
	}
	requireQueueTasks(t, snap, b)

	// Cancellation interleaves after ownership is restored but before Wait
	// returns. The broadcast result was already selected, so cancellation is
	// observed but cannot replace it or enqueue a second mutex request.
	cancel := mustOK(t)(s.Cancel(a))
	if cancel.Status != StatusLateEvent || !cancel.Late || !cancel.CancelRequested || cancel.Reason != ReasonBroadcast {
		t.Fatalf("holding-lock cancel = %+v, want late cancellation preserving broadcast", cancel)
	}
	snap = s.Snapshot()
	requireQueueTasks(t, snap, b)
	info, _ := s.WaitAttempt(wa.WaitID)
	if info.LockEnqueueCount != 1 || info.Reason != ReasonBroadcast {
		t.Fatalf("cancel changed wait attempt: %+v", info)
	}

	fa := mustOK(t)(s.FinishWait(a))
	if fa.Reason != ReasonBroadcast || fa.Depth != 3 || !fa.CancelRequested {
		t.Fatalf("FinishWait after holding cancel = %+v, want broadcast/depth3/cancel flag", fa)
	}
	if snap := s.Snapshot(); snap.Owner != a || snap.Depth != 3 {
		t.Fatalf("FinishWait owner/depth = %q/%d, want a/3", snap.Owner, snap.Depth)
	}

	// Three ordinary unlocks are needed because the saved depth was three.
	unlockN(t, s, a, 3)
	gb := mustOK(t)(s.GrantNext())
	if gb.Task != b || gb.Depth != 3 || gb.Reason != ReasonBroadcast {
		t.Fatalf("second GrantNext = %+v, want b/depth3/broadcast", gb)
	}
	fb := mustOK(t)(s.FinishWait(b))
	if fb.Reason != ReasonBroadcast || fb.Depth != 3 {
		t.Fatalf("FinishWait(b) = %+v, want broadcast/depth3", fb)
	}
	unlockN(t, s, b, 3)

	final := s.Snapshot()
	if final.Owner != "" || final.Depth != 0 || len(final.LockQueue) != 0 || len(final.CondQueue) != 0 {
		t.Fatalf("final snapshot not idle/empty: %+v", final)
	}
}

func TestCancelWinsAndReturnsWhileHoldingRestoredDepth(t *testing.T) {
	s := NewScheduler()
	const a ID = "a"

	lockN(t, s, a, 3)
	w := mustOK(t)(s.Wait(a))

	c := mustOK(t)(s.Cancel(a))
	if c.Status != StatusWakeQueued || c.Reason != ReasonCancel || c.QueuePosition != 1 {
		t.Fatalf("Cancel = %+v, want cancel winner queued at position 1", c)
	}

	// All later wake sources are stale for this attempt.
	to := mustOK(t)(s.Timeout(w.WaitID))
	if !to.Late || to.Reason != ReasonCancel {
		t.Fatalf("Timeout after cancel = %+v, want late preserving cancel", to)
	}
	c2 := mustOK(t)(s.CancelWait(w.WaitID))
	if !c2.Late || c2.Reason != ReasonCancel || !c2.CancelRequested {
		t.Fatalf("second Cancel = %+v, want late preserving cancel", c2)
	}
	if sig := mustOK(t)(s.Signal()); sig.Status != StatusNoWaiters {
		t.Fatalf("Signal after cancel = %+v, want no_waiters", sig)
	}
	if bc := mustOK(t)(s.Broadcast()); bc.Status != StatusNoWaiters {
		t.Fatalf("Broadcast after cancel = %+v, want no_waiters", bc)
	}

	info, ok := s.WaitAttempt(w.WaitID)
	if !ok {
		t.Fatal("wait attempt missing")
	}
	if info.Reason != ReasonCancel || info.LockEnqueueCount != 1 {
		t.Fatalf("wait = %+v, want cancel and exactly one enqueue", info)
	}
	requireQueueTasks(t, s.Snapshot(), a)

	g := mustOK(t)(s.GrantNext())
	if g.Reason != ReasonCancel || g.Depth != 3 {
		t.Fatalf("GrantNext = %+v, want cancel with depth 3", g)
	}
	done := mustOK(t)(s.FinishWait(a))
	if done.Status != StatusWaitCompleted || done.Reason != ReasonCancel || done.Depth != 3 || !done.CancelRequested {
		t.Fatalf("FinishWait = %+v, want canceled return while holding depth 3", done)
	}
	if snap := s.Snapshot(); snap.Owner != a || snap.Depth != 3 {
		t.Fatalf("canceled return must hold restored lock; snapshot=%+v", snap)
	}
	unlockN(t, s, a, 3)
}

func TestCancellationRequestedWhileHoldingBeforeWaitStillRestoresDepth(t *testing.T) {
	s := NewScheduler()
	const a ID = "a"

	pending := mustOK(t)(s.Cancel(a))
	if pending.Status != StatusCancelPending {
		t.Fatalf("Cancel before task exists = %+v, want cancel_pending", pending)
	}

	lockN(t, s, a, 3)
	w := mustOK(t)(s.Wait(a))
	if w.Status != StatusWakeQueued || w.Reason != ReasonCancel || w.ReleasedDepth != 3 || w.QueuePosition != 1 {
		t.Fatalf("Wait with pending cancellation = %+v", w)
	}
	snap := s.Snapshot()
	if snap.Owner != "" || snap.Depth != 0 {
		t.Fatalf("pending-cancel Wait did not release all layers: %+v", snap)
	}
	requireQueueTasks(t, snap, a)

	g := mustOK(t)(s.GrantNext())
	if g.Depth != 3 || g.Reason != ReasonCancel {
		t.Fatalf("GrantNext = %+v, want depth3 cancel", g)
	}
	done := mustOK(t)(s.FinishWait(a))
	if done.Reason != ReasonCancel || done.Depth != 3 {
		t.Fatalf("FinishWait = %+v, want canceled holding depth3", done)
	}
	unlockN(t, s, a, 3)
}

func TestFirstWakeEventWinsAndEnqueuesOnce(t *testing.T) {
	tests := []struct {
		name string
		win  func(t *testing.T, s *Scheduler, id ID, waitID int) Result
		want WakeReason
	}{
		{
			name: "signal",
			want: ReasonSignal,
			win: func(t *testing.T, s *Scheduler, _ ID, _ int) Result {
				return mustOK(t)(s.Signal())
			},
		},
		{
			name: "broadcast",
			want: ReasonBroadcast,
			win: func(t *testing.T, s *Scheduler, _ ID, _ int) Result {
				return mustOK(t)(s.Broadcast())
			},
		},
		{
			name: "timeout",
			want: ReasonTimeout,
			win: func(t *testing.T, s *Scheduler, _ ID, waitID int) Result {
				return mustOK(t)(s.Timeout(waitID))
			},
		},
		{
			name: "cancel",
			want: ReasonCancel,
			win: func(t *testing.T, s *Scheduler, id ID, _ int) Result {
				return mustOK(t)(s.Cancel(id))
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			s := NewScheduler()
			const id ID = "waiter"
			lockN(t, s, id, 3)
			w := mustOK(t)(s.Wait(id))

			first := tc.win(t, s, id, w.WaitID)
			if first.Reason != tc.want {
				t.Fatalf("first event reason = %q, want %q", first.Reason, tc.want)
			}

			to := mustOK(t)(s.Timeout(w.WaitID))
			if !to.Late || to.Reason != tc.want {
				t.Fatalf("competing timeout = %+v, want late reason %q", to, tc.want)
			}
			c := mustOK(t)(s.Cancel(id))
			if !c.Late || c.Reason != tc.want {
				t.Fatalf("competing cancel = %+v, want late reason %q", c, tc.want)
			}
			for _, r := range []Result{mustOK(t)(s.Signal()), mustOK(t)(s.Broadcast())} {
				if r.Status != StatusNoWaiters {
					t.Fatalf("notification after wake = %+v, want no_waiters", r)
				}
			}

			info, ok := s.WaitAttempt(w.WaitID)
			if !ok {
				t.Fatal("wait attempt missing")
			}
			if info.Reason != tc.want {
				t.Fatalf("final reason = %q, want %q", info.Reason, tc.want)
			}
			if info.LockEnqueueCount != 1 {
				t.Fatalf("lock enqueue count = %d, want 1", info.LockEnqueueCount)
			}
			if q := s.Snapshot().LockQueue; len(q) != 1 {
				t.Fatalf("lock queue len = %d, want exactly one entry: %+v", len(q), q)
			}
		})
	}
}

func TestNotificationsAreNotStoredForLaterWaiters(t *testing.T) {
	s := NewScheduler()

	if r := mustOK(t)(s.Signal()); r.Status != StatusNoWaiters {
		t.Fatalf("Signal with no waiter = %+v, want no_waiters", r)
	}
	if r := mustOK(t)(s.Broadcast()); r.Status != StatusNoWaiters {
		t.Fatalf("Broadcast with no waiter = %+v, want no_waiters", r)
	}

	const a ID = "later"
	lockN(t, s, a, 2)
	w := mustOK(t)(s.Wait(a))

	info, _ := s.WaitAttempt(w.WaitID)
	if info.State != string(waitActive) || info.Reason != ReasonNone || info.LockEnqueueCount != 0 {
		t.Fatalf("old notification was stored: %+v", info)
	}
	if len(s.Snapshot().LockQueue) != 0 {
		t.Fatalf("stored notification caused lock queue entry: %+v", s.Snapshot().LockQueue)
	}

	// A fresh signal after the waiter exists is required.
	r := mustOK(t)(s.Signal())
	if r.WaitID != w.WaitID || r.Reason != ReasonSignal {
		t.Fatalf("fresh Signal = %+v, want wake %d", r, w.WaitID)
	}
}

func TestBroadcastAppendsAfterExistingMutexQueue(t *testing.T) {
	s := NewScheduler()
	const (
		a ID = "cond-a"
		b ID = "cond-b"
		m ID = "holder"
		l ID = "lock-waiter"
	)

	lockN(t, s, a, 3)
	mustOK(t)(s.Wait(a))
	lockN(t, s, b, 3)
	mustOK(t)(s.Wait(b))

	lockN(t, s, m, 1)
	mustOK(t)(s.Lock(l)) // normal request is already waiting when broadcast occurs.
	requireQueueTasks(t, s.Snapshot(), l)

	bc := mustOK(t)(s.Broadcast())
	if len(bc.Woken) != 2 || bc.Woken[0] != a || bc.Woken[1] != b {
		t.Fatalf("broadcast woken = %v, want [a b]", bc.Woken)
	}
	requireQueueTasks(t, s.Snapshot(), l, a, b)

	unlockN(t, s, m, 1)
	normal := mustOK(t)(s.GrantNext())
	if normal.Task != l || normal.WaitID != 0 || normal.Depth != 1 {
		t.Fatalf("normal queue head grant = %+v", normal)
	}
	unlockN(t, s, l, 1)

	ga := mustOK(t)(s.GrantNext())
	if ga.Task != a || ga.WaitID == 0 || ga.Depth != 3 {
		t.Fatalf("cond waiter a grant = %+v", ga)
	}
	mustOK(t)(s.FinishWait(a))
	unlockN(t, s, a, 3)

	gb := mustOK(t)(s.GrantNext())
	if gb.Task != b || gb.Depth != 3 {
		t.Fatalf("cond waiter b grant = %+v", gb)
	}
	mustOK(t)(s.FinishWait(b))
	unlockN(t, s, b, 3)
}

func TestPostedEventsAdvanceDeterministicallyInFIFOOrder(t *testing.T) {
	s := NewScheduler()
	s.Post(
		Event{Type: EventLock, Task: "a"},
		Event{Type: EventLock, Task: "b"},
		Event{Type: EventUnlock, Task: "a"},
	)

	st, err, ok := s.RunOne()
	if !ok || err != nil || st.Result.Status != StatusAcquired {
		t.Fatalf("RunOne lock a: step=%+v err=%v ok=%v", st, err, ok)
	}
	if got := s.Snapshot().Owner; got != "a" {
		t.Fatalf("owner after first posted event = %q, want a", got)
	}

	steps, err := s.RunPosted()
	if err != nil {
		t.Fatalf("RunPosted: %v", err)
	}
	if len(steps) != 2 {
		t.Fatalf("RunPosted steps = %d, want 2", len(steps))
	}
	snap := s.Snapshot()
	if snap.Owner != "" || snap.Depth != 0 {
		t.Fatalf("after posted unlock owner/depth = %q/%d", snap.Owner, snap.Depth)
	}
	requireQueueTasks(t, snap, "b")

	history := s.History()
	if len(history) != 3 {
		t.Fatalf("history length = %d, want 3", len(history))
	}
	for i, st := range history {
		if st.Number != i+1 || st.After.Step != i+1 {
			t.Fatalf("history[%d] number/after.step = %d/%d", i, st.Number, st.After.Step)
		}
	}

	if _, err, ok := s.RunOne(); ok || err != nil {
		t.Fatalf("empty RunOne ok=%v err=%v, want false,nil", ok, err)
	}
}

func TestInvalidOperationsAreErrors(t *testing.T) {
	s := NewScheduler()

	if _, err := s.Wait("nobody"); !errors.Is(err, ErrNotOwner) {
		t.Fatalf("Wait without owner error = %v, want ErrNotOwner", err)
	}
	if _, err := s.FinishWait("nobody"); !errors.Is(err, ErrNoActiveWait) {
		t.Fatalf("FinishWait without wait error = %v, want ErrNoActiveWait", err)
	}
	if _, err := s.Timeout(404); !errors.Is(err, ErrWaitNotFound) {
		t.Fatalf("Timeout unknown wait error = %v, want ErrWaitNotFound", err)
	}

	lockN(t, s, "a", 1)
	if _, err := s.GrantNext(); !errors.Is(err, ErrLockBusy) {
		t.Fatalf("GrantNext with owner error = %v, want ErrLockBusy", err)
	}
	if _, err := s.Register("a"); !errors.Is(err, ErrDuplicateTask) {
		t.Fatalf("duplicate register error = %v, want ErrDuplicateTask", err)
	}
}

func TestSignalWakesOnlyFrontWaiter(t *testing.T) {
	s := NewScheduler()
	const (
		a ID = "a"
		b ID = "b"
	)

	lockN(t, s, a, 1)
	wa := mustOK(t)(s.Wait(a))
	lockN(t, s, b, 1)
	wb := mustOK(t)(s.Wait(b))

	sig := mustOK(t)(s.Signal())
	if sig.WaitID != wa.WaitID || sig.Reason != ReasonSignal {
		t.Fatalf("Signal = %+v, want front wait %d", sig, wa.WaitID)
	}
	snap := s.Snapshot()
	requireQueueTasks(t, snap, a)
	if len(snap.CondQueue) != 1 || snap.CondQueue[0].Task != b || snap.CondQueue[0].WaitID != wb.WaitID {
		t.Fatalf("cond queue after Signal = %+v, want only b/%d", snap.CondQueue, wb.WaitID)
	}
}

func TestStaleTimeoutFromPriorWaitDoesNotDecideLaterWait(t *testing.T) {
	s := NewScheduler()
	const a ID = "a"

	lockN(t, s, a, 1)
	first := mustOK(t)(s.Wait(a))
	mustOK(t)(s.Timeout(first.WaitID))
	mustOK(t)(s.GrantNext())
	done := mustOK(t)(s.FinishWait(a))
	if done.Reason != ReasonTimeout {
		t.Fatalf("first wait reason = %q, want timeout", done.Reason)
	}
	unlockN(t, s, a, 1)

	lockN(t, s, a, 2)
	second := mustOK(t)(s.Wait(a))

	// The old timer event is wait-ID scoped and must not terminate wait two.
	stale := mustOK(t)(s.Timeout(first.WaitID))
	if !stale.Late || stale.Reason != ReasonTimeout || stale.WaitID != first.WaitID {
		t.Fatalf("stale timeout = %+v, want late old timeout result", stale)
	}
	info, ok := s.WaitAttempt(second.WaitID)
	if !ok {
		t.Fatal("second wait missing")
	}
	if info.State != string(waitActive) || info.Reason != ReasonNone || info.LockEnqueueCount != 0 {
		t.Fatalf("second wait affected by stale timeout: %+v", info)
	}

	sig := mustOK(t)(s.Signal())
	if sig.WaitID != second.WaitID || sig.Reason != ReasonSignal {
		t.Fatalf("fresh Signal = %+v, want second wait %d", sig, second.WaitID)
	}
}
