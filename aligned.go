// Package alignedjoin implements a two-input stream operator that accumulates
// signed per-key deltas and supports aligned checkpoints.
//
// The operator has two inputs (Left and Right). Each input is an ordered
// sequence of positions starting at zero. Records carry a key and a signed
// delta; every time a record is processed the new accumulated value of its
// key is emitted.
//
// Barriers carry an identifier. Both inputs receive barriers with the same
// identifier, in the same identifier order. The input whose barrier arrives
// first stops processing subsequent records: Pull on that side reports
// EvStaged without consuming the position, while the other input may still
// update the state. Once both barriers have arrived, the accumulated state
// and both barrier positions are frozen into a Snapshot; the blocked sides
// resume and the staged (unconsumed) positions are pulled again in the normal
// way. At most one checkpoint may be in flight at a time.
//
// The caller drives the operator explicitly with Push and Pull, so the exact
// interleaving of the two inputs is under the caller's control and remains
// deterministic across a replay. EvStaged pulls have no effect and do not
// consume a position; the effective, consuming pull order is what determines
// the outputs and is reproduced on replay.
package alignedjoin

import (
	"errors"
)

// Side identifies one of the two operator inputs.
type Side int

const (
	Left  Side = 0
	Right Side = 1
)

// ItemKind distinguishes data records from barriers.
type ItemKind int

const (
	// KindRecord is a data record carrying a key and a signed delta.
	KindRecord ItemKind = iota
	// KindBarrier is an alignment barrier carrying a BarrierID.
	KindBarrier
)

// Item is one position of an operator input.
//
// For KindRecord, Key and Delta are meaningful and BarrierID must be zero.
// For KindBarrier, BarrierID must be positive and Key/Delta are ignored.
type Item struct {
	Kind      ItemKind
	Key       string
	Delta     int64
	BarrierID int64
}

// Record builds a data-record item.
func Record(key string, delta int64) Item {
	return Item{Kind: KindRecord, Key: key, Delta: delta}
}

// Barrier builds a barrier item with the given identifier.
func Barrier(id int64) Item {
	return Item{Kind: KindBarrier, BarrierID: id}
}

// EventKind classifies the events returned by Pull.
type EventKind int

const (
	// EvIdle means the requested input has no queued item. No state changed.
	EvIdle EventKind = iota
	// EvOutput means a record was applied to the state; Value is the new
	// accumulated value of Key.
	EvOutput
	// EvStaged means the side has already consumed its barrier and is waiting
	// for the other side. The record at Pos was NOT consumed: the position
	// cursor does not advance and the same position must be pulled again once
	// the aligned checkpoint completes.
	EvStaged
	// EvBarrier means a barrier was consumed but the other input has not yet
	// reached the same barrier; this input is now blocked.
	EvBarrier
	// EvCheckpoint means both barriers arrived and a Snapshot was frozen.
	// Snapshot is non-nil only for this event.
	EvCheckpoint
)

// Event is the result of one Pull call.
type Event struct {
	Kind EventKind
	Side Side
	// Pos is the input position that was consumed (all kinds except EvIdle
	// and EvStaged, which do not consume a position).
	Pos int64
	// Key and Value describe an EvOutput event.
	Key   string
	Value int64
	// BarrierID identifies the barrier on EvBarrier and EvCheckpoint events.
	BarrierID int64
	// Snapshot is populated only on EvCheckpoint events.
	Snapshot *Snapshot
}

// Snapshot is the frozen aligned checkpoint.
type Snapshot struct {
	// BarrierID is the identifier of the aligned barrier pair.
	BarrierID int64
	// BarrierPos is the barrier position per input side.
	BarrierPos [2]int64
	// Values is a deep copy of the accumulated per-key sums.
	Values map[string]int64
}

// ResumePos returns the first position the caller must replay for side sn
// after restoring this snapshot: the position immediately after its barrier.
func (s *Snapshot) ResumePos(sn Side) int64 {
	return s.BarrierPos[sn] + 1
}

// Errors returned by the operator. All are sentinel values usable with
// errors.Is.
var (
	// ErrInvalidSide is returned for a side other than Left or Right.
	ErrInvalidSide = errors.New("alignedjoin: invalid side")
	// ErrInvalidItem is returned for a malformed pushed item.
	ErrInvalidItem = errors.New("alignedjoin: invalid item")
	// ErrEmptyQueue is returned by Pull when the requested side has nothing
	// queued.
	ErrEmptyQueue = errors.New("alignedjoin: queue empty")
	// ErrNonContiguousPos is returned when Pull positions skip or repeat a
	// position on one side.
	ErrNonContiguousPos = errors.New("alignedjoin: non-contiguous position")
	// ErrBarrierMismatch is returned when the two sides present barriers with
	// different identifiers.
	ErrBarrierMismatch = errors.New("alignedjoin: barrier id mismatch between sides")
	// ErrCheckpointInFlight is returned when a new barrier is presented while
	// the previous checkpoint is still waiting for the other side.
	ErrCheckpointInFlight = errors.New("alignedjoin: checkpoint already in flight")
	// ErrBarrierOrder is returned when a barrier identifier does not strictly
	// increase, including when a restored operator sees an old barrier.
	ErrBarrierOrder = errors.New("alignedjoin: barrier ids must strictly increase")
)

type sideState struct {
	queue   []Item // pending items in arrival order
	nextPos int64  // position of the next Pull (continuity check)
	blocked bool   // a barrier was consumed and alignment is incomplete
}

// Operator is the dual-input aligned-checkpoint accumulator.
// It is intended for single-goroutine use; the caller drives it through
// Push and Pull.
type Operator struct {
	values       map[string]int64
	sides        [2]sideState
	lastBarrier  int64 // highest completed barrier identifier
	cpInFlight   bool
	cpID         int64
	cpArrived    [2]bool
	cpBarrierPos [2]int64
}

// New creates an empty operator.
func New() *Operator {
	return &Operator{values: make(map[string]int64)}
}

// Restore recreates an operator from an aligned snapshot. The restored
// operator contains exactly the frozen state; the caller replays, for each
// side, the items starting at Snapshot.ResumePos(side) using the same
// consuming pull order as in the original run.
func Restore(s *Snapshot) *Operator {
	values := make(map[string]int64, len(s.Values))
	for k, v := range s.Values {
		values[k] = v
	}
	op := &Operator{
		values:      values,
		lastBarrier: s.BarrierID,
	}
	for i := range op.sides {
		// The next Pull must be the first position after the barrier.
		op.sides[i].nextPos = s.BarrierPos[i] + 1
	}
	return op
}

// Push appends an item to one input side. Items are buffered in arrival order
// and consumed by Pull. Items pushed while the side is blocked stay queued
// behind the barrier and cannot be processed until the aligned checkpoint
// completes.
func (o *Operator) Push(sn Side, item Item) error {
	if sn != Left && sn != Right {
		return ErrInvalidSide
	}
	switch item.Kind {
	case KindRecord:
		if item.BarrierID != 0 {
			return ErrInvalidItem
		}
	case KindBarrier:
		if item.BarrierID <= 0 {
			return ErrInvalidItem
		}
	default:
		return ErrInvalidItem
	}
	s := &o.sides[sn]
	s.queue = append(s.queue, item)
	return nil
}

// Pull consumes the next item of side sn at the given position. Positions on a
// side must be contiguous and start at zero.
//
// If the side is blocked behind a consumed barrier and the queued item is a
// record, Pull returns EvStaged and consumes nothing: the same position is
// pulled again after the checkpoint completes. ErrEmptyQueue (with an EvIdle
// event) is returned when the side has nothing queued, leaving the operator
// untouched so the caller can pull the other side instead.
func (o *Operator) Pull(sn Side, pos int64) (Event, error) {
	if sn != Left && sn != Right {
		return Event{Kind: EvIdle, Side: sn}, ErrInvalidSide
	}
	s := &o.sides[sn]
	if pos != s.nextPos {
		return Event{Kind: EvIdle, Side: sn}, ErrNonContiguousPos
	}
	if len(s.queue) == 0 {
		return Event{Kind: EvIdle, Side: sn, Pos: pos}, ErrEmptyQueue
	}
	item := s.queue[0]

	// Blocked sides do not process records and do not consume the position.
	// The item stays at the head of the queue for a later pull.
	if item.Kind == KindRecord && s.blocked {
		return Event{Kind: EvStaged, Side: sn, Pos: pos, Key: item.Key}, nil
	}

	// Validate barriers before consuming the position so a rejected barrier
	// leaves the queue and position cursor untouched.
	if item.Kind == KindBarrier {
		if err := o.validateBarrier(sn, item.BarrierID); err != nil {
			return Event{Kind: EvIdle, Side: sn, Pos: pos}, err
		}
	}

	s.queue = s.queue[1:]
	s.nextPos = pos + 1

	// Push rejects every Kind other than KindBarrier and KindRecord, so only
	// these two cases can reach this point.
	if item.Kind == KindBarrier {
		return o.commitBarrier(sn, pos, item.BarrierID)
	}
	o.values[item.Key] += item.Delta
	return Event{
		Kind:  EvOutput,
		Side:  sn,
		Pos:   pos,
		Key:   item.Key,
		Value: o.values[item.Key],
	}, nil
}

// validateBarrier checks whether a barrier at the head of side sn's queue may
// be consumed, without changing any state.
func (o *Operator) validateBarrier(sn Side, id int64) error {
	if id <= o.lastBarrier {
		return ErrBarrierOrder
	}
	if o.cpInFlight && o.cpArrived[sn] {
		// This side already consumed a barrier: only one checkpoint at a time.
		return ErrCheckpointInFlight
	}
	if o.cpInFlight && id != o.cpID {
		// The other side is already waiting: identifiers must match.
		return ErrBarrierMismatch
	}
	return nil
}

// commitBarrier records a validated barrier and performs the alignment
// handshake, freezing a Snapshot when both sides have arrived.
func (o *Operator) commitBarrier(sn Side, pos, id int64) (Event, error) {
	if !o.cpInFlight {
		o.cpInFlight = true
		o.cpID = id
	}

	o.sides[sn].blocked = true
	o.cpArrived[sn] = true
	o.cpBarrierPos[sn] = pos

	// Wait for the other side.
	if !(o.cpArrived[Left] && o.cpArrived[Right]) {
		return Event{Kind: EvBarrier, Side: sn, Pos: pos, BarrierID: id}, nil
	}

	// Aligned: freeze the checkpoint as a deep copy.
	snap := &Snapshot{
		BarrierID: id,
		Values:    make(map[string]int64, len(o.values)),
	}
	for k, v := range o.values {
		snap.Values[k] = v
	}
	snap.BarrierPos = o.cpBarrierPos

	// Release both sides. Queued records keep their positions; the caller
	// pulls them again starting at the resume position.
	for i := range o.sides {
		o.sides[i].blocked = false
	}
	o.cpInFlight = false
	o.cpID = 0
	o.cpArrived = [2]bool{}
	o.lastBarrier = id

	return Event{
		Kind:      EvCheckpoint,
		Side:      sn,
		Pos:       pos,
		BarrierID: id,
		Snapshot:  snap,
	}, nil
}

// Value returns the current accumulated value of key (zero when absent).
func (o *Operator) Value(key string) int64 {
	return o.values[key]
}

// PendingBarrier reports whether a checkpoint is waiting for the other side.
func (o *Operator) PendingBarrier() bool {
	return o.cpInFlight
}

// Blocked reports whether side sn has consumed a barrier whose checkpoint is
// not yet complete.
func (o *Operator) Blocked(sn Side) bool {
	if sn != Left && sn != Right {
		return false
	}
	return o.sides[sn].blocked
}

// LastBarrier returns the identifier of the last completed checkpoint barrier,
// or zero when no checkpoint has completed.
func (o *Operator) LastBarrier() int64 {
	return o.lastBarrier
}
