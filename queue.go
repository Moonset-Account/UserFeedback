// Package neighborqueue implements a simulated neighbor send queue.
//
// The model follows the semantics of a network stack's neighbor subsystem
// (ARP/NDP-like) on top of a small, deterministic simulation:
//
//   - A route maps a network-layer target to the egress interface currently
//     used to reach it. The same target may resolve to different link-layer
//     addresses on different interfaces; neighbor state is therefore keyed by
//     (interface, target), not by target alone.
//   - A target whose neighbor entry has never resolved starts in the
//     Incomplete state and every packet is queued (bounded queue) from the
//     very first packet.
//   - An existing, reachable entry that goes stale may still send one packet
//     using the old link-layer address. That packet moves the entry into the
//     Confirm state, and all further packets are queued until the entry is
//     confirmed.
//   - Only a receipt belonging to the same interface and to the exact packet
//     whose send triggered the current resolution/confirmation can release a
//     queue. Mismatched receipts (wrong interface, stale receipt, or a receipt
//     from another attempt) are rejected.
//   - On resolution failure, confirmation timeout or queue overflow, the
//     affected packets are returned to the caller as FailedPackets.
//   - When a route changes interface, subsequent packets use the new
//     interface's own neighbor state (starting fresh). Packets already sent or
//     queued on the old interface continue to be accounted under the old
//     interface independently.
package neighborqueue

import (
	"errors"
	"sync"
	"time"
)

// IfIndex identifies a simulated network interface.
type IfIndex int

// Target is a network-layer destination address (e.g. an IPv4/IPv6 address).
type Target string

// LinkAddr is a link-layer address (e.g. a MAC address).
type LinkAddr string

// PacketID identifies a packet handed to the queue. IDs are caller-owned and
// need only be meaningful to the caller; the queue treats them opaquely.
type PacketID uint64

// Packet is one simulated outbound packet.
type Packet struct {
	ID      PacketID
	Payload []byte
}

// Receipt is returned for every packet the queue actually transmits. Callers
// later present it through Resolve to confirm the corresponding probe.
// A zero Receipt is never returned by a successful transmit.
type Receipt struct {
	ID     uint64
	IfIdx  IfIndex
	Target Target
}

// Valid reports whether r is a non-zero receipt.
func (r Receipt) Valid() bool { return r.ID != 0 }

// State of a per-(interface,target) neighbor entry.
type State int

const (
	// NoEntry means no neighbor entry exists yet.
	NoEntry State = iota
	// Incomplete: address resolution was requested but never succeeded.
	Incomplete
	// Reachable: a link-layer address is known and fresh.
	Reachable
	// Stale: a link-layer address is known but its reachability is
	// unconfirmed. One packet may still be sent using the old address.
	Stale
	// Confirm: one probe packet has been sent with the stale address and the
	// queue waits for its receipt before releasing queued packets.
	Confirm
	// Failed: the last resolution/confirmation failed. A later packet starts a
	// fresh resolution cycle.
	Failed
)

// String returns a human-readable state name.
func (s State) String() string {
	switch s {
	case NoEntry:
		return "NoEntry"
	case Incomplete:
		return "Incomplete"
	case Reachable:
		return "Reachable"
	case Stale:
		return "Stale"
	case Confirm:
		return "Confirm"
	case Failed:
		return "Failed"
	default:
		return "Unknown"
	}
}

// Errors reported via SendResult / EventResult. The queue never returns Go
// errors for ordinary packet outcomes; packets rejected by policy are returned
// through result fields so that accounting stays explicit.
var (
	// ErrNoRoute means no egress interface is known for the target.
	ErrNoRoute = errors.New("neighborqueue: no route to target")
	// ErrUnknownEntry means the referenced (interface,target) entry does not
	// exist.
	ErrUnknownEntry = errors.New("neighborqueue: unknown neighbor entry")
	// ErrReceiptMismatch means the receipt does not match the interface and
	// the packet currently awaiting confirmation.
	ErrReceiptMismatch = errors.New("neighborqueue: receipt does not match pending confirmation")
	// ErrBadLinkAddr means a successful resolution carried an empty link
	// address.
	ErrBadLinkAddr = errors.New("neighborqueue: successful resolution requires a non-empty link address")
)

// Config holds queue timing and capacity parameters.
type Config struct {
	// ReachableTTL is how long a confirmed link address stays Reachable.
	ReachableTTL time.Duration
	// ConfirmTimeout bounds the time an entry may wait in Confirm for the
	// probe receipt.
	ConfirmTimeout time.Duration
	// ResolveTimeout bounds the time an entry may wait in Incomplete for a
	// resolution event.
	ResolveTimeout time.Duration
	// QueueCap is the maximum number of packets held per neighbor entry.
	// Overflow packets are returned immediately as failed.
	QueueCap int
}

// DefaultConfig returns conservative, non-zero simulation defaults.
func DefaultConfig() Config {
	return Config{
		ReachableTTL:   30 * time.Second,
		ConfirmTimeout: 5 * time.Second,
		ResolveTimeout: 5 * time.Second,
		QueueCap:       8,
	}
}

func (c *Config) withDefaults() {
	d := DefaultConfig()
	if c.ReachableTTL <= 0 {
		c.ReachableTTL = d.ReachableTTL
	}
	if c.ConfirmTimeout <= 0 {
		c.ConfirmTimeout = d.ConfirmTimeout
	}
	if c.ResolveTimeout <= 0 {
		c.ResolveTimeout = d.ResolveTimeout
	}
	if c.QueueCap <= 0 {
		c.QueueCap = d.QueueCap
	}
}

// FailReason categorizes why a packet was returned without delivery.
type FailReason string

const (
	FailOverflow        FailReason = "queue-overflow"
	FailResolveTimeout  FailReason = "resolve-timeout"
	FailResolveRejected FailReason = "resolution-rejected"
	FailConfirmTimeout  FailReason = "confirm-timeout"
	FailConfirmRejected FailReason = "confirmation-rejected"
	FailNoRoute         FailReason = "no-route"
)

// FailedPacket is a packet handed back to the caller after a failure.
type FailedPacket struct {
	Packet Packet
	IfIdx  IfIndex
	Target Target
	Reason FailReason
	Err    error
}

// IfStats accumulates per-interface accounting. Totals aggregate across all
// interfaces.
type IfStats struct {
	// Transmitted counts packets actually handed to the (simulated) wire.
	Transmitted uint64
	// Enqueued counts packets accepted into a bounded queue.
	Enqueued uint64
	// Failed counts packets returned to the caller through failure paths
	// (overflow, timeout, explicit reject), excluding no-route rejections.
	Failed uint64
	// NoRoute counts packets rejected because no egress interface exists.
	NoRoute uint64
}

// entryKey uniquely identifies a neighbor entry by interface and target.
type entryKey struct {
	ifIdx  IfIndex
	target Target
}

// entry is the per-(interface,target) neighbor state machine.
type entry struct {
	ifIdx  IfIndex
	target Target

	state State
	ll    LinkAddr

	// queue holds packets waiting for resolution/confirmation, in FIFO order.
	queue []Packet

	// pendingReceipt is the receipt of the single in-flight probe in
	// Incomplete/Confirm; zero in every other state.
	pendingReceipt uint64

	// stateDeadline is the time at which the current state times out.
	stateDeadline time.Time

	// reachableUntil is the freshness deadline of ll.
	reachableUntil time.Time
}

// TransmitRecord describes one packet actually sent on the wire.
type TransmitRecord struct {
	Packet Packet
	IfIdx  IfIndex
	Target Target
	Link   LinkAddr
	Receipt
}

// SendResult is the outcome of Queue.Send.
type SendResult struct {
	IfIdx IfIndex
	// Transmitted is set when the packet went out immediately. At most one of
	// Transmitted / Enqueued / Failed is populated.
	Transmitted *TransmitRecord
	// Enqueued is true when the packet was accepted into the bounded queue.
	Enqueued bool
	// Failed is set when the packet could not be accepted (overflow or no
	// route).
	Failed *FailedPacket
}

// ResolveResult is the outcome of a resolution/confirmation event.
type ResolveResult struct {
	State State
	// Flushed lists queued packets released after a successful matching event.
	Flushed []TransmitRecord
	// Failed lists queued packets returned after a failed matching event.
	Failed []FailedPacket
	// Err is set for events that did not match any pending state.
	Err error
}

// TickResult is the outcome of advancing the simulation clock.
type TickResult struct {
	// Expired lists entries whose state timed out (state transition only).
	Expired []entryKey
	// Failed lists packets returned because their entry timed out.
	Failed []FailedPacket
}

// InterfaceChangeResult reports a route change.
type InterfaceChangeResult struct {
	Target Target
	Old    IfIndex
	New    IfIndex
	// Created is true when a fresh entry had to be created on the new
	// interface.
	Created bool
}

// Queue is the neighbor send queue. It is safe for concurrent use.
type Queue struct {
	mu      sync.Mutex
	cfg     Config
	now     time.Time
	entries map[entryKey]*entry
	routes  map[Target]IfIndex
	stats   map[IfIndex]*IfStats
	total   IfStats
	nextID  uint64
}

// New creates a queue with the given simulation start time and configuration.
// A zero Config is replaced by DefaultConfig.
func New(start time.Time, cfg Config) *Queue {
	cfg.withDefaults()
	return &Queue{
		cfg:     cfg,
		now:     start,
		entries: make(map[entryKey]*entry),
		routes:  make(map[Target]IfIndex),
		stats:   make(map[IfIndex]*IfStats),
		nextID:  1,
	}
}

// statsLocked returns the accumulator for an interface, creating it on demand.
func (q *Queue) statsLocked(i IfIndex) *IfStats {
	s, ok := q.stats[i]
	if !ok {
		s = &IfStats{}
		q.stats[i] = s
	}
	return s
}

// newReceiptLocked mints a globally unique receipt.
func (q *Queue) newReceiptLocked(i IfIndex, t Target) Receipt {
	id := q.nextID
	q.nextID++
	return Receipt{ID: id, IfIdx: i, Target: t}
}

// transmitLocked puts a packet on the (simulated) wire and updates counters.
func (q *Queue) transmitLocked(e *entry, p Packet, link LinkAddr) TransmitRecord {
	r := q.newReceiptLocked(e.ifIdx, e.target)
	q.statsLocked(e.ifIdx).Transmitted++
	q.total.Transmitted++
	return TransmitRecord{
		Packet:  p,
		IfIdx:   e.ifIdx,
		Target:  e.target,
		Link:    link,
		Receipt: r,
	}
}

// failLocked records a rejected packet in per-interface and total counters.
func (q *Queue) failLocked(i IfIndex, t Target, p Packet, reason FailReason, err error) *FailedPacket {
	fp := &FailedPacket{Packet: p, IfIdx: i, Target: t, Reason: reason, Err: err}
	if reason != FailNoRoute {
		q.statsLocked(i).Failed++
		q.total.Failed++
	} else {
		q.statsLocked(i).NoRoute++
		q.total.NoRoute++
	}
	return fp
}

// enqueueLocked appends a packet to an entry's bounded queue. On overflow the
// packet is returned as a failed packet instead.
func (q *Queue) enqueueLocked(e *entry, p Packet) *FailedPacket {
	if len(e.queue) >= q.cfg.QueueCap {
		return q.failLocked(e.ifIdx, e.target, p, FailOverflow, nil)
	}
	e.queue = append(e.queue, p)
	q.statsLocked(e.ifIdx).Enqueued++
	q.total.Enqueued++
	return nil
}

// drainFailedLocked returns all queued packets of an entry as failed and
// clears the queue.
func (q *Queue) drainFailedLocked(e *entry, reason FailReason, err error) []FailedPacket {
	out := make([]FailedPacket, 0, len(e.queue))
	for _, p := range e.queue {
		fp := q.failLocked(e.ifIdx, e.target, p, reason, err)
		out = append(out, *fp)
	}
	e.queue = e.queue[:0]
	return out
}

// flushLocked transmits every queued packet of a now-reachable entry, in FIFO
// order, and clears the queue.
func (q *Queue) flushLocked(e *entry) []TransmitRecord {
	out := make([]TransmitRecord, 0, len(e.queue))
	for _, p := range e.queue {
		out = append(out, q.transmitLocked(e, p, e.ll))
	}
	e.queue = e.queue[:0]
	return out
}

// markReachableLocked moves an entry to Reachable with a fresh TTL.
func (q *Queue) markReachableLocked(e *entry, ll LinkAddr) {
	e.state = Reachable
	e.ll = ll
	e.pendingReceipt = 0
	e.reachableUntil = q.now.Add(q.cfg.ReachableTTL)
	e.stateDeadline = time.Time{}
}

// beginIncompleteLocked (re)starts resolution for an entry with no usable
// link address and drains anything still queued from a previous attempt.
func (q *Queue) beginIncompleteLocked(e *entry) []FailedPacket {
	var failed []FailedPacket
	if len(e.queue) > 0 {
		failed = q.drainFailedLocked(e, FailResolveRejected, nil)
	}
	e.state = Incomplete
	e.ll = ""
	e.pendingReceipt = 0
	e.stateDeadline = q.now.Add(q.cfg.ResolveTimeout)
	return failed
}

// beginConfirmLocked sends one probe with the stale link address and records
// its receipt as the only receipt allowed to release the queue. The probe
// itself is transmitted, not queued; later packets enter the bounded queue.
func (q *Queue) beginConfirmLocked(e *entry, p Packet) *TransmitRecord {
	rec := q.transmitLocked(e, p, e.ll)
	e.state = Confirm
	e.pendingReceipt = rec.ID
	e.stateDeadline = q.now.Add(q.cfg.ConfirmTimeout)
	return &rec
}

// sweepLocked applies time-based state transitions at time now across all
// entries. Entries are never deleted; Failed entries remain inspectable.
func (q *Queue) sweepLocked(now time.Time) (expired []entryKey, failed []FailedPacket) {
	q.now = now
	for k, e := range q.entries {
		switch e.state {
		case Reachable:
			if !e.reachableUntil.IsZero() && !now.Before(e.reachableUntil) {
				e.state = Stale
				e.stateDeadline = time.Time{}
				expired = append(expired, k)
			}
		case Stale:
			// Stale is idle until the next packet; nothing times out here.
		case Confirm:
			if !e.stateDeadline.IsZero() && !now.Before(e.stateDeadline) {
				failed = append(failed, q.drainFailedLocked(e, FailConfirmTimeout, nil)...)
				e.state = Failed
				e.pendingReceipt = 0
				e.stateDeadline = time.Time{}
				expired = append(expired, k)
			}
		case Incomplete:
			if !e.stateDeadline.IsZero() && !now.Before(e.stateDeadline) {
				failed = append(failed, q.drainFailedLocked(e, FailResolveTimeout, nil)...)
				e.state = Failed
				e.stateDeadline = time.Time{}
				expired = append(expired, k)
			}
		}
	}
	return expired, failed
}

// SetRoute installs or replaces the egress interface for a target. It does not
// create a neighbor entry; the first Send on the new interface does that.
func (q *Queue) SetRoute(t Target, i IfIndex) {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.routes[t] = i
}

// InterfaceChange updates a target's route and returns the previous interface.
// Old neighbor state stays intact so in-flight/queued packets on the old
// interface keep their own accounting.
func (q *Queue) InterfaceChange(t Target, newIf IfIndex) InterfaceChangeResult {
	q.mu.Lock()
	defer q.mu.Unlock()
	old := q.routes[t]
	q.routes[t] = newIf
	res := InterfaceChangeResult{Target: t, Old: old, New: newIf}
	key := entryKey{ifIdx: newIf, target: t}
	if _, ok := q.entries[key]; !ok {
		q.entries[key] = &entry{ifIdx: newIf, target: t}
		res.Created = true
	}
	return res
}

// Route returns the current egress interface for a target, if any.
func (q *Queue) Route(t Target) (IfIndex, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	i, ok := q.routes[t]
	return i, ok
}

// Send enqueues or transmits one packet toward target using its current route.
func (q *Queue) Send(t Target, p Packet) SendResult {
	q.mu.Lock()
	defer q.mu.Unlock()

	i, ok := q.routes[t]
	if !ok {
		return SendResult{Failed: q.failLocked(0, t, p, FailNoRoute, ErrNoRoute)}
	}
	key := entryKey{ifIdx: i, target: t}
	e, ok := q.entries[key]
	if !ok {
		// Never resolved on this interface: queue from the very first packet.
		e = &entry{ifIdx: i, target: t}
		q.entries[key] = e
	}

	switch e.state {
	case NoEntry, Failed:
		// First contact on this interface, or a retry after failure: start a
		// fresh resolution cycle and queue the packet.
		failed := q.beginIncompleteLocked(e)
		_ = failed // a freshly created entry has no prior queue
		if fp := q.enqueueLocked(e, p); fp != nil {
			return SendResult{IfIdx: i, Failed: fp}
		}
		return SendResult{IfIdx: i, Enqueued: true}

	case Incomplete, Confirm:
		// Resolution/confirmation already in flight: bounded queue.
		if fp := q.enqueueLocked(e, p); fp != nil {
			return SendResult{IfIdx: i, Failed: fp}
		}
		return SendResult{IfIdx: i, Enqueued: true}

	case Stale:
		// One last packet may use the old link address, then we wait for its
		// receipt before releasing anything else.
		rec := q.beginConfirmLocked(e, p)
		return SendResult{IfIdx: i, Transmitted: rec}

	case Reachable:
		// Time only advances through Tick, so an expired Reachable entry has
		// already been moved to Stale by sweepLocked before we get here.
		rec := q.transmitLocked(e, p, e.ll)
		return SendResult{IfIdx: i, Transmitted: &rec}
	}
	return SendResult{IfIdx: i, Failed: q.failLocked(i, t, p, FailNoRoute, ErrNoRoute)}
}

// Resolve feeds a resolution or confirmation event.
//
//   - For an Incomplete entry, success requires link != "".
//   - For a Confirm entry, receipt must match the entry's interface and the
//     exact probe packet currently awaiting confirmation.
//   - A non-nil err fails the pending attempt and returns all affected packets.
func (q *Queue) Resolve(i IfIndex, t Target, link LinkAddr, receipt Receipt, err error) ResolveResult {
	q.mu.Lock()
	defer q.mu.Unlock()

	key := entryKey{ifIdx: i, target: t}
	e, ok := q.entries[key]
	if !ok {
		return ResolveResult{Err: ErrUnknownEntry}
	}
	res := ResolveResult{State: e.state}

	if err != nil {
		switch e.state {
		case Incomplete:
			res.Failed = q.drainFailedLocked(e, FailResolveRejected, err)
			e.state = Failed
			e.stateDeadline = time.Time{}
		case Confirm:
			if !receipt.Valid() || receipt.IfIdx != i || receipt.ID != e.pendingReceipt {
				res.Err = ErrReceiptMismatch
				res.State = e.state
				return res
			}
			res.Failed = q.drainFailedLocked(e, FailConfirmRejected, err)
			e.state = Failed
			e.pendingReceipt = 0
			e.stateDeadline = time.Time{}
		default:
			res.Err = ErrReceiptMismatch
		}
		return res
	}

	switch e.state {
	case Incomplete:
		if link == "" {
			res.Err = ErrBadLinkAddr
			return res
		}
		q.markReachableLocked(e, link)
		res.Flushed = q.flushLocked(e)
		res.State = Reachable
	case Confirm:
		if !receipt.Valid() || receipt.IfIdx != i || receipt.Target != t || receipt.ID != e.pendingReceipt {
			res.Err = ErrReceiptMismatch
			return res
		}
		if link != "" {
			e.ll = link
		}
		q.markReachableLocked(e, e.ll)
		res.Flushed = q.flushLocked(e)
		res.State = Reachable
	case Reachable, Stale, Failed, NoEntry:
		// Unsolicited / gratuitous update: refresh a known address but never
		// release queues outside a pending resolution/confirmation.
		if link != "" {
			e.ll = link
			if e.state == Reachable {
				e.reachableUntil = q.now.Add(q.cfg.ReachableTTL)
			}
		}
		res.Err = ErrReceiptMismatch
	default:
		res.Err = ErrReceiptMismatch
	}
	return res
}

// Tick advances the simulation clock to now and processes timeouts.
func (q *Queue) Tick(now time.Time) TickResult {
	q.mu.Lock()
	defer q.mu.Unlock()
	expired, failed := q.sweepLocked(now)
	return TickResult{Expired: expired, Failed: failed}
}

// HandleEvent dispatches the four supported event kinds through one entry
// point, mirroring a real event-driven neighbor subsystem.
func (q *Queue) HandleEvent(ev Event) EventResult {
	switch e := ev.(type) {
	case SendEvent:
		sr := q.Send(e.Target, e.Packet)
		return EventResult{Send: &sr}
	case ResolveEvent:
		rr := q.Resolve(e.IfIdx, e.Target, e.Link, e.Receipt, e.Err)
		return EventResult{Resolve: &rr}
	case TickEvent:
		tr := q.Tick(e.Now)
		return EventResult{Tick: &tr}
	case InterfaceChangeEvent:
		ir := q.InterfaceChange(e.Target, e.NewIf)
		return EventResult{InterfaceChange: &ir}
	default:
		return EventResult{Err: errors.New("neighborqueue: unknown event type")}
	}
}

// EventResult wraps whichever result the dispatched event produced.
type EventResult struct {
	Send            *SendResult
	Resolve         *ResolveResult
	Tick            *TickResult
	InterfaceChange *InterfaceChangeResult
	Err             error
}

// State returns the current state of the entry for (i, t).
func (q *Queue) State(i IfIndex, t Target) State {
	q.mu.Lock()
	defer q.mu.Unlock()
	e, ok := q.entries[entryKey{ifIdx: i, target: t}]
	if !ok {
		return NoEntry
	}
	return e.state
}

// PendingReceipt returns the receipt currently awaiting confirmation, or zero.
func (q *Queue) PendingReceipt(i IfIndex, t Target) Receipt {
	q.mu.Lock()
	defer q.mu.Unlock()
	e, ok := q.entries[entryKey{ifIdx: i, target: t}]
	if !ok || e.pendingReceipt == 0 {
		return Receipt{}
	}
	return Receipt{ID: e.pendingReceipt, IfIdx: i, Target: t}
}

// Queued returns the IDs of packets held on an entry, in queue order.
func (q *Queue) Queued(i IfIndex, t Target) []PacketID {
	q.mu.Lock()
	defer q.mu.Unlock()
	e, ok := q.entries[entryKey{ifIdx: i, target: t}]
	if !ok {
		return nil
	}
	out := make([]PacketID, len(e.queue))
	for n, p := range e.queue {
		out[n] = p.ID
	}
	return out
}

// LinkAddr returns the last known link-layer address of an entry.
func (q *Queue) LinkAddr(i IfIndex, t Target) (LinkAddr, bool) {
	q.mu.Lock()
	defer q.mu.Unlock()
	e, ok := q.entries[entryKey{ifIdx: i, target: t}]
	if !ok || e.ll == "" {
		return "", false
	}
	return e.ll, true
}

// Stats returns a copy of the per-interface counters and the aggregate total.
func (q *Queue) Stats() (map[IfIndex]IfStats, IfStats) {
	q.mu.Lock()
	defer q.mu.Unlock()
	out := make(map[IfIndex]IfStats, len(q.stats))
	for k, v := range q.stats {
		out[k] = *v
	}
	return out, q.total
}

// Now returns the simulation clock as last set by New/Tick (or lazy Send).
func (q *Queue) Now() time.Time {
	q.mu.Lock()
	defer q.mu.Unlock()
	return q.now
}
