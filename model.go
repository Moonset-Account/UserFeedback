// Package pimutex implements an in-memory model of a priority-inheritance
// mutex for a fixed set of simulated real-time threads and locks.
//
// A thread has a base priority; larger integers mean higher priority. The
// effective priority of a thread is the maximum of its base priority and the
// effective priorities of every thread waiting on a lock it currently holds.
// Because the caller guarantees that wait-for graphs contain no cycles, this
// definition is well founded and the resulting priority propagates along a
// wait chain. Effective priorities are recomputed from the current state on
// every query, so a change (a new waiter, a cancelled wait, or a base-priority
// update) is visible on every link of the chain immediately.
//
// A lock has at most one owner. A thread may hold several locks at once, but
// it may wait on at most one lock at a time. Locks are not recursive: a
// thread that already owns a lock cannot acquire it again. When an owner
// releases a lock, the lock is handed to the waiting thread with the highest
// effective priority; ties are broken by queue order, so the earliest waiter
// wins.
package pimutex

import (
	"errors"
	"fmt"
	"sort"
	"sync"
)

// ThreadID identifies a simulated thread.
type ThreadID string

// LockID identifies a mutex in the model.
type LockID string

// Sentinel errors returned by Model methods. They are wrapped with context,
// so callers should test them with errors.Is.
var (
	ErrInvalidID       = errors.New("pimutex: identifier must not be empty")
	ErrDuplicateThread = errors.New("pimutex: thread already registered")
	ErrDuplicateLock   = errors.New("pimutex: lock already registered")
	ErrUnknownThread   = errors.New("pimutex: unknown thread")
	ErrUnknownLock     = errors.New("pimutex: unknown lock")
	ErrAlreadyOwned    = errors.New("pimutex: thread already owns lock")
	ErrThreadWaiting   = errors.New("pimutex: thread already waits on a lock")
	ErrNotOwner        = errors.New("pimutex: thread does not own lock")
	ErrNotWaiting      = errors.New("pimutex: thread is not waiting on a lock")
	ErrWouldDeadlock   = errors.New("pimutex: acquisition would create a wait cycle")
)

// AcquireOutcome reports what happened when a thread called Acquire.
type AcquireOutcome uint8

const (
	// Acquired means the lock was free and the caller now owns it.
	Acquired AcquireOutcome = iota
	// Waiting means the lock was held and the caller joined its wait queue.
	Waiting
)

func (o AcquireOutcome) String() string {
	switch o {
	case Acquired:
		return "acquired"
	case Waiting:
		return "waiting"
	default:
		return fmt.Sprintf("AcquireOutcome(%d)", uint8(o))
	}
}

type thread struct {
	id      ThreadID
	base    int
	waiting *lock   // non-nil exactly when the thread is queued on a lock
	held    []*lock // locks owned by this thread, in acquisition order
}

type lock struct {
	id      LockID
	owner   *thread
	waiters []*thread // FIFO: earliest waiter first
}

// Model is a priority-inheritance mutex model. All methods are safe for
// concurrent use; the internal mutex serializes access to the graph.
type Model struct {
	mu      sync.Mutex
	threads map[ThreadID]*thread
	locks   map[LockID]*lock
}

// New returns an empty model with no threads and no locks.
func New() *Model {
	return &Model{
		threads: make(map[ThreadID]*thread),
		locks:   make(map[LockID]*lock),
	}
}

// AddThread registers a thread with the given base priority. Larger values
// mean higher priority. Identifiers must be non-empty and unique.
func (m *Model) AddThread(id ThreadID, base int) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if id == "" {
		return fmt.Errorf("%w: thread", ErrInvalidID)
	}
	if _, exists := m.threads[id]; exists {
		return fmt.Errorf("%w: %q", ErrDuplicateThread, id)
	}
	m.threads[id] = &thread{id: id, base: base}
	return nil
}

// AddLock registers a lock. Identifiers must be non-empty and unique.
func (m *Model) AddLock(id LockID) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if id == "" {
		return fmt.Errorf("%w: lock", ErrInvalidID)
	}
	if _, exists := m.locks[id]; exists {
		return fmt.Errorf("%w: %q", ErrDuplicateLock, id)
	}
	m.locks[id] = &lock{id: id}
	return nil
}

// Acquire asks thread id to acquire lock lockID.
//
// If the lock is free the thread becomes its owner and Acquired is returned.
// If another thread owns the lock, the caller is appended to the lock's wait
// queue and Waiting is returned. A thread that already waits on a lock, or
// that already owns the requested lock, is rejected. An acquisition that
// would close a wait-for cycle is rejected with ErrWouldDeadlock (the model
// itself never needs to create such a cycle because callers guarantee
// acyclic input, but the check keeps an accidental cycle from corrupting the
// model).
func (m *Model) Acquire(id ThreadID, lockID LockID) (AcquireOutcome, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	t, err := m.lookupThread(id)
	if err != nil {
		return 0, err
	}
	l, err := m.lookupLock(lockID)
	if err != nil {
		return 0, err
	}
	if t.waiting != nil {
		return 0, fmt.Errorf("%w: thread %q waits on %q", ErrThreadWaiting, id, t.waiting.id)
	}
	if l.owner == t {
		return 0, fmt.Errorf("%w: thread %q owns %q", ErrAlreadyOwned, id, lockID)
	}
	if l.owner == nil {
		l.owner = t
		t.held = append(t.held, l)
		return Acquired, nil
	}
	if m.wouldCreateCycle(t, l) {
		return 0, fmt.Errorf("%w: thread %q waiting on %q held by %q", ErrWouldDeadlock, id, lockID, l.owner.id)
	}
	l.waiters = append(l.waiters, t)
	t.waiting = l
	return Waiting, nil
}

// Release releases lockID, which must be owned by thread id.
//
// If waiters exist, the lock is handed to the waiter with the highest
// effective priority at the time of the release. Equal effective priorities
// are resolved in queue order: the earliest waiter is chosen. The chosen
// waiter stops waiting (its wait-for edge disappears) and becomes the owner.
func (m *Model) Release(id ThreadID, lockID LockID) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	t, err := m.lookupThread(id)
	if err != nil {
		return err
	}
	l, err := m.lookupLock(lockID)
	if err != nil {
		return err
	}
	if l.owner != t {
		owner := ThreadID("")
		if l.owner != nil {
			owner = l.owner.id
		}
		return fmt.Errorf("%w: thread %q does not own %q (owner %q)", ErrNotOwner, id, lockID, owner)
	}

	t.held = removeLock(t.held, l)
	l.owner = nil

	if len(l.waiters) > 0 {
		winner := m.highestPriorityWaiter(l)
		l.waiters = removeThread(l.waiters, winner)
		winner.waiting = nil
		l.owner = winner
		winner.held = append(winner.held, l)
	}
	return nil
}

// CancelWait removes thread id from the wait queue it is currently on. It
// returns the lock the thread was waiting for. The thread's own base
// priority and any locks it still holds are unchanged; only the wait-for
// edge disappears, so effective priorities above it fall back immediately.
func (m *Model) CancelWait(id ThreadID) (LockID, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	t, err := m.lookupThread(id)
	if err != nil {
		return "", err
	}
	if t.waiting == nil {
		return "", fmt.Errorf("%w: thread %q", ErrNotWaiting, id)
	}
	l := t.waiting
	l.waiters = removeThread(l.waiters, t)
	t.waiting = nil
	return l.id, nil
}

// SetBasePriority changes a thread's base priority. Effective priorities that
// depend on this thread are recomputed by subsequent queries.
func (m *Model) SetBasePriority(id ThreadID, base int) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	t, err := m.lookupThread(id)
	if err != nil {
		return err
	}
	t.base = base
	return nil
}

// BasePriority returns the thread's own (non-inherited) priority.
func (m *Model) BasePriority(id ThreadID) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	t, err := m.lookupThread(id)
	if err != nil {
		return 0, err
	}
	return t.base, nil
}

// EffectivePriority returns a thread's priority after inheritance: the
// maximum of its base priority and the effective priorities of all threads
// waiting on any lock it holds, computed recursively along the wait chain.
func (m *Model) EffectivePriority(id ThreadID) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	t, err := m.lookupThread(id)
	if err != nil {
		return 0, err
	}
	return m.effective(t, make(map[*thread]int), make(map[*thread]bool)), nil
}

// EffectivePriorities returns a snapshot of the effective priority of every
// registered thread.
func (m *Model) EffectivePriorities() map[ThreadID]int {
	m.mu.Lock()
	defer m.mu.Unlock()

	memo := make(map[*thread]int, len(m.threads))
	visiting := make(map[*thread]bool)
	out := make(map[ThreadID]int, len(m.threads))
	for id, t := range m.threads {
		out[id] = m.effective(t, memo, visiting)
	}
	return out
}

// Owner returns the current owner of lockID. held is false when the lock is
// free. An unregistered lock yields ErrUnknownLock.
func (m *Model) Owner(lockID LockID) (owner ThreadID, held bool, err error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	l, err := m.lookupLock(lockID)
	if err != nil {
		return "", false, err
	}
	if l.owner == nil {
		return "", false, nil
	}
	return l.owner.id, true, nil
}

// Owners returns a snapshot mapping each held lock to its owner. Free locks
// are omitted; use Owner to test an individual lock.
func (m *Model) Owners() map[LockID]ThreadID {
	m.mu.Lock()
	defer m.mu.Unlock()

	out := make(map[LockID]ThreadID, len(m.locks))
	for id, l := range m.locks {
		if l.owner != nil {
			out[id] = l.owner.id
		}
	}
	return out
}

// Waiters returns the wait queue of lockID in queue order, earliest first.
func (m *Model) Waiters(lockID LockID) ([]ThreadID, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	l, err := m.lookupLock(lockID)
	if err != nil {
		return nil, err
	}
	out := make([]ThreadID, len(l.waiters))
	for i, t := range l.waiters {
		out[i] = t.id
	}
	return out, nil
}

// WaitingOn reports whether a thread is waiting and, if so, on which lock.
func (m *Model) WaitingOn(id ThreadID) (lockID LockID, waiting bool, err error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	t, err := m.lookupThread(id)
	if err != nil {
		return "", false, err
	}
	if t.waiting == nil {
		return "", false, nil
	}
	return t.waiting.id, true, nil
}

// HeldLocks returns the locks owned by a thread in acquisition order.
func (m *Model) HeldLocks(id ThreadID) ([]LockID, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	t, err := m.lookupThread(id)
	if err != nil {
		return nil, err
	}
	out := make([]LockID, len(t.held))
	for i, l := range t.held {
		out[i] = l.id
	}
	return out, nil
}

// Threads returns all registered thread identifiers in sorted order.
func (m *Model) Threads() []ThreadID {
	m.mu.Lock()
	defer m.mu.Unlock()

	out := make([]ThreadID, 0, len(m.threads))
	for id := range m.threads {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// Locks returns all registered lock identifiers in sorted order.
func (m *Model) Locks() []LockID {
	m.mu.Lock()
	defer m.mu.Unlock()

	out := make([]LockID, 0, len(m.locks))
	for id := range m.locks {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func (m *Model) lookupThread(id ThreadID) (*thread, error) {
	t, ok := m.threads[id]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrUnknownThread, id)
	}
	return t, nil
}

func (m *Model) lookupLock(id LockID) (*lock, error) {
	l, ok := m.locks[id]
	if !ok {
		return nil, fmt.Errorf("%w: %q", ErrUnknownLock, id)
	}
	return l, nil
}

// effective computes a thread's inherited priority. memo caches results for
// the duration of a public call; visiting is a defensive guard that cannot
// trigger while the no-cycle invariant holds.
func (m *Model) effective(t *thread, memo map[*thread]int, visiting map[*thread]bool) int {
	if v, ok := memo[t]; ok {
		return v
	}
	if visiting[t] {
		return t.base
	}
	visiting[t] = true
	best := t.base
	for _, l := range t.held {
		for _, waiter := range l.waiters {
			if v := m.effective(waiter, memo, visiting); v > best {
				best = v
			}
		}
	}
	delete(visiting, t)
	memo[t] = best
	return best
}

// highestPriorityWaiter chooses the waiter with the greatest effective
// priority. Scanning in queue order and replacing only on a strictly greater
// priority makes an equal-priority tie go to the earliest waiter.
func (m *Model) highestPriorityWaiter(l *lock) *thread {
	memo := make(map[*thread]int)
	visiting := make(map[*thread]bool)
	var winner *thread
	best := 0
	for i, waiter := range l.waiters {
		p := m.effective(waiter, memo, visiting)
		if i == 0 || p > best {
			winner = waiter
			best = p
		}
	}
	return winner
}

// wouldCreateCycle reports whether making want wait on a lock held by its
// owner chain would reach waiting. The check walks owner -> owner.waiting ->
// owner, which is the wait-for chain, and is defensive: valid input has no
// cycles.
func (m *Model) wouldCreateCycle(waiting *thread, want *lock) bool {
	seen := make(map[*thread]bool)
	for cur := want.owner; cur != nil; {
		if cur == waiting {
			return true
		}
		if seen[cur] {
			return true
		}
		seen[cur] = true
		if cur.waiting == nil {
			return false
		}
		cur = cur.waiting.owner
	}
	return false
}

func removeThread(s []*thread, target *thread) []*thread {
	for i, v := range s {
		if v == target {
			return append(s[:i], s[i+1:]...)
		}
	}
	return s
}

func removeLock(s []*lock, target *lock) []*lock {
	for i, v := range s {
		if v == target {
			return append(s[:i], s[i+1:]...)
		}
	}
	return s
}
