package rcu

import (
	"encoding/json"
	"fmt"
	"sort"
)

// snapshotFormat versions the on-disk state so a future incompatible change is
// rejected rather than silently misread.
const snapshotFormat = 1

type snapshotReader struct {
	ID      uint64 `json:"id"`
	Thread  string `json:"thread"`
	Gen     uint64 `json:"gen"`
	Nesting int    `json:"nesting"`
	CPU     int    `json:"cpu"`
	HasCPU  bool   `json:"has_cpu"`
}

type snapshotCallback struct {
	ID       string   `json:"id"`
	Source   string   `json:"source"`
	Order    int64    `json:"order"`
	Seq      int64    `json:"seq"`
	Waiters  []uint64 `json:"waiters"`
	GraceSeq int64    `json:"grace_seq"`
	EffSeq   int64    `json:"eff_seq"`
	AckSeq   int64    `json:"ack_seq"`
	Acked    bool     `json:"acked"`
}

type snapshotCPU struct {
	CPU      int   `json:"cpu"`
	Online   bool  `json:"online"`
	QuiesSeq int64 `json:"quies_seq"`
}

type snapshotThread struct {
	Thread string `json:"thread"`
	CPU    int    `json:"cpu"`
}

// snapshotState is the complete durable state of a coordinator. It deliberately
// omits nothing that affects future behaviour, so reopening resumes exactly
// where the previous run stopped.
type snapshotState struct {
	Format       int                `json:"format"`
	NextSeq      int64              `json:"next_seq"`
	Generation   uint64             `json:"generation"`
	NextReaderID uint64             `json:"next_reader_id"`
	NextOrder    int64              `json:"next_order"`
	Readers      []snapshotReader   `json:"readers"`
	Callbacks    []snapshotCallback `json:"callbacks"`
	CPUs         []snapshotCPU      `json:"cpus"`
	Threads      []snapshotThread   `json:"threads"`
	Log          []LoggedEvent      `json:"log"`
}

// Snapshot serialises the coordinator, including open read sections, committed
// and pending callback positions, and the accepted event log.
func (c *Coordinator) Snapshot() ([]byte, error) {
	s := snapshotState{
		Format:       snapshotFormat,
		NextSeq:      c.nextSeq,
		Generation:   c.generation,
		NextReaderID: c.nextReaderID,
		NextOrder:    c.nextOrder,
		Log:          append([]LoggedEvent(nil), c.log...),
	}

	for _, r := range c.readersByID {
		s.Readers = append(s.Readers, snapshotReader{
			ID: r.id, Thread: r.thread, Gen: r.gen, Nesting: r.nesting,
			CPU: r.cpu, HasCPU: r.hasCPU,
		})
	}
	sort.Slice(s.Readers, func(i, j int) bool { return s.Readers[i].ID < s.Readers[j].ID })

	for _, cb := range c.callbacksByID {
		waiters := make([]uint64, 0, len(cb.waiters))
		for id := range cb.waiters {
			waiters = append(waiters, id)
		}
		sort.Slice(waiters, func(i, j int) bool { return waiters[i] < waiters[j] })
		s.Callbacks = append(s.Callbacks, snapshotCallback{
			ID: cb.id, Source: cb.source, Order: cb.order, Seq: cb.seq,
			Waiters: waiters, GraceSeq: cb.graceSeq, EffSeq: cb.effSeq,
			AckSeq: cb.ackSeq, Acked: cb.acked,
		})
	}
	sort.Slice(s.Callbacks, func(i, j int) bool { return s.Callbacks[i].Order < s.Callbacks[j].Order })

	for id, cs := range c.cpus {
		s.CPUs = append(s.CPUs, snapshotCPU{CPU: id, Online: cs.online, QuiesSeq: cs.quiesSeq})
	}
	sort.Slice(s.CPUs, func(i, j int) bool { return s.CPUs[i].CPU < s.CPUs[j].CPU })

	for thread, cpu := range c.trackedThreads {
		s.Threads = append(s.Threads, snapshotThread{Thread: thread, CPU: cpu})
	}
	sort.Slice(s.Threads, func(i, j int) bool { return s.Threads[i].Thread < s.Threads[j].Thread })

	return json.MarshalIndent(s, "", "  ")
}

// Restore rebuilds a coordinator from a Snapshot. It rejects structurally
// invalid snapshots and leaves the returned coordinator unusable on error.
func Restore(data []byte) (*Coordinator, error) {
	var s snapshotState
	if err := json.Unmarshal(data, &s); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrBadSnapshot, err)
	}
	if s.Format != snapshotFormat {
		return nil, fmt.Errorf("%w: unsupported format %d", ErrBadSnapshot, s.Format)
	}

	c := New()
	c.nextSeq = s.NextSeq
	c.generation = s.Generation
	c.nextReaderID = s.NextReaderID
	c.nextOrder = s.NextOrder
	c.log = append([]LoggedEvent(nil), s.Log...)

	for _, sr := range s.Readers {
		if sr.ID == 0 || sr.Thread == "" || sr.Nesting < 1 {
			return nil, fmt.Errorf("%w: bad reader %+v", ErrBadSnapshot, sr)
		}
		if _, dup := c.readersByID[sr.ID]; dup {
			return nil, fmt.Errorf("%w: duplicate reader id %d", ErrBadSnapshot, sr.ID)
		}
		if _, dup := c.readers[sr.Thread]; dup {
			return nil, fmt.Errorf("%w: duplicate open thread %q", ErrBadSnapshot, sr.Thread)
		}
		r := &readerState{
			id: sr.ID, thread: sr.Thread, gen: sr.Gen, nesting: sr.Nesting,
			cpu: sr.CPU, hasCPU: sr.HasCPU,
		}
		c.readers[sr.Thread] = r
		c.readersByID[r.id] = r
		if r.id >= c.nextReaderID {
			c.nextReaderID = r.id
		}
	}

	for _, sc := range s.Callbacks {
		if sc.ID == "" || sc.Order == 0 {
			return nil, fmt.Errorf("%w: bad callback %+v", ErrBadSnapshot, sc)
		}
		if _, dup := c.callbacks[sc.ID]; dup {
			return nil, fmt.Errorf("%w: duplicate callback %q", ErrBadSnapshot, sc.ID)
		}
		cb := &callbackState{
			id: sc.ID, source: sc.Source, order: sc.Order, seq: sc.Seq,
			waiters:  make(map[uint64]struct{}, len(sc.Waiters)),
			graceSeq: sc.GraceSeq, effSeq: sc.EffSeq, ackSeq: sc.AckSeq, acked: sc.Acked,
		}
		for _, w := range sc.Waiters {
			if _, ok := c.readersByID[w]; !ok {
				return nil, fmt.Errorf("%w: callback %q waits on unknown reader %d", ErrBadSnapshot, sc.ID, w)
			}
			cb.waiters[w] = struct{}{}
		}
		c.callbacks[sc.ID] = cb
		c.callbacksByID[uint64(cb.order)] = cb
		c.bySource[cb.source] = append(c.bySource[cb.source], cb)
		if cb.order >= c.nextOrder {
			c.nextOrder = cb.order
		}
	}
	// Preserve registration order per source after the map reconstruction.
	for _, list := range c.bySource {
		sort.Slice(list, func(i, j int) bool { return list[i].order < list[j].order })
	}

	for _, sc := range s.CPUs {
		c.cpus[sc.CPU] = &cpuState{online: sc.Online, quiesSeq: sc.QuiesSeq}
	}
	for _, st := range s.Threads {
		c.trackedThreads[st.Thread] = st.CPU
	}
	return c, nil
}

// Reopen restores a coordinator from a snapshot and verifies that replaying the
// recorded log into a fresh coordinator reproduces the same durable state. The
// verification guards against a corrupt or hand-edited snapshot. The accepted
// log is not re-appended: the restored coordinator continues after it.
func Reopen(data []byte) (*Coordinator, error) {
	c, err := Restore(data)
	if err != nil {
		return nil, err
	}
	logged := c.Log()
	events := make([]Event, len(logged))
	for i, le := range logged {
		events[i] = le.Event
	}
	replayed, err := Replay(events)
	if err != nil {
		return nil, fmt.Errorf("%w: log replay failed: %v", ErrBadSnapshot, err)
	}
	// Compare the durable projections that a snapshot carries.
	want, err := c.projection()
	if err != nil {
		return nil, err
	}
	got, err := replayed.projection()
	if err != nil {
		return nil, err
	}
	if want != got {
		return nil, fmt.Errorf("%w: snapshot state does not match its recorded log", ErrBadSnapshot)
	}
	return c, nil
}

// Replay applies events in order to a fresh coordinator.
func Replay(events []Event) (*Coordinator, error) {
	c := New()
	for i, e := range events {
		if _, err := c.Apply(e); err != nil {
			return nil, fmt.Errorf("event %d (%s): %w", i, e, err)
		}
	}
	return c, nil
}

// projection renders the durable, comparable state used to verify that a
// snapshot and its log agree. Reports are naturally ordered, so string equality
// is meaningful.
func (c *Coordinator) projection() (string, error) {
	type view struct {
		Generation uint64
		LastSeq    int64
		Readers    []ReaderReport
		Callbacks  []CallbackReport
		CPUs       []CPUReport
		Threads    map[string]int
	}
	v := view{
		Generation: c.generation,
		LastSeq:    c.nextSeq,
		Readers:    c.Readers(),
		Callbacks:  c.Reports(),
		CPUs:       c.CPUs(),
		Threads:    map[string]int{},
	}
	for k, val := range c.trackedThreads {
		v.Threads[k] = val
	}
	b, err := json.Marshal(v)
	if err != nil {
		return "", err
	}
	return string(b), nil
}
