// Package dedup implements an anti-replay deduplication filter for the
// receiving end of an encrypted tunnel.
//
// During a key rotation the receiver may see integrity-valid packets from
// two key generations at once. The filter keeps one bounded sliding
// sequence-number window per (security association, key generation) pair:
//
//   - the first occurrence of an in-window, out-of-order packet is accepted;
//   - a packet whose sequence number was already received is rejected;
//   - a packet whose sequence number fell behind the window is rejected;
//   - after rotation, the old generation may keep advancing its own window
//     only inside the configured overlap period, and is fully independent
//     of the new generation's window.
//
// All methods are safe for concurrent use: if two threads submit the same
// packet, exactly one of them observes acceptance. Window state can be
// captured with Snapshot and reloaded with Restore; reloaded state still
// recognizes previously processed sequence numbers.
package dedup

import (
	"errors"
	"sync"
	"time"
)

// defaultWindowSize is used when Config.WindowSize is zero.
const defaultWindowSize = 64

// Config tunes a Filter.
type Config struct {
	// WindowSize is the number of sequence numbers tracked per generation
	// window. Zero selects defaultWindowSize.
	WindowSize uint64
	// Overlap is how long a superseded key generation may still accept
	// packets after a rotation. Zero means the old generation stops
	// accepting immediately at rotation time.
	Overlap time.Duration
	// Now supplies the current time; nil selects time.Now. Tests inject a
	// fake clock through this hook.
	Now func() time.Time
}

func (c Config) normalize() Config {
	if c.WindowSize == 0 {
		c.WindowSize = defaultWindowSize
	}
	if c.Now == nil {
		c.Now = time.Now
	}
	return c
}

// Errors returned by Filter management methods.
var (
	ErrUnknownSA          = errors.New("dedup: unknown security association")
	ErrSAExists           = errors.New("dedup: security association already registered")
	ErrGenerationExists   = errors.New("dedup: generation already registered")
	ErrWindowSizeMismatch = errors.New("dedup: snapshot window size does not match config")
)

// Filter is a concurrent anti-replay deduplicator keyed by
// (security association, key generation).
type Filter struct {
	mu  sync.Mutex
	cfg Config
	sas map[string]*saState
}

type saState struct {
	current uint64
	gens    map[uint64]*genState
}

type genState struct {
	win       window
	hasExpiry bool
	expiresAt time.Time
}

// NewFilter returns an empty Filter using cfg.
func NewFilter(cfg Config) *Filter {
	return &Filter{
		cfg: cfg.normalize(),
		sas: make(map[string]*saState),
	}
}

// AddSA registers a security association whose initial key generation is
// gen. Packets for unregistered SAs are rejected by Accept.
func (f *Filter) AddSA(sa string, gen uint64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.sas[sa]; ok {
		return ErrSAExists
	}
	f.sas[sa] = &saState{
		current: gen,
		gens: map[uint64]*genState{
			gen: {win: newWindow(f.cfg.WindowSize)},
		},
	}
	return nil
}

// Rotate moves sa to a new key generation. The previous generation keeps
// its own window and may continue to accept packets until the configured
// overlap period elapses; afterwards every packet for it is rejected. The
// new generation starts with a fresh, independent window.
func (f *Filter) Rotate(sa string, newGen uint64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	s, ok := f.sas[sa]
	if !ok {
		return ErrUnknownSA
	}
	if _, ok := s.gens[newGen]; ok {
		return ErrGenerationExists
	}
	old := s.gens[s.current]
	if !old.hasExpiry {
		old.hasExpiry = true
		old.expiresAt = f.cfg.Now().Add(f.cfg.Overlap)
	}
	s.gens[newGen] = &genState{win: newWindow(f.cfg.WindowSize)}
	s.current = newGen
	return nil
}

// Accept reports whether the packet identified by (sa, gen, seq) is seen
// for the first time and may be processed. It returns false for packets
// already received, packets behind the window, packets of an expired old
// generation, and packets of unknown SAs or generations. Concurrent calls
// with the same identifiers yield true exactly once.
func (f *Filter) Accept(sa string, gen uint64, seq uint64) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	g, ok := f.genLocked(sa, gen)
	if !ok {
		return false
	}
	if g.hasExpiry && !f.cfg.Now().Before(g.expiresAt) {
		return false
	}
	return g.win.accept(seq)
}

// Received reports whether seq is currently recorded as received in the
// window of (sa, gen). Sequence numbers that have already fallen behind
// the window are no longer individually tracked; Received reports false
// for them even though Accept would still reject them.
func (f *Filter) Received(sa string, gen uint64, seq uint64) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	g, ok := f.genLocked(sa, gen)
	if !ok {
		return false
	}
	return g.win.received(seq)
}

func (f *Filter) genLocked(sa string, gen uint64) (*genState, bool) {
	s, ok := f.sas[sa]
	if !ok {
		return nil, false
	}
	g, ok := s.gens[gen]
	if !ok {
		return nil, false
	}
	return g, true
}

// Snapshot captures the full deduplication state of the filter. The
// returned value is a deep copy and remains valid if the filter keeps
// accepting packets afterwards.
func (f *Filter) Snapshot() Snapshot {
	f.mu.Lock()
	defer f.mu.Unlock()
	snap := Snapshot{
		WindowSize: f.cfg.WindowSize,
		SAs:        make(map[string]SASnapshot, len(f.sas)),
	}
	for id, s := range f.sas {
		ss := SASnapshot{
			Current: s.current,
			Gens:    make(map[uint64]GenSnapshot, len(s.gens)),
		}
		for gen, g := range s.gens {
			gs := GenSnapshot{
				HasExpiry: g.hasExpiry,
				ExpiresAt: g.expiresAt,
				Window: WindowSnapshot{
					Started: g.win.started,
					Highest: g.win.highest,
					Bits:    append([]uint64(nil), g.win.bits...),
				},
			}
			ss.Gens[gen] = gs
		}
		snap.SAs[id] = ss
	}
	return snap
}

// Restore rebuilds a Filter from a snapshot taken by Snapshot. The
// restored filter rejects every sequence number that was already processed
// when the snapshot was taken, including ones that since fell behind the
// window, and honours any old-generation overlap deadline still in force.
func Restore(cfg Config, snap Snapshot) (*Filter, error) {
	ncfg := cfg.normalize()
	if snap.WindowSize != ncfg.WindowSize {
		return nil, ErrWindowSizeMismatch
	}
	f := &Filter{cfg: ncfg, sas: make(map[string]*saState, len(snap.SAs))}
	for id, ss := range snap.SAs {
		s := &saState{
			current: ss.Current,
			gens:    make(map[uint64]*genState, len(ss.Gens)),
		}
		for gen, gs := range ss.Gens {
			w := newWindow(ncfg.WindowSize)
			w.started = gs.Window.Started
			w.highest = gs.Window.Highest
			copy(w.bits, gs.Window.Bits)
			s.gens[gen] = &genState{
				win:       w,
				hasExpiry: gs.HasExpiry,
				expiresAt: gs.ExpiresAt,
			}
		}
		f.sas[id] = s
	}
	return f, nil
}

// Snapshot is a serializable copy of a Filter's state.
type Snapshot struct {
	WindowSize uint64
	SAs        map[string]SASnapshot
}

// SASnapshot is the snapshot of one security association.
type SASnapshot struct {
	Current uint64
	Gens    map[uint64]GenSnapshot
}

// GenSnapshot is the snapshot of one key generation.
type GenSnapshot struct {
	Window    WindowSnapshot
	HasExpiry bool
	ExpiresAt time.Time
}

// WindowSnapshot is the snapshot of one sequence-number window.
type WindowSnapshot struct {
	Started bool
	Highest uint64
	Bits    []uint64
}

// window is a bounded sliding replay window over sequence numbers. Bit i
// of the bitmap (counted from the most recent sequence number) records
// whether highest-i has been received.
type window struct {
	size    uint64
	started bool
	highest uint64
	bits    []uint64
}

func newWindow(size uint64) window {
	return window{
		size: size,
		bits: make([]uint64, (size+63)/64),
	}
}

// accept marks seq and reports whether it was unseen and inside the
// window.
func (w *window) accept(seq uint64) bool {
	if w.size == 0 {
		return false
	}
	if !w.started {
		w.started = true
		w.highest = seq
		w.bits[0] |= 1
		return true
	}
	if seq > w.highest {
		w.shift(seq - w.highest)
		w.highest = seq
		w.bits[0] |= 1
		return true
	}
	diff := w.highest - seq
	if diff >= w.size {
		return false // fell behind the window
	}
	mask := uint64(1) << (diff % 64)
	word := diff / 64
	if w.bits[word]&mask != 0 {
		return false // duplicate
	}
	w.bits[word] |= mask
	return true
}

// received reports whether seq is inside the window and marked.
func (w *window) received(seq uint64) bool {
	if !w.started || seq > w.highest {
		return false
	}
	diff := w.highest - seq
	if diff >= w.size {
		return false
	}
	return w.bits[diff/64]&(uint64(1)<<(diff%64)) != 0
}

// shift advances the bitmap by n positions: the entry for sequence number
// s moves to the slot of s relative to the new highest.
func (w *window) shift(n uint64) {
	if n >= w.size {
		for i := range w.bits {
			w.bits[i] = 0
		}
		return
	}
	words := int(n / 64)
	rem := uint(n % 64)
	if words > 0 {
		for i := len(w.bits) - 1; i >= 0; i-- {
			if i-words >= 0 {
				w.bits[i] = w.bits[i-words]
			} else {
				w.bits[i] = 0
			}
		}
	}
	if rem > 0 {
		for i := len(w.bits) - 1; i >= 0; i-- {
			var carry uint64
			if i > 0 {
				carry = w.bits[i-1] >> (64 - rem)
			}
			w.bits[i] = (w.bits[i] << rem) | carry
		}
	}
}
