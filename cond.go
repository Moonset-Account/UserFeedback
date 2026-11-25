package condmodel

import "fmt"

func (s *Scheduler) waitLocked(id ID) (Result, error) {
	t, err := s.ensureTaskLocked(id)
	if err != nil {
		return Result{}, err
	}
	if s.owner != id || s.depth == 0 {
		return Result{}, fmt.Errorf("%w: Wait requires task %q to own the mutex", ErrNotOwner, id)
	}
	if t.state != TaskHolding {
		return Result{}, fmt.Errorf("%w: task %q state is %q", ErrInvalidState, id, t.state)
	}
	if t.activeWaitID != 0 {
		return Result{}, fmt.Errorf("%w: task %q already has active wait %d", ErrInvalidState, id, t.activeWaitID)
	}

	savedDepth := s.depth
	releasedDepth := s.depth

	s.nextWait++
	w := &waitAttempt{
		id:          s.nextWait,
		task:        id,
		savedDepth:  savedDepth,
		state:       waitActive,
		createdStep: s.step,
	}
	s.waits[w.id] = w
	s.condQueue = append(s.condQueue, w)
	t.activeWaitID = w.id
	t.state = TaskCondWaiting

	// A reentrant holder releases every held layer in one transition. The
	// owner/depth are cleared without traversing the individual Unlock calls.
	s.owner = ""
	s.depth = 0

	res := Result{
		OK:            true,
		Status:        StatusWaitStarted,
		Task:          id,
		WaitID:        w.id,
		Depth:         savedDepth,
		ReleasedDepth: releasedDepth,
	}

	// Task cancellation is sticky like a canceled context. If cancellation was
	// requested before this Wait event, this new attempt still follows the
	// normal release -> one lock-queue insertion -> grant -> return path.
	if t.cancelRequested {
		decided := s.decideWakeLocked(w, ReasonCancel)
		res.Status = StatusWakeQueued
		res.Reason = ReasonCancel
		res.QueuePosition = decided.QueuePosition
		res.CancelRequested = true
		res.Message = "previously requested cancellation selected this wait"
	}

	return res, nil
}

func (s *Scheduler) finishWaitLocked(id ID) (Result, error) {
	t, err := s.ensureTaskLocked(id)
	if err != nil {
		return Result{}, err
	}
	if t.activeWaitID == 0 {
		return Result{}, fmt.Errorf("%w: task %q", ErrNoActiveWait, id)
	}

	w := s.waits[t.activeWaitID]
	if w == nil {
		return Result{}, fmt.Errorf("%w: wait %d not found", ErrWaitNotFound, t.activeWaitID)
	}
	if w.state != waitGranted {
		return Result{}, fmt.Errorf("%w: wait %d state %q", ErrWaitNotGranted, w.id, w.state)
	}
	if s.owner != id || s.depth != w.savedDepth {
		return Result{}, fmt.Errorf("%w: restored lock invariant failed: owner=%q depth=%d want depth=%d",
			ErrInvalidState, s.owner, s.depth, w.savedDepth)
	}

	w.state = waitCompleted
	w.completedStep = s.step
	t.activeWaitID = 0
	t.lastWaitID = w.id
	t.state = TaskHolding

	return Result{
		OK:              true,
		Status:          StatusWaitCompleted,
		Task:            id,
		WaitID:          w.id,
		Reason:          w.reason,
		Depth:           s.depth,
		CancelRequested: t.cancelRequested,
	}, nil
}

func (s *Scheduler) signalLocked() (Result, error) {
	if len(s.condQueue) == 0 {
		// Standard condition-variable semantics: a signal with no current
		// waiter is lost. There is deliberately no signal permit/counter.
		return Result{
			OK:     true,
			Status: StatusNoWaiters,
			Reason: ReasonSignal,
		}, nil
	}

	w := s.condQueue[0]
	res := s.decideWakeLocked(w, ReasonSignal)
	return res, nil
}

func (s *Scheduler) broadcastLocked() (Result, error) {
	if len(s.condQueue) == 0 {
		return Result{
			OK:     true,
			Status: StatusNoWaiters,
			Reason: ReasonBroadcast,
		}, nil
	}

	// Copy the condition queue before decideWakeLocked removes each entry.
	current := append([]*waitAttempt(nil), s.condQueue...)
	woken := make([]ID, 0, len(current))

	var last Result
	for _, w := range current {
		last = s.decideWakeLocked(w, ReasonBroadcast)
		woken = append(woken, w.task)
	}
	last.Status = StatusWakeQueued
	last.Reason = ReasonBroadcast
	last.Woken = woken
	last.QueuePosition = 0
	last.Message = fmt.Sprintf("broadcast moved %d waiters to lock queue", len(woken))
	return last, nil
}

func (s *Scheduler) timeoutEventLocked(ev Event) (Result, error) {
	w, err := s.waitForEventLocked(ev)
	if err != nil {
		return Result{}, err
	}
	if ev.WaitID == 0 {
		// Task-scoped timeout only makes sense for the active wait.
	}
	if w.state == waitActive {
		return s.decideWakeLocked(w, ReasonTimeout), nil
	}
	return lateResult(w, ReasonTimeout), nil
}

func (s *Scheduler) cancelEventLocked(ev Event) (Result, error) {
	var (
		w   *waitAttempt
		t   *task
		err error
	)

	if ev.WaitID != 0 {
		w, err = s.waitByIDLocked(ev.WaitID)
		if err != nil {
			return Result{}, err
		}
		t = s.tasks[w.task]
	} else {
		t, err = s.ensureTaskLocked(ev.Task)
		if err != nil {
			return Result{}, err
		}
		if t.activeWaitID != 0 {
			w = s.waits[t.activeWaitID]
		}
	}

	t.cancelRequested = true

	if w == nil {
		return Result{
			OK:              true,
			Status:          StatusCancelPending,
			Task:            t.id,
			CancelRequested: true,
			Message:         "cancellation recorded for a future wait",
		}, nil
	}

	if w.state == waitActive {
		res := s.decideWakeLocked(w, ReasonCancel)
		res.CancelRequested = true
		return res, nil
	}

	res := lateResult(w, ReasonCancel)
	res.Task = t.id
	res.CancelRequested = true
	if w.state == waitGranted {
		res.Message = "cancellation observed while the wait holds the restored lock; established wake result is immutable"
	}
	return res, nil
}

func (s *Scheduler) waitForEventLocked(ev Event) (*waitAttempt, error) {
	if ev.WaitID != 0 {
		return s.waitByIDLocked(ev.WaitID)
	}
	t, err := s.ensureTaskLocked(ev.Task)
	if err != nil {
		return nil, err
	}
	if t.activeWaitID == 0 {
		return nil, fmt.Errorf("%w: task %q", ErrNoActiveWait, ev.Task)
	}
	return s.waitByIDLocked(t.activeWaitID)
}

func (s *Scheduler) waitByIDLocked(id int) (*waitAttempt, error) {
	if id == 0 {
		return nil, fmt.Errorf("%w: zero wait id", ErrWaitNotFound)
	}
	w, ok := s.waits[id]
	if !ok {
		return nil, fmt.Errorf("%w: %d", ErrWaitNotFound, id)
	}
	return w, nil
}

// decideWakeLocked selects the single terminal wake reason and performs the
// one-and-only insertion into the mutex queue for this wait. Later wake events
// must not call this method for the same wait because its state is no longer
// waitActive.
func (s *Scheduler) decideWakeLocked(w *waitAttempt, reason WakeReason) Result {
	if w.state != waitActive {
		return lateResult(w, reason)
	}

	w.state = waitLockQueued
	w.reason = reason
	w.decidedStep = s.step
	w.enqueueCount++

	s.nextSeq++
	ticket := &lockTicket{
		task:         w.task,
		restoreDepth: w.savedDepth,
		waitID:       w.id,
		sequence:     s.nextSeq,
	}
	s.lockQueue = append(s.lockQueue, ticket)
	s.removeCondWaitLocked(w.id)

	if t := s.tasks[w.task]; t != nil {
		t.state = TaskReacquiring
	}

	return Result{
		OK:            true,
		Status:        StatusWakeQueued,
		Task:          w.task,
		WaitID:        w.id,
		Reason:        reason,
		Depth:         w.savedDepth,
		QueuePosition: len(s.lockQueue),
	}
}

func (s *Scheduler) removeCondWaitLocked(waitID int) {
	for i, w := range s.condQueue {
		if w.id == waitID {
			s.condQueue = append(s.condQueue[:i], s.condQueue[i+1:]...)
			return
		}
	}
}

func lateResult(w *waitAttempt, incoming WakeReason) Result {
	return Result{
		OK:      true,
		Status:  StatusLateEvent,
		Task:    w.task,
		WaitID:  w.id,
		Reason:  w.reason,
		Depth:   w.savedDepth,
		Late:    true,
		Message: fmt.Sprintf("%s event arrived after %s result was selected", incoming, w.reason),
	}
}
