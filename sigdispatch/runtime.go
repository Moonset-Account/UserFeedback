// Package sigdispatch implements an in-process, user-space signal
// dispatch model for a threaded runtime.
//
// The model mirrors the core POSIX.1-2008 signal semantics without
// touching any host operating-system signal machinery:
//
//   - Signals can be directed at the whole process or at one thread.
//   - The process and every thread each own an independent pending set.
//   - Standard (non real-time) signals coalesce by signal number: only
//     the payload of the first undelivered occurrence is retained.
//   - Real-time signals queue every occurrence in arrival order.
//   - A thread explicitly taking a signal selects the smallest,
//     unblocked pending signal number; for an equal number the
//     thread-private pending item wins over the process-wide one,
//     and a process-wide item is handed to exactly one thread.
//   - Entering a handler temporarily blocks the signal itself plus the
//     handler's declared mask, while other signals remain allowed and
//     can nest; the previous mask is restored on return.
package sigdispatch

import (
	"sort"
	"sync"
)

// Sig identifies a signal by its number.
type Sig int

// RTMin is the smallest real-time signal number in this model.
// Numbers >= RTMin queue (every occurrence is retained); smaller
// numbers are standard signals that coalesce.
const RTMin Sig = 32

// IsRT reports whether s is a real-time (queuing) signal.
func (s Sig) IsRT() bool { return s >= RTMin }

// Payload is the data carried by one signal occurrence.
type Payload struct {
	// Seq is assigned in arrival order and lets tests identify which
	// concrete occurrence of a coalesced/queued signal survives.
	Seq  int
	Code int
	Data any
}

// HandlerFunc is a signal handler. It runs synchronously on the
// thread that dispatches the signal and is allowed to dispatch
// further signals to produce nested handler frames.
type HandlerFunc func(t *Thread, s Sig, p Payload)

// Handler describes a signal handler and its additional block mask.
type Handler struct {
	// Fn is invoked for the signal. A nil Fn means the signal is
	// accepted (and removed from pending) without user action.
	Fn HandlerFunc
	// Mask lists extra signals blocked while the handler runs. The
	// signal itself is blocked automatically and need not be listed.
	Mask []Sig
}

// Runtime is the shared signal runtime: it owns the process-wide
// pending set, the handler table and the set of threads.
type Runtime struct {
	mu       sync.Mutex
	cond     *sync.Cond
	nextSeq  int
	process  pendingSet
	handlers map[Sig]Handler
	threads  map[int]*Thread
	nextTID  int
}

// NewRuntime creates an empty runtime with no installed handlers.
func NewRuntime() *Runtime {
	r := &Runtime{
		process:  newPendingSet(),
		handlers: make(map[Sig]Handler),
		threads:  make(map[int]*Thread),
	}
	r.cond = sync.NewCond(&r.mu)
	return r
}

// SetHandler installs or replaces the handler for s.
func (r *Runtime) SetHandler(s Sig, h Handler) {
	r.mu.Lock()
	r.handlers[s] = h
	r.mu.Unlock()
}

// NewThread creates a thread with an empty (all signals unblocked) mask.
func (r *Runtime) NewThread() *Thread {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.nextTID++
	t := &Thread{
		id:      r.nextTID,
		rt:      r,
		pending: newPendingSet(),
		mask:    make(map[Sig]int),
	}
	r.threads[t.id] = t
	return t
}

// removeThread deletes a terminated thread from the runtime table.
// Its private pending set is discarded; it is never merged back into
// the process-wide pending set.
func (r *Runtime) removeThread(id int) {
	r.mu.Lock()
	delete(r.threads, id)
	r.mu.Unlock()
}

// SendProcess directs a signal at the whole process. Standard signals
// coalesce; real-time signals queue.
func (r *Runtime) SendProcess(s Sig, code int, data any) Payload {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := r.enqueueLocked(&r.process, s, code, data)
	r.cond.Broadcast()
	return p
}

// ProcessPending returns a snapshot view of the process-wide pending
// signals. Standard signals report at most one entry; real-time
// signals report one entry per queued occurrence, in arrival order.
func (r *Runtime) ProcessPending() PendingView {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.process.view()
}

// enqueueLocked appends one occurrence and returns the stored payload.
// Caller must hold r.mu.
func (r *Runtime) enqueueLocked(set *pendingSet, s Sig, code int, data any) Payload {
	r.nextSeq++
	p := Payload{Seq: r.nextSeq, Code: code, Data: data}
	set.add(s, p)
	return p
}

// selectLocked chooses the smallest unblocked signal available to t
// from its private pending set and the process-wide pending set.
// For an equal signal number the thread-private item is preferred.
// It returns ok == false when nothing deliverable is pending.
// Caller must hold r.mu.
func (r *Runtime) selectLocked(t *Thread) (s Sig, p Payload, ok bool) {
	candidates := make(map[Sig]struct{})
	for s := range t.pending.standard {
		candidates[s] = struct{}{}
	}
	for s := range r.process.standard {
		candidates[s] = struct{}{}
	}
	for s := range t.pending.realtime {
		candidates[s] = struct{}{}
	}
	for s := range r.process.realtime {
		candidates[s] = struct{}{}
	}
	if len(candidates) == 0 {
		return 0, Payload{}, false
	}
	nums := make([]int, 0, len(candidates))
	for s := range candidates {
		nums = append(nums, int(s))
	}
	sort.Ints(nums)
	for _, n := range nums {
		sig := Sig(n)
		if t.blockedLocked(sig) {
			continue
		}
		// Thread-private entries take precedence on equal numbers.
		if p, ok := t.pending.remove(sig); ok {
			return sig, p, true
		}
		if p, ok := r.process.remove(sig); ok {
			return sig, p, true
		}
	}
	return 0, Payload{}, false
}
