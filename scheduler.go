package condmodel

import (
	"fmt"
)

// Dispatch processes exactly one event as one deterministic step.
func (s *Scheduler) Dispatch(ev Event) (Step, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.dispatchLocked(ev)
}

func (s *Scheduler) dispatchLocked(ev Event) (Step, error) {
	s.step++
	res, err := s.applyLocked(ev)
	if err != nil && !res.OK {
		// Keep the zero/partial result unusable as a successful transition.
		res.OK = false
	}
	after := s.snapshotLocked()
	record := Step{
		Number: s.step,
		Event:  ev,
		Result: res,
		After:  after,
	}
	if err != nil {
		record.Err = err.Error()
	}
	s.history = append(s.history, record)
	return record, err
}

// Post appends external events to the deterministic FIFO input queue.
func (s *Scheduler) Post(events ...Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pending = append(s.pending, events...)
}

// RunOne processes the oldest posted event. The boolean is false when no event
// is pending.
func (s *Scheduler) RunOne() (Step, error, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.pending) == 0 {
		return Step{}, nil, false
	}
	ev := s.pending[0]
	s.pending = s.pending[1:]
	st, err := s.dispatchLocked(ev)
	return st, err, true
}

// RunPosted processes all currently posted events in FIFO order. It stops at
// the first invalid event and returns all steps produced before that error.
func (s *Scheduler) RunPosted() ([]Step, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	var steps []Step
	for len(s.pending) > 0 {
		ev := s.pending[0]
		s.pending = s.pending[1:]
		st, err := s.dispatchLocked(ev)
		steps = append(steps, st)
		if err != nil {
			return steps, err
		}
	}
	return steps, nil
}

// StepCount returns the number of dispatched model events.
func (s *Scheduler) StepCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.step
}

// History returns a copy of every processed step, including its post-step
// snapshot.
func (s *Scheduler) History() []Step {
	s.mu.Lock()
	defer s.mu.Unlock()

	out := make([]Step, len(s.history))
	for i, st := range s.history {
		out[i] = cloneStep(st)
	}
	return out
}

// Snapshot returns the current owner, depth, mutex queue, and condition queue.
func (s *Scheduler) Snapshot() Snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	return cloneSnapshot(s.snapshotLocked())
}

func (s *Scheduler) applyLocked(ev Event) (Result, error) {
	switch ev.Type {
	case EventRegister:
		return s.registerLocked(ev.Task)
	case EventLock:
		return s.lockLocked(ev.Task)
	case EventUnlock:
		return s.unlockLocked(ev.Task)
	case EventGrant:
		return s.grantNextLocked()
	case EventWait:
		return s.waitLocked(ev.Task)
	case EventFinishWait:
		return s.finishWaitLocked(ev.Task)
	case EventSignal:
		return s.signalLocked()
	case EventBroadcast:
		return s.broadcastLocked()
	case EventTimeout:
		return s.timeoutEventLocked(ev)
	case EventCancel:
		return s.cancelEventLocked(ev)
	default:
		return Result{}, fmt.Errorf("condmodel: unknown event type %q", ev.Type)
	}
}

func (s *Scheduler) registerLocked(id ID) (Result, error) {
	if id == "" {
		return Result{}, ErrEmptyTask
	}
	if _, exists := s.tasks[id]; exists {
		return Result{}, fmt.Errorf("%w: %q", ErrDuplicateTask, id)
	}
	s.tasks[id] = &task{id: id, state: TaskIdle}
	s.taskOrder = append(s.taskOrder, id)
	return Result{OK: true, Status: StatusRegistered, Task: id}, nil
}

func (s *Scheduler) ensureTaskLocked(id ID) (*task, error) {
	if id == "" {
		return nil, ErrEmptyTask
	}
	t, ok := s.tasks[id]
	if !ok {
		t = &task{id: id, state: TaskIdle}
		s.tasks[id] = t
		s.taskOrder = append(s.taskOrder, id)
	}
	return t, nil
}

func (s *Scheduler) lockLocked(id ID) (Result, error) {
	t, err := s.ensureTaskLocked(id)
	if err != nil {
		return Result{}, err
	}

	switch t.state {
	case TaskLockBlocked:
		return Result{}, fmt.Errorf("%w: task %q is already in the lock queue", ErrAlreadyQueued, id)
	case TaskCondWaiting, TaskReacquiring:
		return Result{}, fmt.Errorf("%w: task %q has an unfinished condition wait", ErrInvalidState, id)
	case TaskWaitToResume:
		return Result{}, fmt.Errorf("%w: task %q must return from Wait before locking again", ErrInvalidState, id)
	}

	if s.owner == id {
		s.depth++
		t.state = TaskHolding
		return Result{
			OK:     true,
			Status: StatusReentered,
			Task:   id,
			Depth:  s.depth,
		}, nil
	}

	if s.owner == "" {
		s.owner = id
		s.depth = 1
		t.state = TaskHolding
		return Result{
			OK:     true,
			Status: StatusAcquired,
			Task:   id,
			Depth:  1,
		}, nil
	}

	for _, ticket := range s.lockQueue {
		if ticket.task == id {
			return Result{}, fmt.Errorf("%w: task %q", ErrAlreadyQueued, id)
		}
	}

	s.nextSeq++
	ticket := &lockTicket{
		task:         id,
		restoreDepth: 1,
		sequence:     s.nextSeq,
	}
	s.lockQueue = append(s.lockQueue, ticket)
	t.state = TaskLockBlocked

	return Result{
		OK:            true,
		Status:        StatusBlocked,
		Task:          id,
		Depth:         s.depth,
		QueuePosition: len(s.lockQueue),
		Message:       fmt.Sprintf("mutex owned by %q", s.owner),
	}, nil
}

func (s *Scheduler) unlockLocked(id ID) (Result, error) {
	t, err := s.ensureTaskLocked(id)
	if err != nil {
		return Result{}, err
	}
	if s.owner != id {
		return Result{}, fmt.Errorf("%w: task %q; owner is %q", ErrNotOwner, id, s.owner)
	}
	if t.state == TaskWaitToResume {
		return Result{}, fmt.Errorf("%w: Wait for task %q has not returned yet", ErrInvalidState, id)
	}
	if s.depth <= 0 {
		return Result{}, fmt.Errorf("%w: non-positive depth for owner %q", ErrInvalidState, id)
	}

	s.depth--
	if s.depth > 0 {
		t.state = TaskHolding
		return Result{
			OK:     true,
			Status: StatusStillHeld,
			Task:   id,
			Depth:  s.depth,
		}, nil
	}

	s.owner = ""
	s.depth = 0
	t.state = TaskIdle
	return Result{
		OK:            true,
		Status:        StatusReleased,
		Task:          id,
		ReleasedDepth: 1,
	}, nil
}

func (s *Scheduler) grantNextLocked() (Result, error) {
	if s.owner != "" {
		return Result{}, fmt.Errorf("%w: %q still holds depth %d", ErrLockBusy, s.owner, s.depth)
	}
	if len(s.lockQueue) == 0 {
		return Result{}, ErrQueueEmpty
	}

	ticket := s.lockQueue[0]
	s.lockQueue = s.lockQueue[1:]
	t := s.tasks[ticket.task]
	if t == nil {
		return Result{}, fmt.Errorf("%w: missing task %q", ErrInvalidState, ticket.task)
	}

	s.owner = ticket.task
	s.depth = ticket.restoreDepth

	if ticket.waitID == 0 {
		t.state = TaskHolding
		return Result{
			OK:     true,
			Status: StatusAcquired,
			Task:   ticket.task,
			Depth:  s.depth,
		}, nil
	}

	w := s.waits[ticket.waitID]
	if w == nil {
		return Result{}, fmt.Errorf("%w: missing wait %d", ErrWaitNotFound, ticket.waitID)
	}
	if w.state != waitLockQueued {
		return Result{}, fmt.Errorf("%w: wait %d is %q, expected lock_queued_once", ErrInvalidState, w.id, w.state)
	}

	w.state = waitGranted
	w.grantedStep = s.step
	t.state = TaskWaitToResume
	t.activeWaitID = w.id

	return Result{
		OK:     true,
		Status: StatusWaitGranted,
		Task:   ticket.task,
		WaitID: w.id,
		Reason: w.reason,
		Depth:  s.depth,
	}, nil
}

func (s *Scheduler) snapshotLocked() Snapshot {
	snap := Snapshot{
		Step:      s.step,
		Owner:     s.owner,
		Depth:     s.depth,
		LockQueue: []LockEntry{},
		CondQueue: []WaitEntry{},
		Tasks:     []TaskEntry{},
	}

	positionByWait := make(map[int]int, len(s.lockQueue))
	for i, ticket := range s.lockQueue {
		snap.LockQueue = append(snap.LockQueue, LockEntry{
			Task:         ticket.task,
			RestoreDepth: ticket.restoreDepth,
			WaitID:       ticket.waitID,
			Sequence:     ticket.sequence,
		})
		if ticket.waitID != 0 {
			positionByWait[ticket.waitID] = i + 1
		}
	}

	for _, w := range s.condQueue {
		snap.CondQueue = append(snap.CondQueue, s.waitEntryLocked(w, positionByWait))
	}

	for _, id := range s.taskOrder {
		t := s.tasks[id]
		snap.Tasks = append(snap.Tasks, TaskEntry{
			ID:              t.id,
			State:           t.state,
			CancelRequested: t.cancelRequested,
			ActiveWaitID:    t.activeWaitID,
			LastWaitID:      t.lastWaitID,
		})
	}

	return snap
}

func (s *Scheduler) waitEntryLocked(w *waitAttempt, positions map[int]int) WaitEntry {
	pos := -1
	if positions != nil {
		if p, ok := positions[w.id]; ok {
			pos = p
		}
	}
	t := s.tasks[w.task]
	cancelRequested := false
	if t != nil {
		cancelRequested = t.cancelRequested
	}
	return WaitEntry{
		WaitID:            w.id,
		Task:              w.task,
		SavedDepth:        w.savedDepth,
		State:             string(w.state),
		Reason:            w.reason,
		LockEnqueueCount:  w.enqueueCount,
		CancelRequested:   cancelRequested,
		LockQueuePosition: pos,
	}
}

// WaitAttempt returns detailed information about one wait attempt.
func (s *Scheduler) WaitAttempt(waitID int) (WaitInfo, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()

	w, ok := s.waits[waitID]
	if !ok {
		return WaitInfo{}, false
	}
	positions := map[int]int{}
	for i, ticket := range s.lockQueue {
		if ticket.waitID != 0 {
			positions[ticket.waitID] = i + 1
		}
	}
	entry := s.waitEntryLocked(w, positions)
	return WaitInfo{
		WaitEntry:     entry,
		CreatedStep:   w.createdStep,
		DecidedStep:   w.decidedStep,
		GrantedStep:   w.grantedStep,
		CompletedStep: w.completedStep,
	}, true
}

func cloneSnapshot(in Snapshot) Snapshot {
	out := in
	if in.LockQueue != nil {
		out.LockQueue = append([]LockEntry(nil), in.LockQueue...)
	}
	if in.CondQueue != nil {
		out.CondQueue = append([]WaitEntry(nil), in.CondQueue...)
	}
	if in.Tasks != nil {
		out.Tasks = append([]TaskEntry(nil), in.Tasks...)
	}
	return out
}

func cloneStep(in Step) Step {
	out := in
	out.After = cloneSnapshot(in.After)
	if in.Result.Woken != nil {
		out.Result.Woken = append([]ID(nil), in.Result.Woken...)
	}
	return out
}
