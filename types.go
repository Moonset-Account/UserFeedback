package capacitycontroller

import "fmt"

// Observation is one reported interval for one generation of one instance.
// It carries the request and busy increments observed inside [Start, End).
//
// Rev is 1-based revision number for the tuple (Instance, Gen, Start, End):
// a later ingest with a higher Rev replaces the previous record, while a
// smaller or conflicting equal Rev is rejected.
type Observation struct {
	Instance string
	Gen      int
	Start    int64
	End      int64
	Rev      int
	Requests int64
	Busy     int64
}

func (o Observation) validate() error {
	if o.Instance == "" {
		return fmt.Errorf("observation: instance id is empty")
	}
	if o.Gen < 0 {
		return fmt.Errorf("observation: negative generation %d", o.Gen)
	}
	if o.End <= o.Start {
		return fmt.Errorf("observation: non-positive interval [%d,%d)", o.Start, o.End)
	}
	if o.Rev < 1 {
		return fmt.Errorf("observation: revision must be >= 1, got %d", o.Rev)
	}
	if o.Requests < 0 || o.Busy < 0 {
		return fmt.Errorf("observation: negative request/busy increment")
	}
	if o.Busy > o.End-o.Start {
		return fmt.Errorf("observation: busy %d exceeds interval length %d", o.Busy, o.End-o.Start)
	}
	return nil
}

// Lifecycle marks one instance generation going online (Up == true) or
// offline (Up == false) at time At.
type Lifecycle struct {
	Instance string
	Gen      int
	At       int64
	Up       bool
}

func (e Lifecycle) validate() error {
	if e.Instance == "" {
		return fmt.Errorf("lifecycle: instance id is empty")
	}
	if e.Gen < 0 {
		return fmt.Errorf("lifecycle: negative generation %d", e.Gen)
	}
	return nil
}

// ProblemKind classifies why a window cannot be fully verified.
type ProblemKind string

const (
	// ProblemGap: an online slice of an instance generation is not covered
	// by any observation.
	ProblemGap ProblemKind = "gap"
	// ProblemOverlap: two observations of the same generation overlap in
	// time, so the increments cannot be attributed unambiguously.
	ProblemOverlap ProblemKind = "overlap"
	// ProblemContradiction: an observation contradicts the known state,
	// e.g. busy time exceeds the interval, the interval crosses a window
	// boundary, or the instance generation was not online at that time.
	ProblemContradiction ProblemKind = "contradiction"
	// ProblemNoOnline: no instance generation is online in the window, so
	// the utilization denominator is zero.
	ProblemNoOnline ProblemKind = "no-online"
)

// Problem points at the concrete slice that cannot be verified.
type Problem struct {
	Kind     ProblemKind
	Instance string
	Gen      int
	Start    int64
	End      int64
	Detail   string
}

// WindowStatus is the verified pressure class of one window.
type WindowStatus string

const (
	StatusHigh         WindowStatus = "high"
	StatusNormal       WindowStatus = "normal"
	StatusLow          WindowStatus = "low"
	StatusUnverifiable WindowStatus = "unverifiable"
)

// Verdict is the controller's conclusion for one fixed-size window.
type Verdict struct {
	Window              int
	Start               int64
	End                 int64
	Status              WindowStatus
	Busy                int64
	Available           int64
	Requests            int64
	UtilizationPermille int
	Problems            []Problem
}

// Direction of a scaling action.
type Direction string

const (
	ScaleOut Direction = "scale_out"
	ScaleIn  Direction = "scale_in"
)

// Action is a published scaling decision. Cooldown starts only after the
// action is acknowledged via Acknowledge.
type Action struct {
	ID        int64
	Window    int
	Direction Direction
	IssuedAt  int64
	AckedAt   int64
}

// Acknowledged reports whether the receipt for this action has been seen.
func (a Action) Acknowledged() bool { return a.AckedAt > 0 }

// Decision is one element returned by Pump: a frozen window verdict plus
// the action published together with it, if any.
type Decision struct {
	Verdict Verdict
	Action  *Action
	Blocked bool // a scaling decision was withheld (pending action/cooldown)
	Flushed bool // a previously blocked action is emitted now
}

// Config holds the tunable thresholds. Watermarks are integer permilles.
type Config struct {
	WindowSize            int64
	HighWatermarkPermille int
	LowWatermarkPermille  int
	OutStreak             int
	InStreak              int
	Cooldown              int64
}

// DefaultConfig matches the task statement: two consecutive high windows
// trigger scale-out, three consecutive low windows trigger scale-in.
func DefaultConfig() Config {
	return Config{
		WindowSize:            10,
		HighWatermarkPermille: 800,
		LowWatermarkPermille:  300,
		OutStreak:             2,
		InStreak:              3,
		Cooldown:              50,
	}
}

func (c Config) validate() error {
	if c.WindowSize <= 0 {
		return fmt.Errorf("config: window size must be positive")
	}
	if c.HighWatermarkPermille <= 0 || c.HighWatermarkPermille > 1000 {
		return fmt.Errorf("config: high watermark must be in (0,1000] permille")
	}
	if c.LowWatermarkPermille < 0 || c.LowWatermarkPermille >= c.HighWatermarkPermille {
		return fmt.Errorf("config: low watermark must be below high watermark")
	}
	if c.OutStreak <= 0 || c.InStreak <= 0 {
		return fmt.Errorf("config: streak lengths must be positive")
	}
	if c.Cooldown < 0 {
		return fmt.Errorf("config: cooldown must not be negative")
	}
	return nil
}
