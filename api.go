package condmodel

// Register explicitly creates a task. Tasks are also created automatically by
// Lock/Wait/Cancel when convenient for tests.
func (s *Scheduler) Register(task ID) (Result, error) {
	st, err := s.Dispatch(Event{Type: EventRegister, Task: task})
	return st.Result, err
}

// Lock requests one normal (depth-one) reentrant lock acquisition.
func (s *Scheduler) Lock(task ID) (Result, error) {
	st, err := s.Dispatch(Event{Type: EventLock, Task: task})
	return st.Result, err
}

// Unlock releases one reentrant layer. It does not automatically grant the
// next queue entry; call GrantNext as the next deterministic scheduler step.
func (s *Scheduler) Unlock(task ID) (Result, error) {
	st, err := s.Dispatch(Event{Type: EventUnlock, Task: task})
	return st.Result, err
}

// GrantNext lets the deterministic scheduler admit the mutex queue head.
func (s *Scheduler) GrantNext() (Result, error) {
	st, err := s.Dispatch(Event{Type: EventGrant})
	return st.Result, err
}

// Wait begins cond.Wait: it records the current depth, removes every reentrant
// layer in one transition, and puts the task on the condition queue.
func (s *Scheduler) Wait(task ID) (Result, error) {
	st, err := s.Dispatch(Event{Type: EventWait, Task: task})
	return st.Result, err
}

// FinishWait models the scheduled return from cond.Wait after GrantNext has
// restored ownership and depth.
func (s *Scheduler) FinishWait(task ID) (Result, error) {
	st, err := s.Dispatch(Event{Type: EventFinishWait, Task: task})
	return st.Result, err
}

// Signal wakes the front active condition waiter, if one exists.
func (s *Scheduler) Signal() (Result, error) {
	st, err := s.Dispatch(Event{Type: EventSignal})
	return st.Result, err
}

// Broadcast wakes all currently active condition waiters in FIFO order.
func (s *Scheduler) Broadcast() (Result, error) {
	st, err := s.Dispatch(Event{Type: EventBroadcast})
	return st.Result, err
}

// Timeout delivers a timeout event to a specific wait attempt.
func (s *Scheduler) Timeout(waitID int) (Result, error) {
	st, err := s.Dispatch(Event{Type: EventTimeout, WaitID: waitID})
	return st.Result, err
}

// TimeoutTask delivers a timeout to the task's current unfinished wait.
func (s *Scheduler) TimeoutTask(task ID) (Result, error) {
	st, err := s.Dispatch(Event{Type: EventTimeout, Task: task})
	return st.Result, err
}

// Cancel requests task cancellation. Cancellation is task-sticky and also
// arbitrates the current wait when that wait has no terminal result yet.
func (s *Scheduler) Cancel(task ID) (Result, error) {
	st, err := s.Dispatch(Event{Type: EventCancel, Task: task})
	return st.Result, err
}

// CancelWait delivers cancellation using a wait attempt identifier.
func (s *Scheduler) CancelWait(waitID int) (Result, error) {
	st, err := s.Dispatch(Event{Type: EventCancel, WaitID: waitID})
	return st.Result, err
}
