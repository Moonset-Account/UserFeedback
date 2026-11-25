// Package condmodel contains a deterministic, event-driven model of a
// reentrant mutex and a condition variable for a coroutine runtime.
//
// A Scheduler is a pure state model: callers (or an external deterministic
// scheduler) choose Event order and advance the model one event at a time.
// No real goroutine or timer is created by this package.
package condmodel

import (
	"errors"
	"sync"
)

// ID identifies a modeled coroutine/task.
type ID string

// WakeReason is the single terminal wake result selected for one Wait attempt.
type WakeReason string

const (
	ReasonNone      WakeReason = ""
	ReasonSignal    WakeReason = "signal"
	ReasonBroadcast WakeReason = "broadcast"
	ReasonTimeout   WakeReason = "timeout"
	ReasonCancel    WakeReason = "cancel"
)

// TaskState is the modeled scheduler-visible state of a task.
type TaskState string

const (
	TaskIdle         TaskState = "idle"
	TaskHolding      TaskState = "holding"
	TaskLockBlocked  TaskState = "lock_blocked"
	TaskCondWaiting  TaskState = "cond_waiting"
	TaskReacquiring  TaskState = "reacquiring_lock"
	TaskWaitToResume TaskState = "wait_granted_not_returned"
)

// Status is the non-error outcome of advancing one event.
type Status string

const (
	StatusRegistered    Status = "registered"
	StatusAcquired      Status = "acquired"
	StatusReentered     Status = "reentered"
	StatusBlocked       Status = "blocked"
	StatusStillHeld     Status = "still_held"
	StatusReleased      Status = "released"
	StatusWaitStarted   Status = "wait_started"
	StatusWakeQueued    Status = "wake_queued_for_lock"
	StatusWaitGranted   Status = "wait_lock_granted"
	StatusWaitCompleted Status = "wait_completed"
	StatusNoWaiters     Status = "no_waiters"
	StatusLateEvent     Status = "late_event_ignored"
	StatusCancelPending Status = "cancel_pending"
)

// EventType enumerates the deterministic inputs understood by the model.
type EventType string

const (
	EventRegister   EventType = "register"
	EventLock       EventType = "lock"
	EventUnlock     EventType = "unlock"
	EventGrant      EventType = "grant_next_lock"
	EventWait       EventType = "wait"
	EventFinishWait EventType = "finish_wait"
	EventSignal     EventType = "signal"
	EventBroadcast  EventType = "broadcast"
	EventTimeout    EventType = "timeout"
	EventCancel     EventType = "cancel"
)

// Event is one scheduler stimulus. The zero WaitID means that task-scoped
// cancellation/timeout should target the task's current wait.
type Event struct {
	Type   EventType `json:"type"`
	Task   ID        `json:"task,omitempty"`
	WaitID int       `json:"wait_id,omitempty"`
}

// Result describes a state transition without exposing internal pointers.
type Result struct {
	OK              bool       `json:"ok"`
	Status          Status     `json:"status"`
	Task            ID         `json:"task,omitempty"`
	WaitID          int        `json:"wait_id,omitempty"`
	Reason          WakeReason `json:"reason,omitempty"`
	Depth           int        `json:"depth,omitempty"`
	ReleasedDepth   int        `json:"released_depth,omitempty"`
	QueuePosition   int        `json:"queue_position,omitempty"`
	Woken           []ID       `json:"woken,omitempty"`
	CancelRequested bool       `json:"cancel_requested,omitempty"`
	Late            bool       `json:"late,omitempty"`
	Message         string     `json:"message,omitempty"`
}

// LockEntry is one position in the mutex acquisition queue.
type LockEntry struct {
	Task         ID  `json:"task"`
	RestoreDepth int `json:"restore_depth"`
	WaitID       int `json:"wait_id,omitempty"`
	Sequence     int `json:"sequence"`
}

// WaitEntry is the public view of a condition-variable wait attempt.
type WaitEntry struct {
	WaitID            int        `json:"wait_id"`
	Task              ID         `json:"task"`
	SavedDepth        int        `json:"saved_depth"`
	State             string     `json:"state"`
	Reason            WakeReason `json:"reason,omitempty"`
	LockEnqueueCount  int        `json:"lock_enqueue_count"`
	CancelRequested   bool       `json:"cancel_requested"`
	LockQueuePosition int        `json:"lock_queue_position"`
}

// WaitInfo adds transition timestamps to WaitEntry.
type WaitInfo struct {
	WaitEntry
	CreatedStep   int `json:"created_step"`
	DecidedStep   int `json:"decided_step,omitempty"`
	GrantedStep   int `json:"granted_step,omitempty"`
	CompletedStep int `json:"completed_step,omitempty"`
}

// TaskEntry is the public view of a task.
type TaskEntry struct {
	ID              ID        `json:"id"`
	State           TaskState `json:"state"`
	CancelRequested bool      `json:"cancel_requested"`
	ActiveWaitID    int       `json:"active_wait_id,omitempty"`
	LastWaitID      int       `json:"last_wait_id,omitempty"`
}

// Snapshot is an immutable point-in-time view of owner and both wait queues.
type Snapshot struct {
	Step      int         `json:"step"`
	Owner     ID          `json:"owner"`
	Depth     int         `json:"depth"`
	LockQueue []LockEntry `json:"lock_queue"`
	CondQueue []WaitEntry `json:"cond_queue"`
	Tasks     []TaskEntry `json:"tasks"`
}

// Step is one processed event and the state immediately after that event.
type Step struct {
	Number int      `json:"number"`
	Event  Event    `json:"event"`
	Result Result   `json:"result"`
	Err    string   `json:"error,omitempty"`
	After  Snapshot `json:"after"`
}

// Model errors identify invalid use of the modeled mutex/condition variable.
var (
	ErrEmptyTask      = errors.New("condmodel: empty task id")
	ErrDuplicateTask  = errors.New("condmodel: duplicate task")
	ErrNotOwner       = errors.New("condmodel: task is not the mutex owner")
	ErrLockBusy       = errors.New("condmodel: mutex still has an owner")
	ErrQueueEmpty     = errors.New("condmodel: lock queue is empty")
	ErrAlreadyQueued  = errors.New("condmodel: task already has a lock request")
	ErrInvalidState   = errors.New("condmodel: operation is invalid in current task state")
	ErrWaitNotFound   = errors.New("condmodel: wait attempt not found")
	ErrNoActiveWait   = errors.New("condmodel: task has no unfinished wait")
	ErrWaitNotGranted = errors.New("condmodel: wait has not reacquired the mutex")
)

type waitState string

const (
	waitActive     waitState = "active"
	waitLockQueued waitState = "lock_queued_once"
	waitGranted    waitState = "lock_granted"
	waitCompleted  waitState = "completed"
)

type task struct {
	id              ID
	state           TaskState
	cancelRequested bool
	activeWaitID    int
	lastWaitID      int
}

type lockTicket struct {
	task         ID
	restoreDepth int
	waitID       int
	sequence     int
}

type waitAttempt struct {
	id            int
	task          ID
	savedDepth    int
	state         waitState
	reason        WakeReason
	enqueueCount  int
	createdStep   int
	decidedStep   int
	grantedStep   int
	completedStep int
}

// Scheduler is the deterministic event-driven model.
//
// Its methods are safe for serialized/test use and take an internal mutex so
// Snapshot may be called while another goroutine drives events. Determinism
// still comes from the caller choosing the event order.
type Scheduler struct {
	mu sync.Mutex

	step  int
	owner ID
	depth int

	lockQueue []*lockTicket
	condQueue []*waitAttempt

	tasks     map[ID]*task
	taskOrder []ID

	waits    map[int]*waitAttempt
	nextWait int
	nextSeq  int

	history []Step
	pending []Event
}

// NewScheduler creates an empty unlocked model.
func NewScheduler() *Scheduler {
	return &Scheduler{
		tasks:    make(map[ID]*task),
		waits:    make(map[int]*waitAttempt),
		nextWait: 0,
	}
}

// New is a short alias for NewScheduler.
func New() *Scheduler { return NewScheduler() }
