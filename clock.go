package guesttimer

import (
	"sync"
	"time"
)

// Host is the host-side time source a Guest is attached to.
//
// Monotonic is the host monotonic clock in nanoseconds. It is only used as a
// delta source while the guest is running; its absolute origin is irrelevant
// and may differ completely between hosts.
//
// Wall is the host calendar clock in nanoseconds since the Unix epoch. It may
// be adjusted forwards or backwards by the environment (RTC sync, migration,
// administrator action).
type Host interface {
	Monotonic() int64
	Wall() int64
}

// AdvanceableHost is a Host whose clocks can be moved explicitly. It is meant
// for deterministic tests and simulator-style environments.
type AdvanceableHost interface {
	Host
	// Advance moves both the host monotonic clock and the host wall clock
	// forward by d. Non-positive durations are ignored because host monotonic
	// time does not run backwards.
	Advance(d time.Duration)
}

// SystemHost is a Host backed by the real process clocks.
type SystemHost struct{}

var processStart = time.Now()

// Monotonic returns nanoseconds since process start using the Go runtime's
// monotonic clock source.
func (SystemHost) Monotonic() int64 { return time.Since(processStart).Nanoseconds() }

// Wall returns the current Unix wall clock in nanoseconds.
func (SystemHost) Wall() int64 { return time.Now().UnixNano() }

// ManualHost is a fully deterministic Host driven by the caller.
type ManualHost struct {
	mu   sync.Mutex
	mono int64
	wall int64
}

// NewManualHost creates a ManualHost with the given absolute clock readings.
func NewManualHost(monotonic, wall int64) *ManualHost {
	return &ManualHost{mono: monotonic, wall: wall}
}

// Monotonic implements Host.
func (h *ManualHost) Monotonic() int64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.mono
}

// Wall implements Host.
func (h *ManualHost) Wall() int64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.wall
}

// Advance implements AdvanceableHost.
func (h *ManualHost) Advance(d time.Duration) {
	if d <= 0 {
		return
	}
	h.mu.Lock()
	h.mono += int64(d)
	h.wall += int64(d)
	h.mu.Unlock()
}

// JumpWall moves only the host wall clock, modelling an RTC adjustment. It
// accepts negative durations. The host monotonic clock is untouched.
func (h *ManualHost) JumpWall(d time.Duration) {
	h.mu.Lock()
	h.wall += int64(d)
	h.mu.Unlock()
}
