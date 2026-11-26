// Package segmentmerge merges adjacent network segments into immutable
// aggregates while retaining enough information to split them back into the
// original segments.
//
// The merger has two independent orderings:
//
//   - Receive IDs are per-connection completion/report numbers. Reports may be
//     submitted out of order, but an aggregate is released only after every
//     lower receive ID has arrived and, when configured, been confirmed.
//   - Sequence ranges describe byte adjacency. Consecutive receive records are
//     placed in receive-ID order, but are coalesced only when their sequence
//     ranges are adjacent and their merge attributes match.
//
// A receive ID is skipped only after an explicit MarkLost call. Such a loss is
// also an aggregation boundary: records on opposite sides of the lost ID are
// never placed in the same aggregate.
package segmentmerge

import (
	"bytes"
	"errors"
	"fmt"
	"sync"
	"time"
)

// ConnID identifies one connection.
type ConnID string

// ReceiveID is a connection-local receive/report number. Numbering is assumed
// to increase by one and not wrap during a simulation.
type ReceiveID uint64

// Protocol identifies the protocol family/marker carried by a segment.
type Protocol string

// Flags contains protocol/segment flags. FlagEnd is a boundary control bit.
// It closes an aggregate after the carrying segment has been appended and is
// intentionally excluded from merge-attribute equality.
type Flags uint32

const (
	// FlagEnd marks the final segment of an upper-layer message/group.
	FlagEnd Flags = 1 << iota
)

// Options are protocol options that must be identical for segments to merge.
// A nil map and an empty map are considered equal.
type Options map[string]string

// Segment is one simulated received segment.
type Segment struct {
	// ConnID is the connection to which the segment belongs.
	ConnID ConnID

	// RecvID is the connection-local receive/report number.
	RecvID ReceiveID

	// SeqStart is the inclusive sequence start.
	SeqStart uint64

	// SeqEnd is the exclusive sequence end. It must equal SeqStart plus
	// uint64(len(Data)).
	SeqEnd uint64

	// Data is the exact segment payload. Submit copies this slice.
	Data []byte

	// Timestamp is preserved in the aggregate boundary.
	Timestamp time.Time

	// Protocol is the protocol marker.
	Protocol Protocol

	// Flags are merge flags. FlagEnd closes the aggregate after this segment.
	Flags Flags

	// Options must match for adjacent segments to coalesce.
	Options Options

	// End is an explicit end marker. It is equivalent to setting FlagEnd.
	End bool
}

func (s Segment) terminal() bool {
	return s.End || s.Flags&FlagEnd != 0
}

// coalesceFlags removes boundary-control bits from attribute comparison.
func (s Segment) coalesceFlags() Flags {
	return s.Flags &^ FlagEnd
}

// CloseReason records why an Aggregate was closed.
type CloseReason string

const (
	// CloseCapacity means the byte limit was reached or the next segment would
	// exceed it.
	CloseCapacity CloseReason = "capacity"

	// CloseEndFlag means a segment carried the end marker.
	CloseEndFlag CloseReason = "end_flag"

	// CloseSequence means sequence ranges were not adjacent.
	CloseSequence CloseReason = "sequence_discontinuity"

	// CloseAttributes means protocol markers, flags or options differed.
	CloseAttributes CloseReason = "attributes_mismatch"

	// CloseReceiveLoss means an explicitly lost receive ID cuts the group.
	CloseReceiveLoss CloseReason = "receive_id_lost"

	// CloseFlush means Flush or FlushAll forcibly released a partial group.
	CloseFlush CloseReason = "flush"
)

// Boundary describes one original segment inside an Aggregate.
type Boundary struct {
	RecvID    ReceiveID
	Offset    int
	Length    int
	SeqStart  uint64
	SeqEnd    uint64
	Timestamp time.Time
}

// Aggregate is a sealed coalesced group. It is safe to retain and inspect after
// it has been returned by Drain; the Merger never mutates a returned value.
type Aggregate struct {
	ConnID    ConnID
	RecvStart ReceiveID
	RecvEnd   ReceiveID
	SeqStart  uint64
	SeqEnd    uint64

	Protocol Protocol
	Flags    Flags
	Options  Options

	// Data is the concatenation of the original segment payloads.
	Data []byte

	// Boundaries retains every original timestamp and byte/sequence boundary.
	Boundaries []Boundary

	// Reason is the immediate cause for sealing the aggregate.
	Reason CloseReason

	originals []Segment
}

// Len returns the number of coalesced bytes.
func (a *Aggregate) Len() int {
	if a == nil {
		return 0
	}
	return len(a.Data)
}

// SegmentCount returns the number of original segments in the aggregate.
func (a *Aggregate) SegmentCount() int {
	if a == nil {
		return 0
	}
	return len(a.originals)
}

// Split returns independent copies of the original segments, in receive-ID
// order. The returned payload and option maps do not alias the Aggregate.
func (a *Aggregate) Split() []Segment {
	if a == nil {
		return nil
	}
	out := make([]Segment, len(a.originals))
	for i, seg := range a.originals {
		if seg.Data != nil {
			seg.Data = append([]byte(nil), seg.Data...)
		}
		seg.Options = cloneOptions(seg.Options)
		out[i] = seg
	}
	return out
}

// Validate checks that Data, Boundaries and retained original payloads agree.
// It is intended for diagnostics and tests.
func (a *Aggregate) Validate() error {
	if a == nil {
		return errors.New("segmentmerge: nil aggregate")
	}
	if len(a.Boundaries) != len(a.originals) {
		return fmt.Errorf("segmentmerge: %d boundaries for %d originals", len(a.Boundaries), len(a.originals))
	}

	offset := 0
	for i, original := range a.originals {
		b := a.Boundaries[i]
		if b.Offset != offset {
			return fmt.Errorf("segmentmerge: part %d offset = %d, want %d", i, b.Offset, offset)
		}
		if b.Length != len(original.Data) {
			return fmt.Errorf("segmentmerge: part %d boundary length = %d, data length = %d", i, b.Length, len(original.Data))
		}
		if b.SeqStart != original.SeqStart || b.SeqEnd != original.SeqEnd {
			return fmt.Errorf("segmentmerge: part %d sequence boundary does not match original", i)
		}
		if b.RecvID != original.RecvID {
			return fmt.Errorf("segmentmerge: part %d receive ID does not match original", i)
		}
		end := offset + b.Length
		if end > len(a.Data) {
			return fmt.Errorf("segmentmerge: part %d exceeds aggregate data", i)
		}
		if !bytes.Equal(a.Data[offset:end], original.Data) {
			return fmt.Errorf("segmentmerge: part %d bytes do not match original", i)
		}
		offset = end
	}
	if offset != len(a.Data) {
		return fmt.Errorf("segmentmerge: boundary length %d does not match data length %d", offset, len(a.Data))
	}
	return nil
}

// Config configures a Merger.
type Config struct {
	// MaxBytes is the maximum aggregate payload length. Zero selects
	// DefaultMaxBytes. A negative value means unlimited.
	MaxBytes int

	// FirstReceiveID is the first valid receive ID for every connection.
	// Zero is a valid first ID.
	FirstReceiveID ReceiveID

	// RequireConfirmation makes Submit only store a report. A reported ID can
	// advance release after Confirm has also been called.
	RequireConfirmation bool
}

// DefaultMaxBytes is used when Config.MaxBytes is zero.
const DefaultMaxBytes = 64 << 10

func (c Config) withDefaults() Config {
	if c.MaxBytes == 0 {
		c.MaxBytes = DefaultMaxBytes
	}
	return c
}

// Sentinel errors returned by Merger methods.
var (
	ErrEmptyConnID      = errors.New("segmentmerge: empty connection ID")
	ErrInvalidSegment   = errors.New("segmentmerge: invalid segment")
	ErrSegmentTooLarge  = errors.New("segmentmerge: segment exceeds maximum aggregate size")
	ErrDuplicateReceive = errors.New("segmentmerge: duplicate receive ID")
	ErrReceiveLost      = errors.New("segmentmerge: receive ID was explicitly marked lost")
	ErrAlreadyReceived  = errors.New("segmentmerge: receive ID already has a segment")
	ErrStaleReceiveID   = errors.New("segmentmerge: receive ID is already settled")
)

// Merger coalesces simulated segments. Its methods are safe for concurrent use.
type Merger struct {
	mu    sync.Mutex
	cfg   Config
	conns map[ConnID]*connState
	ready []*Aggregate
}

type rxState struct {
	seg       Segment
	arrived   bool
	confirmed bool
	lost      bool
}

type connState struct {
	first   ReceiveID
	next    ReceiveID
	rx      map[ReceiveID]*rxState
	builder *groupBuilder
}

// New creates a Merger.
func New(cfg Config) *Merger {
	return &Merger{
		cfg:   cfg.withDefaults(),
		conns: make(map[ConnID]*connState),
	}
}

// Submit stores one reported segment and advances all releasable work.
//
// When Config.RequireConfirmation is false (the default), a successful Submit
// also confirms the receive ID. When true, Confirm must be called separately.
func (m *Merger) Submit(seg Segment) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if err := m.validateSegmentLocked(seg); err != nil {
		return err
	}

	cs := m.connLocked(seg.ConnID)
	if seg.RecvID < cs.first {
		return fmt.Errorf("%w: receive ID %d is before first ID %d", ErrStaleReceiveID, seg.RecvID, cs.first)
	}

	rx := cs.rx[seg.RecvID]
	if rx != nil {
		switch {
		case rx.lost:
			return fmt.Errorf("%w: conn=%q recv=%d", ErrReceiveLost, seg.ConnID, seg.RecvID)
		case rx.arrived:
			return fmt.Errorf("%w: conn=%q recv=%d", ErrDuplicateReceive, seg.ConnID, seg.RecvID)
		}
	} else {
		rx = &rxState{}
		cs.rx[seg.RecvID] = rx
	}

	seg.Data = cloneBytes(seg.Data)
	seg.Options = cloneOptions(seg.Options)
	rx.seg = seg
	rx.arrived = true
	if !m.cfg.RequireConfirmation {
		rx.confirmed = true
	}

	m.advanceLocked(cs)
	return nil
}

// Confirm records that the driver has confirmed a receive ID. Confirming an
// already advanced ID is an idempotent no-op. Confirm may be called before the
// corresponding Segment is submitted; the segment still has to arrive before
// it can be published.
func (m *Merger) Confirm(conn ConnID, id ReceiveID) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if conn == "" {
		return ErrEmptyConnID
	}
	cs := m.connLocked(conn)
	if id < cs.next {
		return nil
	}

	rx := m.rxLocked(cs, id)
	if rx.lost {
		return fmt.Errorf("%w: cannot confirm lost conn=%q recv=%d", ErrReceiveLost, conn, id)
	}
	rx.confirmed = true
	m.advanceLocked(cs)
	return nil
}

// ConfirmThrough confirms every already-reported, non-lost receive ID up to
// and including id. It never invents confirmation for an unknown ID, so a
// missing receive-ID gap still blocks release until the segment arrives or
// MarkLost is called.
func (m *Merger) ConfirmThrough(conn ConnID, id ReceiveID) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if conn == "" {
		return ErrEmptyConnID
	}
	cs := m.connLocked(conn)
	if id < cs.next {
		return nil
	}
	for rid, rx := range cs.rx {
		if rid <= id && rx.arrived && !rx.lost {
			rx.confirmed = true
		}
	}
	m.advanceLocked(cs)
	return nil
}

// MarkLost explicitly marks a receive ID as lost. This is the only mechanism
// that allows the receive-ID frontier to skip an ID. The loss cuts coalescing.
// Repeating MarkLost for an already-lost pending ID is an idempotent no-op.
func (m *Merger) MarkLost(conn ConnID, id ReceiveID) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if conn == "" {
		return ErrEmptyConnID
	}
	cs := m.connLocked(conn)
	rx := m.rxLocked(cs, id)

	if rx.lost {
		return nil
	}
	if rx.arrived {
		return fmt.Errorf("%w: conn=%q recv=%d", ErrAlreadyReceived, conn, id)
	}
	if id < cs.next {
		return fmt.Errorf("%w: conn=%q recv=%d already processed", ErrStaleReceiveID, conn, id)
	}

	rx.lost = true
	rx.confirmed = false
	m.advanceLocked(cs)
	return nil
}

// NextReceiveID returns the next receive ID that has not yet been processed.
// It is primarily useful for diagnostics and simulated drivers.
func (m *Merger) NextReceiveID(conn ConnID) ReceiveID {
	m.mu.Lock()
	defer m.mu.Unlock()
	cs, ok := m.conns[conn]
	if !ok {
		return m.cfg.FirstReceiveID
	}
	return cs.next
}

// Flush seals the currently open, contiguous prefix for conn. It never skips
// unresolved receive IDs: segments behind a receive-ID/confirmation gap remain
// buffered.
func (m *Merger) Flush(conn ConnID) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if conn == "" {
		return ErrEmptyConnID
	}
	if cs := m.conns[conn]; cs != nil && cs.builder != nil {
		m.sealLocked(cs, CloseFlush)
	}
	return nil
}

// FlushAll seals every currently open aggregate. It does not skip gaps.
func (m *Merger) FlushAll() {
	m.mu.Lock()
	defer m.mu.Unlock()

	for _, cs := range m.conns {
		if cs.builder != nil {
			m.sealLocked(cs, CloseFlush)
		}
	}
}

// Drain returns and removes all sealed aggregates in sealing order.
func (m *Merger) Drain() []*Aggregate {
	m.mu.Lock()
	defer m.mu.Unlock()

	out := m.ready
	m.ready = nil
	return out
}

func (m *Merger) validateSegmentLocked(seg Segment) error {
	if seg.ConnID == "" {
		return ErrEmptyConnID
	}
	if seg.SeqEnd < seg.SeqStart {
		return fmt.Errorf("%w: seq_end %d < seq_start %d", ErrInvalidSegment, seg.SeqEnd, seg.SeqStart)
	}
	wantLen := seg.SeqEnd - seg.SeqStart
	if wantLen != uint64(len(seg.Data)) {
		return fmt.Errorf("%w: sequence length %d does not match payload length %d", ErrInvalidSegment, wantLen, len(seg.Data))
	}
	if m.cfg.MaxBytes > 0 && len(seg.Data) > m.cfg.MaxBytes {
		return fmt.Errorf("%w: payload %d > limit %d", ErrSegmentTooLarge, len(seg.Data), m.cfg.MaxBytes)
	}
	if seg.RecvID < m.cfg.FirstReceiveID {
		return fmt.Errorf("%w: receive ID %d < first %d", ErrInvalidSegment, seg.RecvID, m.cfg.FirstReceiveID)
	}
	return nil
}

func (m *Merger) connLocked(conn ConnID) *connState {
	cs := m.conns[conn]
	if cs == nil {
		cs = &connState{
			first: m.cfg.FirstReceiveID,
			next:  m.cfg.FirstReceiveID,
			rx:    make(map[ReceiveID]*rxState),
		}
		m.conns[conn] = cs
	}
	return cs
}

func (m *Merger) rxLocked(cs *connState, id ReceiveID) *rxState {
	rx := cs.rx[id]
	if rx == nil {
		rx = &rxState{}
		cs.rx[id] = rx
	}
	return rx
}

func (m *Merger) advanceLocked(cs *connState) {
	for {
		rx := cs.rx[cs.next]
		if rx == nil {
			return
		}

		if rx.lost {
			if cs.builder != nil {
				m.sealLocked(cs, CloseReceiveLoss)
			}
			cs.next++
			continue
		}

		if !rx.arrived {
			// Confirm-only placeholder; wait for the actual segment.
			return
		}
		if m.cfg.RequireConfirmation && !rx.confirmed {
			return
		}

		seg := rx.seg
		if cs.builder != nil {
			if reason := joinBlockedLocked(cs.builder, seg, m.cfg.MaxBytes); reason != "" {
				m.sealLocked(cs, reason)
			}
		}

		if cs.builder == nil {
			cs.builder = newGroupBuilder(seg)
		} else {
			cs.builder.add(seg)
		}

		switch {
		case m.cfg.MaxBytes > 0 && cs.builder.Len() >= m.cfg.MaxBytes:
			m.sealLocked(cs, CloseCapacity)
		case seg.terminal():
			m.sealLocked(cs, CloseEndFlag)
		}

		cs.next++
	}
}

func joinBlockedLocked(b *groupBuilder, seg Segment, maxBytes int) CloseReason {
	if b.conn != seg.ConnID ||
		b.protocol != seg.Protocol ||
		(b.flags&^FlagEnd) != seg.coalesceFlags() ||
		!optionsEqual(b.options, seg.Options) {
		return CloseAttributes
	}
	if b.seqEnd != seg.SeqStart {
		return CloseSequence
	}
	if maxBytes > 0 && b.Len()+len(seg.Data) > maxBytes {
		return CloseCapacity
	}
	return ""
}

func (m *Merger) sealLocked(cs *connState, reason CloseReason) {
	if cs.builder == nil {
		return
	}
	m.ready = append(m.ready, cs.builder.build(reason))
	cs.builder = nil
}

type groupBuilder struct {
	conn       ConnID
	recvStart  ReceiveID
	recvEnd    ReceiveID
	seqStart   uint64
	seqEnd     uint64
	protocol   Protocol
	flags      Flags
	options    Options
	data       []byte
	boundaries []Boundary
	originals  []Segment
}

func newGroupBuilder(seg Segment) *groupBuilder {
	b := &groupBuilder{
		conn:      seg.ConnID,
		recvStart: seg.RecvID,
		recvEnd:   seg.RecvID,
		seqStart:  seg.SeqStart,
		seqEnd:    seg.SeqEnd,
		protocol:  seg.Protocol,
		flags:     seg.Flags,
		options:   cloneOptions(seg.Options),
	}
	b.add(seg)
	return b
}

func (b *groupBuilder) Len() int { return len(b.data) }

func (b *groupBuilder) add(seg Segment) {
	offset := len(b.data)
	b.data = append(b.data, seg.Data...)
	b.recvEnd = seg.RecvID
	b.seqEnd = seg.SeqEnd
	b.boundaries = append(b.boundaries, Boundary{
		RecvID:    seg.RecvID,
		Offset:    offset,
		Length:    len(seg.Data),
		SeqStart:  seg.SeqStart,
		SeqEnd:    seg.SeqEnd,
		Timestamp: seg.Timestamp,
	})
	b.originals = append(b.originals, seg)
}

func (b *groupBuilder) build(reason CloseReason) *Aggregate {
	return &Aggregate{
		ConnID:     b.conn,
		RecvStart:  b.recvStart,
		RecvEnd:    b.recvEnd,
		SeqStart:   b.seqStart,
		SeqEnd:     b.seqEnd,
		Protocol:   b.protocol,
		Flags:      b.flags,
		Options:    cloneOptions(b.options),
		Data:       b.data,
		Boundaries: b.boundaries,
		Reason:     reason,
		originals:  b.originals,
	}
}

func cloneBytes(in []byte) []byte {
	if in == nil {
		return nil
	}
	return append(make([]byte, 0, len(in)), in...)
}

func cloneOptions(in Options) Options {
	out := make(Options, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func optionsEqual(a, b Options) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		if bv, ok := b[k]; !ok || bv != v {
			return false
		}
	}
	return true
}
