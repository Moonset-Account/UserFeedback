// Package readymux implements a readiness multiplexer that is driven by
// logical readiness events rather than by operating-system file descriptors.
//
// A runtime registers resources together with the read/write conditions it
// cares about, chooses level-triggered or edge-triggered delivery, and may ask
// for a one-shot subscription that disarms itself after the first delivery.
// The resource side reports its current readiness through SetReady, and the
// polling side collects at most a caller-supplied number of events through
// Poll.
//
// Level-triggered subscriptions are kept in a FIFO rotation, so a resource
// that is permanently ready cannot starve subscriptions registered after it.
// Edge-triggered subscriptions latch only bits that newly become ready and
// deliver them once. Removing a resource immediately allows its numeric ID to
// be reused; queue entries left behind by the old registration are rejected
// through a per-registration generation stamp so they can never be reported
// for the new object.
package readymux

import (
	"errors"
	"fmt"
	"strings"
	"sync"
)

// Events is a bit set describing the readiness of a resource.
type Events uint32

const (
	// Readable reports that a resource can currently make progress on reads.
	Readable Events = 1 << iota
	// Writable reports that a resource can currently make progress on writes.
	Writable
)

const allEvents = Readable | Writable

// String renders the set with readable names, for example
// "readable|writable". Unknown bits are rendered in hexadecimal.
func (e Events) String() string {
	if e == 0 {
		return "none"
	}
	names := make([]string, 0, 2)
	if e&Readable != 0 {
		names = append(names, "readable")
	}
	if e&Writable != 0 {
		names = append(names, "writable")
	}
	if unknown := e &^ allEvents; unknown != 0 {
		names = append(names, fmt.Sprintf("0x%x", uint32(unknown)))
	}
	return strings.Join(names, "|")
}

// Mode selects how a subscription turns readiness state into events.
type Mode uint8

const (
	// Level reports a subscription on every poll for as long as it remains
	// ready.
	Level Mode = iota
	// Edge reports only bits that newly became ready since the previous
	// observation of the resource.
	Edge
)

// String implements fmt.Stringer.
func (m Mode) String() string {
	switch m {
	case Level:
		return "level"
	case Edge:
		return "edge"
	default:
		return fmt.Sprintf("mode(%d)", uint8(m))
	}
}

var (
	// ErrDuplicateID is returned when a resource ID is registered twice while
	// the first registration is still active.
	ErrDuplicateID = errors.New("readymux: resource id already registered")
	// ErrNotRegistered is returned when an operation names an ID that is not
	// currently registered.
	ErrNotRegistered = errors.New("readymux: resource id not registered")
	// ErrInvalidInterest is returned when an interest mask is empty or
	// contains bits outside Readable|Writable.
	ErrInvalidInterest = errors.New("readymux: interest must contain Readable and/or Writable")
	// ErrInvalidMode is returned when a subscription mode is neither Level nor
	// Edge.
	ErrInvalidMode = errors.New("readymux: mode must be Level or Edge")
	// ErrInvalidEvents is returned when a readiness state contains bits
	// outside Readable|Writable.
	ErrInvalidEvents = errors.New("readymux: readiness contains unknown bits")
)

// Subscription describes one registered resource.
type Subscription struct {
	// ID identifies the resource. IDs only need to be unique among active
	// subscriptions; an ID may be reused immediately after Remove.
	ID int
	// Interest is the set of readiness bits the caller wants to observe. It
	// must contain Readable and/or Writable.
	Interest Events
	// Mode selects level-triggered or edge-triggered delivery.
	Mode Mode
	// OneShot disarms the subscription after its first delivered event. Use
	// Rearm to observe the resource again; re-arming re-reads the current
	// state, so a resource that is already ready is reported again.
	OneShot bool
}

// Event is one poll result: the resource ID together with the subset of its
// interest mask that is being reported.
type Event struct {
	ID     int
	Events Events
}

// String implements fmt.Stringer.
func (e Event) String() string {
	return fmt.Sprintf("%d:%s", e.ID, e.Events)
}

// Mux is a readiness multiplexer. Its zero value is ready to use, and all
// methods are safe for concurrent use by multiple goroutines.
type Mux struct {
	mu      sync.Mutex
	regs    map[int]*registration
	queue   []queuedRef
	nextGen uint64
}

// registration is the per-resource state owned by the Mux. A new
// registration (and therefore a new generation stamp) is created whenever an
// ID is added, so queued references from an earlier registration can be
// recognised and discarded.
type registration struct {
	id       int
	gen      uint64
	interest Events
	mode     Mode
	oneShot  bool
	armed    bool
	active   bool
	queued   bool
	state    Events // latest readiness reported by the resource side
	pending  Events // edge transitions latched but not yet delivered
}

// queuedRef is a placeholder in the ready queue. It carries the generation it
// was created for; lookups that find a different generation discard it.
type queuedRef struct {
	id  int
	gen uint64
}

// New returns an empty Mux.
func New() *Mux {
	return &Mux{regs: make(map[int]*registration)}
}

// Add registers a resource. It fails with ErrDuplicateID if the ID is already
// active, ErrInvalidInterest if the interest mask is empty or has unknown
// bits, and ErrInvalidMode if the mode is invalid. A newly registered
// subscription is armed and starts from a not-ready state; report readiness
// with SetReady.
func (m *Mux) Add(sub Subscription) error {
	if sub.Interest == 0 || sub.Interest&^allEvents != 0 {
		return ErrInvalidInterest
	}
	if sub.Mode != Level && sub.Mode != Edge {
		return ErrInvalidMode
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	if m.regs == nil {
		m.regs = make(map[int]*registration)
	}
	if _, exists := m.regs[sub.ID]; exists {
		return ErrDuplicateID
	}
	m.nextGen++
	m.regs[sub.ID] = &registration{
		id:       sub.ID,
		gen:      m.nextGen,
		interest: sub.Interest,
		mode:     sub.Mode,
		oneShot:  sub.OneShot,
		armed:    true,
		active:   true,
	}
	return nil
}

// Remove unregisters a resource. Its ID becomes available for immediate
// reuse, and any queued events belonging to the removed registration are
// discarded, so they cannot be delivered to a later registration that reuses
// the same ID.
func (m *Mux) Remove(id int) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	r, ok := m.regs[id]
	if !ok {
		return ErrNotRegistered
	}
	r.active = false
	delete(m.regs, id)
	m.dropQueued(id, r.gen)
	return nil
}

// SetReady reports the current readiness state of a registered resource. The
// state is absolute: bits missing from ready are considered not ready.
//
// For a level subscription the registration is scheduled whenever the state
// intersects its interest mask. For an edge subscription only bits that
// changed from not-ready to ready generate a queued event. Disarmed one-shot
// subscriptions keep their latest state but do not schedule events; Rearm
// re-observes that state.
func (m *Mux) SetReady(id int, ready Events) error {
	if ready&^allEvents != 0 {
		return ErrInvalidEvents
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	r, ok := m.regs[id]
	if !ok {
		return ErrNotRegistered
	}
	previous := r.state
	r.state = ready
	if !r.armed {
		return nil
	}

	if r.mode == Edge {
		newly := ready &^ previous & r.interest
		if newly != 0 {
			r.pending |= newly
			m.enqueue(r)
		}
		return nil
	}
	if ready&r.interest != 0 {
		m.enqueue(r)
	}
	return nil
}

// Rearm re-arms a subscription, typically a one-shot subscription that was
// disarmed by a delivery. Re-arming re-observes the current readiness state:
// if the resource is already ready, it is queued again even though no new
// transition occurred.
func (m *Mux) Rearm(id int) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	r, ok := m.regs[id]
	if !ok {
		return ErrNotRegistered
	}
	r.armed = true
	current := r.state & r.interest
	if current == 0 {
		return nil
	}
	if r.mode == Edge {
		r.pending |= current
	}
	m.enqueue(r)
	return nil
}

// Poll returns at most maxEvents pending events in delivery order. A
// non-positive limit returns nil without consuming anything.
//
// Level subscriptions that are still ready after being reported are appended
// to the back of the ready queue, so a permanently ready resource rotates
// behind other ready resources instead of monopolising the limited result
// set. Edge subscriptions report their latched transitions once. Each
// registration contributes at most one event to a single poll.
func (m *Mux) Poll(maxEvents int) []Event {
	m.mu.Lock()
	defer m.mu.Unlock()

	if maxEvents <= 0 || len(m.queue) == 0 {
		return nil
	}

	capacity := len(m.queue)
	if maxEvents < capacity {
		capacity = maxEvents
	}
	out := make([]Event, 0, capacity)
	var again []*registration

	for len(m.queue) > 0 && len(out) < maxEvents {
		ref := m.queue[0]
		m.queue = m.queue[1:]

		r, ok := m.regs[ref.id]
		if !ok || !r.active || r.gen != ref.gen || !r.armed {
			// A queued entry from a removed or replaced registration, or one
			// belonging to a subscription that is currently disarmed.
			continue
		}
		r.queued = false

		var mask Events
		if r.mode == Edge {
			mask = r.pending & r.interest
			r.pending &^= mask
		} else {
			mask = r.state & r.interest
		}
		if mask == 0 {
			continue
		}

		out = append(out, Event{ID: r.id, Events: mask})
		if r.oneShot {
			r.armed = false
			r.pending = 0
			continue
		}
		if r.mode == Level && r.state&r.interest != 0 {
			again = append(again, r)
		}
	}

	// Re-queue only after the loop so a level subscription cannot be reported
	// twice in the same poll.
	for _, r := range again {
		m.enqueue(r)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func (m *Mux) enqueue(r *registration) {
	if r.queued || !r.active || !r.armed {
		return
	}
	r.queued = true
	m.queue = append(m.queue, queuedRef{id: r.id, gen: r.gen})
}

func (m *Mux) dropQueued(id int, gen uint64) {
	if len(m.queue) == 0 {
		return
	}
	kept := m.queue[:0]
	for _, ref := range m.queue {
		if ref.id == id && ref.gen == gen {
			continue
		}
		kept = append(kept, ref)
	}
	m.queue = kept
}
