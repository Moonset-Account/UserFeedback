package coordination

import (
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
)

// ErrNoPredecessor is returned by InstallWatch when the contender is currently
// the smallest node in its queue and therefore has nothing to watch.
var ErrNoPredecessor = errors.New("coordination: no predecessor to watch")

// Phase is the position of a Contender in the lock-acquisition state machine.
//
// A caller drives the protocol one phase at a time with Step, or uses the
// dedicated Enumerate / InstallWatch / TryAcquireLock methods to drive the
// enumeration and watch-installation phases independently. This is what makes
// the "predecessor vanished between listing and watching" race testable.
type Phase int

const (
	// PhaseUnqueued means no ephemeral sequential node has been created yet.
	PhaseUnqueued Phase = iota
	// PhaseEnqueued means the node exists but the queue has not been listed yet.
	PhaseEnqueued
	// PhaseEnumerated means a predecessor is known (or the contender is first).
	PhaseEnumerated
	// PhaseWatching means a one-shot watch is armed on the direct predecessor.
	PhaseWatching
	// PhaseWoken means the predecessor is known to be gone; re-enumeration is due.
	PhaseWoken
	// PhaseHeld means the contender owns the lock.
	PhaseHeld
	// PhaseReleased means the contender deliberately gave up the lock.
	PhaseReleased
	// PhaseLost means the session expired or the contender's node disappeared.
	PhaseLost
)

// String renders a Phase for test failures and logs.
func (p Phase) String() string {
	switch p {
	case PhaseUnqueued:
		return "unqueued"
	case PhaseEnqueued:
		return "enqueued"
	case PhaseEnumerated:
		return "enumerated"
	case PhaseWatching:
		return "watching"
	case PhaseWoken:
		return "woken"
	case PhaseHeld:
		return "held"
	case PhaseReleased:
		return "released"
	case PhaseLost:
		return "lost"
	default:
		return fmt.Sprintf("phase(%d)", int(p))
	}
}

// Contender is one client of the lock protocol. Each contender owns exactly one
// ephemeral sequential node and, while queued, at most one one-shot watch on its
// immediate predecessor.
type Contender struct {
	svc  *Service
	sess string
	// parent is the lock node, e.g. "/locks/job".
	parent string

	mu             sync.Mutex
	node           Node
	phase          Phase
	predecessor    string
	watchID        uint64
	held           bool
	raceRecoveries int
	lastErr        error

	wakeCh chan WatchEvent
	wakes  int64
}

// NewContender creates a contender for one session and one lock parent. The
// session must have been opened on the service.
func NewContender(svc *Service, sessionID, parent string) *Contender {
	return &Contender{
		svc:    svc,
		sess:   sessionID,
		parent: normalizeParent(parent),
		phase:  PhaseUnqueued,
		wakeCh: make(chan WatchEvent, 1),
	}
}

// SessionID returns the session backing this contender.
func (c *Contender) SessionID() string { return c.sess }

// LockParent returns the normalized lock parent path.
func (c *Contender) LockParent() string { return c.parent }

// Node returns the contender's ephemeral node snapshot.
func (c *Contender) Node() Node {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.node
}

// NodePath returns the contender's node path, or "" before enqueueing.
func (c *Contender) NodePath() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.node.Path
}

// Phase returns the current state machine phase.
func (c *Contender) Phase() Phase {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.phase
}

// Predecessor returns the predecessor path the contender last observed.
func (c *Contender) Predecessor() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.predecessor
}

// Fence returns the fencing token (the node sequence number) of this contender.
// It is valid even after the session is lost, which is what allows a stale
// holder to attempt a late write and be rejected by the Resource.
func (c *Contender) Fence() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.node.Sequence
}

// Held reports whether the contender believes it holds the lock.
func (c *Contender) Held() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.held
}

// RaceRecoveries counts how often a watch could not be installed because the
// predecessor disappeared between enumeration and installation.
func (c *Contender) RaceRecoveries() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.raceRecoveries
}

// WakeCount counts watch notifications actually delivered to this contender.
func (c *Contender) WakeCount() int64 { return atomic.LoadInt64(&c.wakes) }

// Wakes exposes the buffered notification channel. Each predecessor removal
// delivers at most one event (the watch is one-shot).
func (c *Contender) Wakes() <-chan WatchEvent { return c.wakeCh }

// Enqueue creates the contender's ephemeral sequential node.
func (c *Contender) Enqueue() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.enqueueLocked()
}

func (c *Contender) enqueueLocked() error {
	switch c.phase {
	case PhaseUnqueued:
	case PhaseLost:
		return fmt.Errorf("%w: %s", ErrSessionExpired, c.sess)
	default:
		return fmt.Errorf("coordination: already enqueued at %s", c.node.Path)
	}
	n, err := c.svc.CreateEphemeralSequential(c.parent, c.sess, "")
	if err != nil {
		c.lastErr = err
		if errors.Is(err, ErrSessionExpired) || errors.Is(err, ErrSessionUnknown) {
			c.phase = PhaseLost
		}
		return err
	}
	c.node = n
	c.phase = PhaseEnqueued
	return nil
}

// Enumerate lists the queue and records the direct predecessor. It is a single
// observable step so a test can expire the predecessor between this call and
// InstallWatch.
func (c *Contender) Enumerate() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.enumerateLocked()
}

func (c *Contender) enumerateLocked() error {
	if c.phase == PhaseLost {
		return fmt.Errorf("%w: %s", ErrSessionExpired, c.sess)
	}
	if c.phase == PhaseUnqueued {
		return errors.New("coordination: cannot enumerate before enqueue")
	}
	if self, ok := c.svc.GetNode(c.node.Path); !ok {
		c.phase = PhaseLost
		err := c.classifyLossLocked()
		c.lastErr = err
		return err
	} else {
		c.node = self
	}
	children := c.svc.Children(c.parent)
	idx := -1
	for i := range children {
		if children[i].Path == c.node.Path {
			idx = i
			break
		}
	}
	if idx == -1 {
		c.phase = PhaseLost
		err := c.classifyLossLocked()
		c.lastErr = err
		return err
	}
	if idx == 0 {
		c.predecessor = ""
	} else {
		c.predecessor = children[idx-1].Path
	}
	c.phase = PhaseEnumerated
	return nil
}

// InstallWatch arms the one-shot watch on the predecessor found by the most
// recent Enumerate. If the predecessor is already gone, WatchNodeMissing is
// returned (nothing is armed), the phase becomes PhaseWoken, and the caller must
// Enumerate again; the next enumeration may produce a different predecessor or
// show that this contender is now first. A WatchNodeMissing result is not an
// error.
func (c *Contender) InstallWatch() (WatchStatus, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.installWatchLocked()
}

func (c *Contender) installWatchLocked() (WatchStatus, error) {
	if c.phase == PhaseLost {
		return WatchNodeMissing, fmt.Errorf("%w: %s", ErrSessionExpired, c.sess)
	}
	if c.phase != PhaseEnumerated {
		return WatchNodeMissing, fmt.Errorf("coordination: install watch in phase %s", c.phase)
	}
	if c.predecessor == "" {
		return WatchNodeMissing, ErrNoPredecessor
	}
	id, status, err := c.svc.WatchPredecessor(c.node.Path, c.predecessor, c.sess, c.onWatch)
	if err != nil {
		c.lastErr = err
		if errors.Is(err, ErrSessionExpired) || errors.Is(err, ErrNodeNotFound) {
			c.phase = PhaseLost
		}
		return status, err
	}
	if status == WatchNodeMissing {
		// The predecessor vanished after enumeration but before the watch was
		// armed. Do not sleep on a node that no longer exists.
		c.raceRecoveries++
		c.phase = PhaseWoken
		return status, nil
	}
	c.watchID = id
	c.phase = PhaseWatching
	return status, nil
}

// TryAcquireLock asks the service atomically whether this contender is still the
// smallest node. On success it records the fencing token and enters PhaseHeld.
// On failure the phase becomes PhaseWoken so the caller re-enumerates.
func (c *Contender) TryAcquireLock() (bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.tryAcquireLocked()
}

func (c *Contender) tryAcquireLocked() (bool, error) {
	ok, err := c.svc.TryAcquire(c.node.Path, c.sess)
	if err != nil {
		c.lastErr = err
		c.phase = PhaseLost
		return false, err
	}
	if !ok {
		c.phase = PhaseWoken
		return false, nil
	}
	if self, found := c.svc.GetNode(c.node.Path); found {
		c.node = self
	}
	c.held = true
	c.phase = PhaseHeld
	c.predecessor = ""
	return true, nil
}

// Step advances the protocol by one phase and reports the phase reached.
//   - unqueued: create the ephemeral sequential node
//   - enqueued: list the queue and pick the predecessor
//   - woken: consume a pending notification, then list the queue again
//   - enumerated: acquire if first, otherwise arm the predecessor watch
//   - watching: consume a notification if one arrived, otherwise no-op
//   - held: verify the lock was not lost (e.g. session expired) and report it
//
// A Step that reaches PhaseWatching reports a blocked contender. Step never
// blocks.
func (c *Contender) Step() (Phase, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	var err error
	switch c.phase {
	case PhaseUnqueued:
		err = c.enqueueLocked()
	case PhaseEnqueued:
		err = c.enumerateLocked()
	case PhaseWoken:
		c.drainWakeLocked()
		err = c.enumerateLocked()
	case PhaseEnumerated:
		if c.predecessor == "" {
			_, err = c.tryAcquireLocked()
		} else {
			var status WatchStatus
			status, err = c.installWatchLocked()
			if err == nil && status == WatchNodeMissing {
				// PhaseWoken is set by installWatchLocked; the next Step
				// re-enumerates. A vanished predecessor is not an error.
			}
		}
	case PhaseWatching:
		if c.drainWakeLocked() {
			c.phase = PhaseWoken
		}
	case PhaseHeld:
		err = c.verifyLocked()
	case PhaseReleased, PhaseLost:
	default:
		err = fmt.Errorf("coordination: unknown phase %d", int(c.phase))
	}
	return c.phase, err
}

// Run calls Step until the contender holds the lock, loses the session, is
// released, or settles into watching a predecessor with no pending wake. A
// pending notification is always consumed, so a wake that arrived before Run is
// not ignored. maxSteps protects against accidental infinite loops. The final
// phase is returned.
func (c *Contender) Run(maxSteps int) (Phase, error) {
	for i := 0; i < maxSteps; i++ {
		phase := c.Phase()
		if phase == PhaseHeld || phase == PhaseLost || phase == PhaseReleased {
			return phase, nil
		}
		if _, err := c.Step(); err != nil {
			return c.Phase(), err
		}
		if c.Phase() == phase && phase == PhaseWatching {
			// Watching with nothing to consume: blocked, not an error.
			return phase, nil
		}
	}
	return c.Phase(), errors.New("coordination: step limit exceeded")
}

// Release gives up the lock by deleting the contender's own node. Watchers on
// that node are notified; no other waiters are touched.
func (c *Contender) Release() error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.phase == PhaseLost {
		return fmt.Errorf("%w: %s", ErrSessionExpired, c.sess)
	}
	if c.node.Path == "" {
		return errors.New("coordination: contender has no node to release")
	}
	if c.watchID != 0 {
		c.svc.Unwatch(c.watchID)
		c.watchID = 0
	}
	if err := c.svc.DeleteNode(c.node.Path, c.sess); err != nil {
		c.lastErr = err
		if errors.Is(err, ErrSessionExpired) || errors.Is(err, ErrNodeNotFound) {
			c.phase = PhaseLost
		}
		return err
	}
	c.held = false
	c.predecessor = ""
	c.phase = PhaseReleased
	return nil
}

// verifyLocked detects a lock that was silently lost, for example because the
// session expired while this client was idle. It models the moment a stale
// holder finally learns that its session is gone.
func (c *Contender) verifyLocked() error {
	if err := c.svc.checkSession(c.sess); err != nil {
		c.held = false
		c.phase = PhaseLost
		c.lastErr = err
		return err
	}
	if _, ok := c.svc.GetNode(c.node.Path); !ok {
		c.held = false
		c.phase = PhaseLost
		err := fmt.Errorf("%w: %s", ErrNodeNotFound, c.node.Path)
		c.lastErr = err
		return err
	}
	return nil
}

func (c *Contender) classifyLossLocked() error {
	if err := c.svc.checkSession(c.sess); err != nil {
		return err
	}
	return fmt.Errorf("%w: %s", ErrNodeNotFound, c.node.Path)
}

func (c *Contender) drainWakeLocked() bool {
	got := false
	for {
		select {
		case <-c.wakeCh:
			got = true
		default:
			return got
		}
	}
}

// onWatch is the one-shot callback. It must not take c.mu: the service invokes
// callbacks while other contenders may hold their own contender lock, and the
// callback only needs to record the event and wake the channel.
func (c *Contender) onWatch(ev WatchEvent) {
	atomic.AddInt64(&c.wakes, 1)
	select {
	case c.wakeCh <- ev:
	default:
		// One-shot per armed watch; if buffer is full the wake is still observed
		// through WakeCount and the draining Step.
	}
}
