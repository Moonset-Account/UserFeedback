package rcu

import (
	"errors"
	"fmt"
	"sort"
)

// Errors returned by Apply and Restore. Apply never changes state when it
// returns a non-nil error, so a rejected event is not appended to the log.
var (
	ErrEmptyThread       = errors.New("rcu: empty thread id")
	ErrEmptyCallbackID   = errors.New("rcu: empty callback id")
	ErrNegativeCPU       = errors.New("rcu: negative cpu id")
	ErrUnknownKind       = errors.New("rcu: unknown event kind")
	ErrNotReading        = errors.New("rcu: thread is not inside a read section")
	ErrDuplicateCallback = errors.New("rcu: callback already registered")
	ErrUnknownCallback   = errors.New("rcu: callback is not registered")
	ErrBadSnapshot       = errors.New("rcu: invalid snapshot")
)

// readerState is one reader instance. A reader instance is created when a
// thread enters its read section from depth zero and retired when the depth
// returns to zero. Nesting does not create a new instance and does not change
// the generation, and migration does not change the identity.
type readerState struct {
	id      uint64 // reader instance id, unique for the life of the coordinator
	thread  string
	gen     uint64 // generation of the outermost EnterRead
	nesting int
	cpu     int
	hasCPU  bool
}

// callbackState is one registered reclaim callback. Waiters holds reader
// instances that were already inside their read section when the callback was
// registered; readers that start afterwards never join it.
type callbackState struct {
	id       string
	source   string
	order    int64 // global registration order, used for delivery and reports
	seq      int64 // sequence number of the registration event
	waiters  map[uint64]struct{}
	graceSeq int64 // event seq at which waiters became empty, 0 while pending
	effSeq   int64 // event seq at which the callback was handed to the reclaimer
	ackSeq   int64 // event seq of the first accepted ack, 0 if never acked
	acked    bool
}

func (c *callbackState) graceSatisfied() bool { return len(c.waiters) == 0 }

// cpuState records CPU lifecycle announcements. It exists so those events are
// replayed faithfully; it never influences which readers block a callback.
type cpuState struct {
	online   bool
	quiesSeq int64
}

// Coordinator is a replayable RCU grace-period coordinator. Apply events one
// at a time in order; the coordinator is deterministic for a given event
// sequence. It is not safe for concurrent use: callers serialise replay, which
// is what makes the event log a faithful record.
type Coordinator struct {
	nextSeq        int64
	generation     uint64
	nextReaderID   uint64
	nextOrder      int64
	readers        map[string]*readerState // open read sections only, by thread
	readersByID    map[uint64]*readerState
	callbacks      map[string]*callbackState
	callbacksByID  map[uint64]*callbackState
	bySource       map[string][]*callbackState
	trackedThreads map[string]int // last known CPU per thread
	cpus           map[int]*cpuState
	log            []LoggedEvent
}

// New returns an empty coordinator with no readers, callbacks or CPUs.
func New() *Coordinator {
	return &Coordinator{
		readers:        make(map[string]*readerState),
		readersByID:    make(map[uint64]*readerState),
		callbacks:      make(map[string]*callbackState),
		callbacksByID:  make(map[uint64]*callbackState),
		bySource:       make(map[string][]*callbackState),
		trackedThreads: make(map[string]int),
		cpus:           make(map[int]*cpuState),
	}
}

// Generation returns the number of outermost read-side entries observed so
// far. Each such entry starts a new generation.
func (c *Coordinator) Generation() uint64 { return c.generation }

// LastSeq returns the sequence number of the most recently accepted event.
func (c *Coordinator) LastSeq() int64 { return c.nextSeq }

// Log returns a copy of every accepted event in application order. Events that
// Apply rejected are absent, so replaying the log into a fresh coordinator
// reproduces the same state.
func (c *Coordinator) Log() []LoggedEvent {
	out := make([]LoggedEvent, len(c.log))
	copy(out, c.log)
	return out
}

// Apply validates e, then applies it and returns what changed. On error the
// coordinator is unchanged and e is not logged.
func (c *Coordinator) Apply(e Event) (Outcome, error) {
	if err := c.validate(e); err != nil {
		return Outcome{}, err
	}
	c.nextSeq++
	seq := c.nextSeq
	var delivered []string

	switch e.Kind {
	case KindEnterRead:
		c.enterRead(e.Thread)
	case KindExitRead:
		c.exitRead(e.Thread)
		delivered = c.deliver()
	case KindMigrate:
		c.migrate(e.Thread, e.CPU)
	case KindCPUOnline:
		c.cpuState(e.CPU).online = true
	case KindCPUOffline:
		c.cpuState(e.CPU).online = false
	case KindCPUQuiescent:
		c.cpuState(e.CPU).quiesSeq = seq
	case KindRegisterCallback:
		c.register(e.Callback, e.Source, seq)
		delivered = c.deliver()
	case KindAckCallback:
		cb := c.callbacks[e.Callback]
		if !cb.acked {
			cb.acked = true
			cb.ackSeq = seq
		}
	}

	c.log = append(c.log, LoggedEvent{Seq: seq, Event: e})
	return Outcome{Seq: seq, Kind: e.Kind, BecameEffective: delivered}, nil
}

func (c *Coordinator) validate(e Event) error {
	switch e.Kind {
	case KindEnterRead, KindExitRead:
		if e.Thread == "" {
			return ErrEmptyThread
		}
		if e.Kind == KindExitRead {
			r, ok := c.readers[e.Thread]
			if !ok {
				return fmt.Errorf("%w: %q", ErrNotReading, e.Thread)
			}
			_ = r
		}
	case KindMigrate:
		if e.Thread == "" {
			return ErrEmptyThread
		}
		if e.CPU < 0 {
			return fmt.Errorf("%w: %d", ErrNegativeCPU, e.CPU)
		}
	case KindCPUOnline, KindCPUOffline, KindCPUQuiescent:
		if e.CPU < 0 {
			return fmt.Errorf("%w: %d", ErrNegativeCPU, e.CPU)
		}
	case KindRegisterCallback:
		if e.Callback == "" {
			return ErrEmptyCallbackID
		}
		if _, dup := c.callbacks[e.Callback]; dup {
			return fmt.Errorf("%w: %q", ErrDuplicateCallback, e.Callback)
		}
	case KindAckCallback:
		if e.Callback == "" {
			return ErrEmptyCallbackID
		}
		if _, ok := c.callbacks[e.Callback]; !ok {
			return fmt.Errorf("%w: %q", ErrUnknownCallback, e.Callback)
		}
	default:
		return fmt.Errorf("%w: %d", ErrUnknownKind, int(e.Kind))
	}
	return nil
}

func (c *Coordinator) enterRead(thread string) {
	if r, ok := c.readers[thread]; ok {
		r.nesting++
		return
	}
	c.generation++
	c.nextReaderID++
	cpu, tracked := c.trackedThreads[thread]
	r := &readerState{
		id:      c.nextReaderID,
		thread:  thread,
		gen:     c.generation,
		nesting: 1,
		cpu:     cpu,
		hasCPU:  tracked,
	}
	c.readers[thread] = r
	c.readersByID[r.id] = r
}

func (c *Coordinator) exitRead(thread string) {
	r := c.readers[thread]
	r.nesting--
	if r.nesting > 0 {
		return
	}
	// The reader instance ends here. Drop it from every wait set so a later
	// EnterRead by the same thread starts as a brand new reader instance and
	// cannot re-block callbacks that already snapshotted the old one.
	for _, cb := range c.callbacks {
		delete(cb.waiters, r.id)
	}
	delete(c.readers, thread)
	delete(c.readersByID, r.id)
}

func (c *Coordinator) migrate(thread string, cpu int) {
	c.trackedThreads[thread] = cpu
	if r, ok := c.readers[thread]; ok {
		r.cpu = cpu
		r.hasCPU = true
	}
}

func (c *Coordinator) register(id, source string, seq int64) {
	c.nextOrder++
	cb := &callbackState{
		id:      id,
		source:  source,
		order:   c.nextOrder,
		seq:     seq,
		waiters: make(map[uint64]struct{}),
	}
	// Wait set: exactly the readers inside a read section right now. Nesting
	// depth does not matter; the outermost generation identifies them.
	for _, r := range c.readers {
		cb.waiters[r.id] = struct{}{}
	}
	c.callbacks[id] = cb
	c.callbacksByID[uint64(cb.order)] = cb
	c.bySource[source] = append(c.bySource[source], cb)
}

// deliver hands grace-satisfied callbacks to the reclaimer. Within one source
// callbacks are handed over strictly in registration order, so an early
// callback holds back later ones from the same source until it is handed over.
// Sources are independent of each other. It returns the ids of callbacks that
// became effective because of the event just applied, ordered by registration.
func (c *Coordinator) deliver() []string {
	type became struct {
		order int64
		id    string
	}
	var newly []became

	for _, list := range c.bySource {
		for _, cb := range list {
			if cb.effSeq != 0 {
				continue // already handed over
			}
			if !cb.graceSatisfied() {
				break // head of this source is still waiting: stop
			}
			if cb.graceSeq == 0 {
				cb.graceSeq = c.nextSeq
			}
			cb.effSeq = c.nextSeq
			newly = append(newly, became{order: cb.order, id: cb.id})
		}
	}
	sort.Slice(newly, func(i, j int) bool { return newly[i].order < newly[j].order })
	ids := make([]string, len(newly))
	for i, b := range newly {
		ids[i] = b.id
	}
	return ids
}

func (c *Coordinator) cpuState(cpu int) *cpuState {
	cs, ok := c.cpus[cpu]
	if !ok {
		cs = &cpuState{}
		c.cpus[cpu] = cs
	}
	return cs
}

// CallbackReport is the observable state of one callback.
type CallbackReport struct {
	ID      string
	Source  string
	Order   int64    // global registration order, starting at 1
	Seq     int64    // sequence number of the registration event
	Waiters []string // threads the callback is still waiting on, sorted
	// GraceSatisfiedAt is the event sequence at which the wait set became
	// empty, or 0 while readers are still inside.
	GraceSatisfiedAt int64
	// EffectiveAt is the event sequence at which the callback was handed to
	// the reclaimer, or 0 if not yet handed over. It equals the point from
	// which the callback may take effect.
	EffectiveAt int64
	Acked       bool
	AckedAt     int64
}

// Effective reports whether the callback has been handed to the reclaimer.
func (r CallbackReport) Effective() bool { return r.EffectiveAt != 0 }

// CallbackReport returns the state of one callback id.
func (c *Coordinator) CallbackReport(id string) (CallbackReport, bool) {
	cb, ok := c.callbacks[id]
	if !ok {
		return CallbackReport{}, false
	}
	return c.report(cb), true
}

func (c *Coordinator) report(cb *callbackState) CallbackReport {
	waiters := make([]string, 0, len(cb.waiters))
	for id := range cb.waiters {
		if r, ok := c.readersByID[id]; ok {
			waiters = append(waiters, r.thread)
		} else {
			// Should not happen: an exited reader is always removed from every
			// wait set. Surfacing the id keeps an internal bug visible.
			waiters = append(waiters, fmt.Sprintf("reader#%d", id))
		}
	}
	sort.Strings(waiters)
	return CallbackReport{
		ID:               cb.id,
		Source:           cb.source,
		Order:            cb.order,
		Seq:              cb.seq,
		Waiters:          waiters,
		GraceSatisfiedAt: cb.graceSeq,
		EffectiveAt:      cb.effSeq,
		Acked:            cb.acked,
		AckedAt:          cb.ackSeq,
	}
}

// Reports returns every callback's state in global registration order.
func (c *Coordinator) Reports() []CallbackReport {
	list := make([]*callbackState, 0, len(c.callbacks))
	for _, cb := range c.callbacks {
		list = append(list, cb)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].order < list[j].order })
	out := make([]CallbackReport, len(list))
	for i, cb := range list {
		out[i] = c.report(cb)
	}
	return out
}

// ReaderReport describes one open read section.
type ReaderReport struct {
	Thread     string
	ReaderID   uint64
	Generation uint64
	Nesting    int
	CPU        int // last observed CPU, valid when HasCPU is true
	HasCPU     bool
}

// Readers returns every open read section, sorted by thread.
func (c *Coordinator) Readers() []ReaderReport {
	out := make([]ReaderReport, 0, len(c.readers))
	for _, r := range c.readers {
		out = append(out, ReaderReport{
			Thread:     r.thread,
			ReaderID:   r.id,
			Generation: r.gen,
			Nesting:    r.nesting,
			CPU:        r.cpu,
			HasCPU:     r.hasCPU,
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Thread < out[j].Thread })
	return out
}

// CPUReport describes a CPU's recorded lifecycle announcements.
type CPUReport struct {
	CPU         int
	Online      bool
	QuiescentAt int64
}

// CPUs returns recorded CPU state, sorted by CPU id.
func (c *Coordinator) CPUs() []CPUReport {
	out := make([]CPUReport, 0, len(c.cpus))
	for id, cs := range c.cpus {
		out = append(out, CPUReport{CPU: id, Online: cs.online, QuiescentAt: cs.quiesSeq})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CPU < out[j].CPU })
	return out
}

// PendingCallbacks returns callbacks that are not yet effective, in
// registration order.
func (c *Coordinator) PendingCallbacks() []CallbackReport {
	var out []CallbackReport
	for _, r := range c.Reports() {
		if !r.Effective() {
			out = append(out, r)
		}
	}
	return out
}
