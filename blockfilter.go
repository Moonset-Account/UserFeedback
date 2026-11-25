// Package blockfilter implements a compact, generational counting filter used
// as a negative index for object blocks.
//
// A caller supplies the hash positions of a block, one uint32 slot index per
// hash. Every primary slot owns a four-bit saturating counter. Counts above 15
// spill into a sparse overflow map keyed by slot index, so the filter never
// keeps a complete key table for each inserted block. A block can be skipped
// when any of its positions has a zero counter: zero always means definitely
// absent. A positive counter means "maybe present" and may be a false positive.
//
// Mutations are submitted as one batch. A batch is validated and applied to a
// private copy of the current state, then published as one new immutable
// generation. Snapshots already handed to readers stay valid and immutable, so
// old readers can keep judging against the generation they pinned.
package blockfilter

import (
	"errors"
	"fmt"
	"math"
	"sync"
	"sync/atomic"
)

// MaxSlots is the largest slot count accepted by New. It keeps packed-nibble
// arithmetic, uint32 positions, and byte offsets comfortably in range.
const MaxSlots uint32 = 1 << 28

var (
	// ErrNoSlots reports a zero slot count.
	ErrNoSlots = errors.New("blockfilter: slot count must be greater than zero")
	// ErrTooManySlots reports a slot count above MaxSlots.
	ErrTooManySlots = errors.New("blockfilter: slot count exceeds MaxSlots")
	// ErrEmptyBatch reports an Apply call with no changes.
	ErrEmptyBatch = errors.New("blockfilter: change batch is empty")
	// ErrNoPositions reports a change without any hash positions.
	ErrNoPositions = errors.New("blockfilter: change contains no hash positions")
	// ErrPositionOutOfRange reports a hash position outside the filter.
	ErrPositionOutOfRange = errors.New("blockfilter: hash position is outside the filter")
	// ErrUnknownKind reports a change kind other than KindAdd or KindRemove.
	ErrUnknownKind = errors.New("blockfilter: unknown change kind")
	// ErrRemoveNotHeld reports a release that asks for more references than the
	// slot currently holds. Such a batch is rejected without publishing.
	ErrRemoveNotHeld = errors.New("blockfilter: remove requires more references than are held")
	// ErrCounterOverflow reports an effective counter that cannot be represented
	// as a uint32.
	ErrCounterOverflow = errors.New("blockfilter: counter overflow")
)

// Kind is the direction of one block mutation.
type Kind uint8

const (
	// KindAdd adds one reference for every supplied hash position.
	KindAdd Kind = iota + 1
	// KindRemove removes one reference for every supplied hash position.
	KindRemove
)

// String returns a stable name for the kind.
func (k Kind) String() string {
	switch k {
	case KindAdd:
		return "add"
	case KindRemove:
		return "remove"
	default:
		return fmt.Sprintf("kind(%d)", uint8(k))
	}
}

// Change describes one block mutation. Positions are the caller-provided hash
// positions, i.e. primary slot indices for that block.
//
// Every occurrence counts: KindAdd increments each entry in Positions once, and
// KindRemove decrements each entry once. Duplicate positions inside one block
// and repeated adds therefore stay symmetric with repeated releases as long as
// the caller supplies the same positions for every add and remove of a block.
type Change struct {
	Kind      Kind
	Positions []uint32
}

// four-bit nibbles saturate at this value; counts above it live in overflow.
const nibbleMax uint32 = 15

// maxExcess bounds the sparse overflow contribution so 15+excess still fits
// in uint32.
const maxExcess uint32 = math.MaxUint32 - nibbleMax

// Snapshot is one immutable published generation of the filter. It is safe for
// many goroutines to read concurrently, including after newer generations have
// been published.
type Snapshot struct {
	slots      uint32
	generation uint64
	// counters packs two four-bit counters per byte, slot i in nibble i.
	counters []byte
	// overflow holds the amount above 15 for saturated slots. It is only
	// populated for slots whose effective count exceeds 15, so it stays sparse.
	// A missing entry and a zero entry both mean "no excess"; removals delete
	// the entry instead of leaving a redundant zero.
	overflow map[uint32]uint32
}

// Slots returns the number of primary slots.
func (s *Snapshot) Slots() uint32 { return s.slots }

// Generation returns the generation number of this snapshot. The empty filter
// published by New is generation 0; every successful Apply publishes exactly
// one new generation.
func (s *Snapshot) Generation() uint64 { return s.generation }

// MaybePresent reports whether the counter at pos is nonzero, meaning the
// block hashing to that position may be present. A false result is definitive:
// no live reference hashes to that position. An out-of-range position has no
// counter and therefore reports false.
func (s *Snapshot) MaybePresent(pos uint32) bool {
	if pos >= s.slots {
		return false
	}
	if s.nibble(pos) != 0 {
		return true
	}
	return s.overflow[pos] != 0
}

// BlockDefinitelyAbsent reports whether the block described by positions is
// definitely absent. Any position with a zero counter proves absence. An
// out-of-range position is treated as a zero counter. An empty position list
// yields false, because no evidence can rule the block out.
func (s *Snapshot) BlockDefinitelyAbsent(positions []uint32) bool {
	if len(positions) == 0 {
		return false
	}
	for _, pos := range positions {
		if !s.MaybePresent(pos) {
			return true
		}
	}
	return false
}

// BlockMaybePresent reports whether every position is nonzero, meaning the
// block may be present. An empty position list yields false.
func (s *Snapshot) BlockMaybePresent(positions []uint32) bool {
	if len(positions) == 0 {
		return false
	}
	for _, pos := range positions {
		if !s.MaybePresent(pos) {
			return false
		}
	}
	return true
}

// Counter returns the effective reference count at pos, including the sparse
// overflow contribution. ok is false when pos is out of range.
func (s *Snapshot) Counter(pos uint32) (count uint32, ok bool) {
	if pos >= s.slots {
		return 0, false
	}
	return s.nibble(pos) + s.overflow[pos], true
}

// PrimaryCounter returns the four-bit primary counter at pos, in 0..15.
// Counts above 15 saturate the primary counter and add OverflowExcess. ok is
// false when pos is out of range.
func (s *Snapshot) PrimaryCounter(pos uint32) (count uint32, ok bool) {
	if pos >= s.slots {
		return 0, false
	}
	return s.nibble(pos), true
}

// OverflowExcess returns the sparse overflow amount above 15 at pos. ok is
// false when pos is out of range.
func (s *Snapshot) OverflowExcess(pos uint32) (excess uint32, ok bool) {
	if pos >= s.slots {
		return 0, false
	}
	return s.overflow[pos], true
}

// OverflowEntries returns the number of saturated slots that currently carry a
// nonzero overflow amount.
func (s *Snapshot) OverflowEntries() int { return len(s.overflow) }

func (s *Snapshot) nibble(pos uint32) uint32 {
	b := s.counters[pos>>1]
	if pos&1 == 0 {
		return uint32(b & 0x0f)
	}
	return uint32(b >> 4)
}

func (s *Snapshot) setNibble(pos, v uint32) {
	i := pos >> 1
	if pos&1 == 0 {
		s.counters[i] = (s.counters[i] & 0xf0) | byte(v&0x0f)
		return
	}
	s.counters[i] = (s.counters[i] & 0x0f) | byte((v&0x0f)<<4)
}

func (s *Snapshot) clone() *Snapshot {
	next := &Snapshot{
		slots:      s.slots,
		generation: s.generation,
		counters:   make([]byte, len(s.counters)),
	}
	copy(next.counters, s.counters)
	// Keep the overflow map nil until a slot actually spills; the map is only
	// sparse bookkeeping and costs nothing while empty.
	if len(s.overflow) > 0 {
		next.overflow = make(map[uint32]uint32, len(s.overflow))
		for pos, excess := range s.overflow {
			next.overflow[pos] = excess
		}
	}
	return next
}

// increment adds one reference at pos, moving into the sparse overflow region
// once the primary counter saturates.
func (s *Snapshot) increment(pos uint32) error {
	base := s.nibble(pos)
	if base < nibbleMax {
		s.setNibble(pos, base+1)
		return nil
	}
	excess := s.overflow[pos]
	if excess >= maxExcess {
		return ErrCounterOverflow
	}
	if s.overflow == nil {
		s.overflow = make(map[uint32]uint32)
	}
	s.overflow[pos] = excess + 1
	return nil
}

// decrement removes one reference at pos. It consumes the sparse overflow
// first, deleting the entry when it reaches zero, and only then lowers the
// primary counter. A zero counter is rejected so an unmatched release cannot
// drive a held block to "definitely absent".
func (s *Snapshot) decrement(pos uint32) error {
	if excess := s.overflow[pos]; excess != 0 {
		if excess == 1 {
			delete(s.overflow, pos)
			return nil
		}
		s.overflow[pos] = excess - 1
		return nil
	}
	base := s.nibble(pos)
	if base == 0 {
		return ErrRemoveNotHeld
	}
	s.setNibble(pos, base-1)
	return nil
}

// applyBatch applies each change in order to s. On any validation failure it
// returns the error; the caller discards s without publishing it, so the whole
// batch is rejected atomically.
func (s *Snapshot) applyBatch(batch []Change) error {
	for i := range batch {
		ch := &batch[i]
		if ch.Kind != KindAdd && ch.Kind != KindRemove {
			return fmt.Errorf("%w: %s", ErrUnknownKind, ch.Kind)
		}
		if len(ch.Positions) == 0 {
			return ErrNoPositions
		}
		for _, pos := range ch.Positions {
			if pos >= s.slots {
				return fmt.Errorf("%w: position %d, slots %d", ErrPositionOutOfRange, pos, s.slots)
			}
		}
		switch ch.Kind {
		case KindAdd:
			for _, pos := range ch.Positions {
				if err := s.increment(pos); err != nil {
					return err
				}
			}
		case KindRemove:
			for _, pos := range ch.Positions {
				if err := s.decrement(pos); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// Filter is a generational container. Its current snapshot is replaced atomically
// by each successful Apply, while previously returned snapshots remain valid.
type Filter struct {
	slots uint32
	state atomic.Pointer[Snapshot]
	mu    sync.Mutex // serialises read-copy-publish of batches
}

// New creates an empty filter with the requested number of primary slots.
func New(slots uint32) (*Filter, error) {
	if slots == 0 {
		return nil, ErrNoSlots
	}
	if slots > MaxSlots {
		return nil, ErrTooManySlots
	}
	empty := &Snapshot{
		slots:    slots,
		counters: make([]byte, (uint64(slots)+1)/2),
	}
	f := &Filter{slots: slots}
	f.state.Store(empty)
	return f, nil
}

// Snapshot pins and returns the filter's current generation. The returned value
// never changes afterwards, so readers can keep judging against it while newer
// generations are published.
func (f *Filter) Snapshot() *Snapshot { return f.state.Load() }

// Generation returns the generation currently published.
func (f *Filter) Generation() uint64 { return f.state.Load().generation }

// Slots returns the number of primary slots.
func (f *Filter) Slots() uint32 { return f.slots }

// Apply validates and applies the whole batch to a private copy of the current
// state, then publishes the result as exactly one new immutable generation. If
// any change is invalid, no change is published and the current generation is
// left untouched.
//
// Apply is safe to call concurrently. Since batches are serialised, the returned
// snapshot is the generation that included this batch.
func (f *Filter) Apply(batch []Change) (*Snapshot, error) {
	if len(batch) == 0 {
		return nil, ErrEmptyBatch
	}
	f.mu.Lock()
	defer f.mu.Unlock()

	current := f.state.Load()
	next := current.clone()
	if err := next.applyBatch(batch); err != nil {
		return nil, err
	}
	next.generation = current.generation + 1
	f.state.Store(next)
	return next, nil
}
