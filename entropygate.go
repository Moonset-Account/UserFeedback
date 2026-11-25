// Package entropygate implements streaming health gating for two independent
// raw entropy sources (lanes) in a hardware security module. It is intended to
// sit between the raw sources and the key generator.
//
// Two health tests are applied independently to each lane:
//
//   - A repetition count test. The lane is isolated as soon as the current run
//     of consecutive identical symbols reaches Config.RepetitionThreshold.
//     The run length is kept across fixed-window boundaries and across Feed
//     calls.
//   - A fixed-window test. Symbols are collected into non-overlapping windows
//     of Config.WindowSize symbols. The number of occurrences of the window's
//     own first symbol is counted; as soon as that count reaches
//     Config.FirstSymbolThreshold the lane is isolated. A window can therefore
//     also fail before it is complete.
//
// A window is buffered until it is complete and no symbol of it is returned
// before the window has passed both tests. Consequently a window header that
// is later judged unhealthy is never handed to the caller, even when the
// failure is discovered only at the end of the window or in a later chunk.
//
// Isolation is latched. Feed returns ErrLaneIsolated for a latched lane until
// the device is explicitly reset with Gate.Reset. After a reset each lane must
// pass Config.ResetCleanWindows new complete windows before it may supply
// entropy again; the symbols of those probation windows are discarded
// (they establish health but are not forwarded). A failure during probation
// latches the lane again.
//
// The two lanes keep completely separate state: isolating one lane does not
// stop or alter the other.
package entropygate

import (
	"errors"
	"fmt"
	"sync"
)

// Lane identifies one of the two independent entropy sources.
type Lane uint8

const (
	// LaneA is the first entropy source.
	LaneA Lane = 0
	// LaneB is the second entropy source.
	LaneB Lane = 1
)

// String returns the lane name, or a numeric representation for invalid lanes.
func (l Lane) String() string {
	switch l {
	case LaneA:
		return "A"
	case LaneB:
		return "B"
	default:
		return fmt.Sprintf("Lane(%d)", uint8(l))
	}
}

var (
	// ErrInvalidConfig is returned by New when a threshold is not usable.
	ErrInvalidConfig = errors.New("entropygate: invalid configuration")
	// ErrInvalidLane is returned for a lane other than LaneA or LaneB.
	ErrInvalidLane = errors.New("entropygate: invalid lane")
	// ErrLaneIsolated is returned when feeding a lane that is latched as
	// isolated. The device must be explicitly reset before it can be fed
	// again.
	ErrLaneIsolated = errors.New("entropygate: lane is isolated; explicit device reset required")
)

// Config holds the caller-supplied health-test thresholds and the reset
// probation length. RepetitionThreshold, WindowSize and FirstSymbolThreshold
// must be positive; ResetCleanWindows must be non-negative.
//
// A threshold of 1 is legal and makes the corresponding test fire as early as
// the test can observe a result. A FirstSymbolThreshold larger than WindowSize
// is also legal; the fixed-window test then never fires because a window
// cannot contain that many occurrences of its first symbol.
type Config struct {
	// RepetitionThreshold is the number of consecutive identical symbols in
	// one lane that isolates it. Reaching the threshold isolates the lane
	// immediately, in the middle of a window if applicable.
	RepetitionThreshold int

	// WindowSize is the number of symbols in each fixed, non-overlapping
	// fixed-window test window.
	WindowSize int

	// FirstSymbolThreshold is the number of occurrences of a window's first
	// symbol that isolates the lane. Reaching the threshold isolates the lane
	// immediately, even if the window is not complete yet.
	FirstSymbolThreshold int

	// ResetCleanWindows is the number of complete passing windows each lane
	// must produce after Reset before it may supply entropy again. Zero means
	// a lane becomes usable immediately after reset.
	ResetCleanWindows int
}

func (c Config) validate() error {
	switch {
	case c.RepetitionThreshold <= 0:
		return fmt.Errorf("%w: RepetitionThreshold must be > 0, got %d", ErrInvalidConfig, c.RepetitionThreshold)
	case c.WindowSize <= 0:
		return fmt.Errorf("%w: WindowSize must be > 0, got %d", ErrInvalidConfig, c.WindowSize)
	case c.FirstSymbolThreshold <= 0:
		return fmt.Errorf("%w: FirstSymbolThreshold must be > 0, got %d", ErrInvalidConfig, c.FirstSymbolThreshold)
	case c.ResetCleanWindows < 0:
		return fmt.Errorf("%w: ResetCleanWindows must be >= 0, got %d", ErrInvalidConfig, c.ResetCleanWindows)
	}
	return nil
}

// Outcome describes the effect of one Feed call.
type Outcome struct {
	// Lane is the lane that was fed.
	Lane Lane

	// Accepted is the number of symbols consumed from the input. It is less
	// than len(input) when isolation happened partway through the chunk; the
	// remaining symbols are ignored.
	Accepted int

	// Released contains the symbols that passed health checks and may be
	// forwarded to the key generator, in order. Only complete windows are
	// released. Windows completed before the isolation point of this chunk are
	// included even if a later symbol of the same chunk isolates the lane. If
	// the lane is in reset probation, passing windows are counted towards the
	// probation but are not released.
	Released []byte

	// Isolated reports whether the lane is latched as isolated when Feed
	// returns.
	Isolated bool

	// IsolationPoint is the 1-based position, within this Feed's input, of
	// the symbol that caused isolation. It is -1 when this call did not
	// isolate the lane. The symbol at IsolationPoint and all later symbols of
	// the chunk are discarded.
	IsolationPoint int

	// ProbationRemaining is the number of additional complete passing windows
	// required before the lane may release entropy. It is zero while active
	// and while isolated.
	ProbationRemaining int
}

// Status is an instantaneous view of one lane.
type Status struct {
	// Isolated reports whether the lane is latched and requires a device
	// reset.
	Isolated bool

	// ProbationRemaining is the number of further passing windows required
	// after a reset before the lane may release entropy.
	ProbationRemaining int

	// Active reports whether a complete passing window fed now would be
	// released. It is true exactly when the lane is not isolated and
	// ProbationRemaining is zero.
	Active bool

	// Buffered is the number of symbols currently held in the incomplete
	// window.
	Buffered int

	// RunLength is the current length of the consecutive-identical-symbol run
	// at the end of the lane's stream.
	RunLength int
}

type laneState struct {
	mu sync.Mutex

	runSymbol byte
	runLength int

	buffer []byte
	// firstCount is the number of occurrences of buffer[0] in the current
	// (possibly incomplete) window. It is only meaningful when buffer is not
	// empty.
	firstCount int

	isolated  bool
	probation int
}

func (ls *laneState) resetWindow() {
	ls.buffer = ls.buffer[:0]
	ls.firstCount = 0
}

// Gate is a streaming health gate for two independent entropy lanes. It is
// safe for concurrent use; each lane has its own state and lock.
type Gate struct {
	cfg   Config
	lanes [2]laneState
}

// New creates a gate with the caller-supplied thresholds. It returns an error
// wrapping ErrInvalidConfig if a threshold is not usable.
func New(cfg Config) (*Gate, error) {
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	return &Gate{cfg: cfg}, nil
}

func (g *Gate) laneState(lane Lane) (*laneState, error) {
	switch lane {
	case LaneA, LaneB:
		return &g.lanes[lane], nil
	default:
		return nil, fmt.Errorf("%w: %s", ErrInvalidLane, lane)
	}
}

// Feed consumes a chunk of raw symbols from one lane. Chunks may have any
// length, including zero, and the same stream may be split arbitrarily across
// calls.
//
// Feed returns ErrLaneIsolated when the lane is already latched; the input is
// not consumed. A non-nil, non-ErrLaneIsolated error is returned only for an
// invalid lane.
func (g *Gate) Feed(lane Lane, symbols []byte) (Outcome, error) {
	ls, err := g.laneState(lane)
	if err != nil {
		return Outcome{}, err
	}

	ls.mu.Lock()
	defer ls.mu.Unlock()

	out := Outcome{
		Lane:           lane,
		IsolationPoint: -1,
	}
	if ls.isolated {
		out.Isolated = true
		return out, ErrLaneIsolated
	}

	for i, symbol := range symbols {
		// The repetition counter deliberately spans fixed-window boundaries
		// and Feed calls.
		if ls.runLength > 0 && symbol == ls.runSymbol {
			ls.runLength++
		} else {
			ls.runSymbol = symbol
			ls.runLength = 1
		}

		out.Accepted = i + 1

		if ls.runLength >= g.cfg.RepetitionThreshold {
			g.isolate(ls, &out)
			return out, nil
		}

		if len(ls.buffer) == 0 {
			ls.firstCount = 1
		} else if symbol == ls.buffer[0] {
			ls.firstCount++
		}
		ls.buffer = append(ls.buffer, symbol)

		if ls.firstCount >= g.cfg.FirstSymbolThreshold {
			// Detected before the window is complete: the whole window,
			// including its header, is discarded.
			g.isolate(ls, &out)
			return out, nil
		}

		if len(ls.buffer) < g.cfg.WindowSize {
			continue
		}

		// The window is complete and has passed both tests. Only now may its
		// symbols leave the gate.
		if ls.probation > 0 {
			ls.probation--
		} else {
			// append copies the symbols, so the buffer can be reused for the
			// next window without aliasing Released.
			out.Released = append(out.Released, ls.buffer...)
		}
		out.ProbationRemaining = ls.probation
		ls.resetWindow()
	}

	out.Isolated = ls.isolated
	out.ProbationRemaining = ls.probation
	return out, nil
}

// isolate latches the lane and discards all buffered symbols of the current
// window.
func (g *Gate) isolate(ls *laneState, out *Outcome) {
	ls.isolated = true
	ls.probation = 0
	ls.resetWindow()
	out.Isolated = true
	out.ProbationRemaining = 0
	out.IsolationPoint = out.Accepted
}

// Reset performs an explicit device reset. It clears the health state and any
// partial windows of both lanes and puts both lanes into reset probation:
// each must pass Config.ResetCleanWindows new complete windows before it may
// release entropy. Reset may be called at any time, including while a lane is
// isolated or already active.
func (g *Gate) Reset() {
	// Always lock in lane order to keep Reset deadlock-free; Feed locks only
	// one lane at a time.
	for i := range g.lanes {
		ls := &g.lanes[i]
		ls.mu.Lock()
		ls.runSymbol = 0
		ls.runLength = 0
		ls.resetWindow()
		ls.isolated = false
		ls.probation = g.cfg.ResetCleanWindows
		ls.mu.Unlock()
	}
}

// Status returns a snapshot of one lane's health-gate state.
func (g *Gate) Status(lane Lane) (Status, error) {
	ls, err := g.laneState(lane)
	if err != nil {
		return Status{}, err
	}
	ls.mu.Lock()
	defer ls.mu.Unlock()
	return Status{
		Isolated:           ls.isolated,
		ProbationRemaining: ls.probation,
		Active:             !ls.isolated && ls.probation == 0,
		Buffered:           len(ls.buffer),
		RunLength:          ls.runLength,
	}, nil
}
