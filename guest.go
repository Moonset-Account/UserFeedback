package guesttimer

import (
	"fmt"
	"sync"
	"time"
)

// Kind identifies the clock a timer follows.
type Kind int

const (
	// KindRun timers follow the guest's accumulated runtime clock. That clock
	// only advances while the guest is running, so it is frozen for the whole
	// duration of a pause and is unaffected by host wall-clock corrections.
	KindRun Kind = iota

	// KindCalendar timers follow the injected guest calendar clock. That clock
	// tracks the host wall clock (including forward and backward corrections)
	// and keeps moving even while the guest is paused.
	KindCalendar
)

// String implements fmt.Stringer.
func (k Kind) String() string {
	switch k {
	case KindRun:
		return "run"
	case KindCalendar:
		return "calendar"
	default:
		return fmt.Sprintf("Kind(%d)", int(k))
	}
}

// Tick reports one timer expiry.
type Tick struct {
	ID   string
	Kind Kind
	// Deadline is the guest-clock reading (run nanoseconds or calendar
	// nanoseconds, depending on Kind) of the first expiry coalesced into this
	// tick.
	Deadline int64
	// Count is the number of whole periods that expired since the previous
	// report. It is 1 for an ordinary single expiry and greater than 1 when a
	// pause, a host time jump or a migration skipped several periods.
	Count int64
}

// TimerState is the serialisable state of a single timer.
type TimerState struct {
	ID     string
	Kind   Kind
	Period int64
	Next   int64
}

// GuestState is the complete serialisable guest timer state. It contains no
// host clock readings at all, which is what makes a save/restore or a
// migration independent of the destination host's monotonic origin.
type GuestState struct {
	RunNanos    int64
	WallNanos   int64
	Paused      bool
	TimerStates []TimerState
}

type timer struct {
	id     string
	kind   Kind
	period int64
	next   int64
}

// Guest owns the guest-side timer state and the two guest clocks.
type Guest struct {
	mu     sync.Mutex
	host   Host
	onTick func(Tick)

	running bool

	runNanos   int64
	monoAnchor int64

	wallNanos  int64
	wallAnchor int64

	timers []*timer
}

// NewGuest creates a running guest attached to host.
func NewGuest(host Host) *Guest {
	if host == nil {
		panic("guesttimer: nil host")
	}
	wall := host.Wall()
	return &Guest{
		host:       host,
		running:    true,
		runNanos:   0,
		monoAnchor: host.Monotonic(),
		// The calendar clock starts at the host wall reading so that calendar
		// deadlines are absolute guest calendar instants.
		wallNanos:  wall,
		wallAnchor: wall,
	}
}

// NewGuestFromState restores a guest from a previously saved state onto host.
// The destination host may have a completely unrelated monotonic origin and a
// different wall clock; both timers' next deadlines are taken from the saved
// guest state.
func NewGuestFromState(host Host, st GuestState) (*Guest, error) {
	if host == nil {
		return nil, fmt.Errorf("guesttimer: nil host")
	}
	g := &Guest{
		host:       host,
		running:    !st.Paused,
		runNanos:   st.RunNanos,
		monoAnchor: host.Monotonic(),
		wallNanos:  st.WallNanos,
		wallAnchor: host.Wall(),
	}
	seen := make(map[string]struct{}, len(st.TimerStates))
	for _, ts := range st.TimerStates {
		if ts.Period <= 0 {
			return nil, fmt.Errorf("guesttimer: timer %q has non-positive period %d", ts.ID, ts.Period)
		}
		if ts.Kind != KindRun && ts.Kind != KindCalendar {
			return nil, fmt.Errorf("guesttimer: timer %q has invalid kind %d", ts.ID, int(ts.Kind))
		}
		if _, dup := seen[ts.ID]; dup {
			return nil, fmt.Errorf("guesttimer: duplicate timer %q", ts.ID)
		}
		seen[ts.ID] = struct{}{}
		g.timers = append(g.timers, &timer{id: ts.ID, kind: ts.Kind, period: ts.Period, next: ts.Next})
	}
	return g, nil
}

// OnTick installs a callback invoked once per coalesced timer expiry, in timer
// registration order. The callback is never invoked while the guest lock is
// held.
func (g *Guest) OnTick(fn func(Tick)) {
	g.mu.Lock()
	g.onTick = fn
	g.mu.Unlock()
}

// AddTimer registers a periodic timer. The first expiry is one period after
// the current reading of the timer's clock.
func (g *Guest) AddTimer(id string, kind Kind, period time.Duration) error {
	if id == "" {
		return fmt.Errorf("guesttimer: empty timer id")
	}
	if period <= 0 {
		return fmt.Errorf("guesttimer: timer %q needs a positive period, got %v", id, period)
	}
	if kind != KindRun && kind != KindCalendar {
		return fmt.Errorf("guesttimer: timer %q has invalid kind %d", id, int(kind))
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	g.syncLocked()
	for _, t := range g.timers {
		if t.id == id {
			return fmt.Errorf("guesttimer: duplicate timer %q", id)
		}
	}
	var now int64
	if kind == KindRun {
		now = g.runNanos
	} else {
		now = g.wallNanos
	}
	g.timers = append(g.timers, &timer{id: id, kind: kind, period: int64(period), next: now + int64(period)})
	return nil
}

// AddRunTimer is shorthand for AddTimer(id, KindRun, period).
func (g *Guest) AddRunTimer(id string, period time.Duration) error {
	return g.AddTimer(id, KindRun, period)
}

// AddCalendarTimer is shorthand for AddTimer(id, KindCalendar, period).
func (g *Guest) AddCalendarTimer(id string, period time.Duration) error {
	return g.AddTimer(id, KindCalendar, period)
}

// Pause freezes the guest runtime clock. The calendar clock keeps following the
// host wall clock, so it may jump forwards or backwards while paused.
func (g *Guest) Pause() {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !g.running {
		return
	}
	g.syncLocked()
	g.running = false
}

// Resume re-starts the guest runtime clock at the current host monotonic
// reading. Host monotonic time consumed during the pause is not charged to the
// guest. Calendar time (including any jump during the pause) is applied.
//
// Any run or calendar timers that became due while paused are reported exactly
// once, with Count set to the number of periods that elapsed, and the next
// deadline stays on the original period phase.
func (g *Guest) Resume() []Tick {
	g.mu.Lock()
	if g.running {
		g.mu.Unlock()
		return nil
	}
	wall := g.host.Wall()
	g.wallNanos += wall - g.wallAnchor
	g.wallAnchor = wall
	g.monoAnchor = g.host.Monotonic()
	g.running = true
	ticks := g.dueLocked()
	g.mu.Unlock()
	g.dispatch(ticks)
	return ticks
}

// Poll advances the guest clocks to the current host readings and reports all
// expired timers. While paused, run timers cannot expire because the runtime
// clock is frozen, but calendar timers can still be reported.
func (g *Guest) Poll() []Tick {
	g.mu.Lock()
	g.syncLocked()
	ticks := g.dueLocked()
	g.mu.Unlock()
	g.dispatch(ticks)
	return ticks
}

// AdvanceHost advances an AdvanceableHost by d and then polls. It is a
// convenience for deterministic environments and returns an error when the
// host does not support explicit advancement.
func (g *Guest) AdvanceHost(d time.Duration) ([]Tick, error) {
	ah, ok := g.host.(AdvanceableHost)
	if !ok {
		return nil, fmt.Errorf("guesttimer: host %T cannot be advanced", g.host)
	}
	ah.Advance(d)
	return g.Poll(), nil
}

// RunNanos returns the guest runtime clock in nanoseconds.
func (g *Guest) RunNanos() int64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.syncLocked()
	return g.runNanos
}

// WallNanos returns the guest calendar clock in nanoseconds.
func (g *Guest) WallNanos() int64 {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.syncLocked()
	return g.wallNanos
}

// Running reports whether the guest runtime clock is advancing.
func (g *Guest) Running() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.running
}

// Save snapshots the complete guest timer state. No host clock reading is
// stored, so the state can be restored on a host with a different monotonic
// origin.
func (g *Guest) Save() GuestState {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.syncLocked()
	st := GuestState{
		RunNanos:  g.runNanos,
		WallNanos: g.wallNanos,
		Paused:    !g.running,
	}
	for _, t := range g.timers {
		st.TimerStates = append(st.TimerStates, TimerState{
			ID:     t.id,
			Kind:   t.kind,
			Period: t.period,
			Next:   t.next,
		})
	}
	return st
}

// Migrate saves the guest state and restores it onto target, returning the new
// guest. The receiver is left untouched.
func (g *Guest) Migrate(target Host) (*Guest, error) {
	return NewGuestFromState(target, g.Save())
}

// syncLocked folds host clock movement into the guest clocks.
func (g *Guest) syncLocked() {
	mono := g.host.Monotonic()
	wall := g.host.Wall()
	if g.running {
		if d := mono - g.monoAnchor; d > 0 {
			g.runNanos += d
		}
		g.monoAnchor = mono
	}
	g.wallNanos += wall - g.wallAnchor
	g.wallAnchor = wall
}

// dueLocked collects every timer whose deadline has been reached, coalescing
// each timer into one Tick. Callers must hold g.mu.
func (g *Guest) dueLocked() []Tick {
	var ticks []Tick
	for _, t := range g.timers {
		var now int64
		switch t.kind {
		case KindRun:
			if !g.running {
				continue
			}
			now = g.runNanos
		case KindCalendar:
			now = g.wallNanos
		}
		if now < t.next {
			continue
		}
		n := (now-t.next)/t.period + 1
		ticks = append(ticks, Tick{ID: t.id, Kind: t.kind, Deadline: t.next, Count: n})
		t.next += n * t.period
	}
	return ticks
}

func (g *Guest) dispatch(ticks []Tick) {
	if len(ticks) == 0 {
		return
	}
	g.mu.Lock()
	fn := g.onTick
	g.mu.Unlock()
	if fn == nil {
		return
	}
	for _, tk := range ticks {
		fn(tk)
	}
}

// Timers returns a snapshot of the registered timers ordered by registration.
func (g *Guest) Timers() []TimerState {
	g.mu.Lock()
	defer g.mu.Unlock()
	out := make([]TimerState, 0, len(g.timers))
	for _, t := range g.timers {
		out = append(out, TimerState{ID: t.id, Kind: t.kind, Period: t.period, Next: t.next})
	}
	return out
}
