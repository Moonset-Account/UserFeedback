// Package probe implements an event-driven probe state machine for a single
// container slot managed by a node agent.
//
// The controller owns a monotonically increasing instance generation. A new
// instance starts in the startup phase and may only have startup probes
// dispatched. Once a startup probe succeeds, the instance enters the running
// phase and readiness and liveness probes become available.
//
// Probe dispatches are numbered from 1 per probe family and per instance.
// Results may be delivered out of order; a family's results take effect in
// dispatch order, buffering any result that arrives before an earlier one.
//
// External events (traffic allowed/blocked and instance restart) are returned
// from Deliver as one atomic batch. If a batch contains a restart, readiness
// transitions in that batch are suppressed: only the restart and, when the
// instance currently had traffic, the necessary traffic removal are published.
package probe

import "sync"

// ProbeType identifies one of the three probe families.
type ProbeType uint8

const (
	// Startup probes gate readiness and liveness probing. They are only
	// dispatched while the instance has not yet completed startup probing.
	Startup ProbeType = iota
	// Readiness probes control whether the instance may receive traffic.
	Readiness
	// Liveness probes detect an instance that must be restarted.
	Liveness
)

// String returns a human-readable probe family name.
func (t ProbeType) String() string {
	switch t {
	case Startup:
		return "startup"
	case Readiness:
		return "readiness"
	case Liveness:
		return "liveness"
	default:
		return "unknown"
	}
}

const (
	// ReadinessSuccessesToServe is the number of consecutive readiness
	// successes required before traffic is allowed.
	ReadinessSuccessesToServe = 2
	// FailureRestartThreshold is the number of consecutive startup or
	// liveness failures that triggers an instance restart.
	FailureRestartThreshold = 3
)

// Probe identifies one probe dispatch. Seq starts at 1 for each probe family
// within an instance generation and increases in dispatch order.
type Probe struct {
	Instance uint64
	Type     ProbeType
	Seq      uint64
}

// Receipt is the result of a dispatched probe. Instance must match the
// instance generation the probe was dispatched for; receipts from an older
// instance are ignored.
type Receipt struct {
	Instance uint64
	Type     ProbeType
	Seq      uint64
	Success  bool
}

// EventKind identifies an externally visible state machine output.
type EventKind uint8

const (
	// TrafficDisabled removes the instance from traffic.
	TrafficDisabled EventKind = iota
	// TrafficEnabled puts the instance into traffic.
	TrafficEnabled
	// Restart announces that a new instance generation has been created.
	Restart
)

// String returns a human-readable event name.
func (k EventKind) String() string {
	switch k {
	case TrafficDisabled:
		return "traffic-disabled"
	case TrafficEnabled:
		return "traffic-enabled"
	case Restart:
		return "restart"
	default:
		return "unknown"
	}
}

// Event is an externally visible state machine output.
//
// For TrafficEnabled and TrafficDisabled, Instance is the instance whose
// traffic state changed. For Restart, Instance is the newly created instance
// generation.
type Event struct {
	Kind     EventKind
	Instance uint64
}

// Snapshot is a read-only view of the controller state.
type Snapshot struct {
	Instance         uint64
	StartupDone      bool
	Ready            bool
	StartupFailures  int
	LivenessFailures int
	ReadinessStreak  int
	NextStartupSeq   uint64
	NextReadinessSeq uint64
	NextLivenessSeq  uint64
}

// Controller is the probe state machine. It is safe for concurrent use.
type Controller struct {
	mu sync.Mutex

	instance    uint64
	startupDone bool
	ready       bool

	// nextSeq is the sequence number the next Dispatch call will assign.
	nextSeq [3]uint64
	// nextApply is the next sequence number whose result may be applied.
	nextApply [3]uint64

	// pending buffers results that arrived before an earlier result in the
	// same family.
	pending [3]map[uint64]bool

	startupFailures  int
	livenessFailures int
	readinessStreak  int
}

// NewController creates a controller for a fresh instance generation.
// Instance numbering starts at 1.
func NewController() *Controller {
	c := &Controller{instance: 1}
	c.resetProbeFamilies()
	return c
}

// Instance returns the current instance generation.
func (c *Controller) Instance() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.instance
}

// Ready reports whether traffic is currently allowed.
func (c *Controller) Ready() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.ready
}

// StartupDone reports whether startup probing has completed for the current
// instance.
func (c *Controller) StartupDone() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.startupDone
}

// Snapshot returns the current controller state.
func (c *Controller) Snapshot() Snapshot {
	c.mu.Lock()
	defer c.mu.Unlock()
	return Snapshot{
		Instance:         c.instance,
		StartupDone:      c.startupDone,
		Ready:            c.ready,
		StartupFailures:  c.startupFailures,
		LivenessFailures: c.livenessFailures,
		ReadinessStreak:  c.readinessStreak,
		NextStartupSeq:   c.nextSeq[Startup],
		NextReadinessSeq: c.nextSeq[Readiness],
		NextLivenessSeq:  c.nextSeq[Liveness],
	}
}

// Dispatch allocates the next probe in family t for the current instance.
//
// Startup probes are only available before startup has succeeded. Readiness
// and liveness probes are only available after startup has succeeded. When a
// family is not currently dispatchable, ok is false.
func (c *Controller) Dispatch(t ProbeType) (p Probe, ok bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if t > Liveness {
		return Probe{}, false
	}
	if t == Startup {
		if c.startupDone {
			return Probe{}, false
		}
	} else if !c.startupDone {
		return Probe{}, false
	}

	seq := c.nextSeq[t]
	c.nextSeq[t]++
	return Probe{Instance: c.instance, Type: t, Seq: seq}, true
}

// Deliver applies a batch of probe results atomically and returns the
// externally visible events produced by the batch.
//
// Results for another instance generation, results that were never dispatched,
// and duplicate results are ignored. Within a probe family, buffered and newly
// delivered results are applied in dispatch order.
//
// If the batch triggers a restart, all readiness transitions in the batch are
// suppressed and the returned events are, in order: TrafficDisabled (only if
// the instance was serving traffic when the batch began) followed by Restart.
// Otherwise at most one traffic event is returned, representing the net
// readiness change of the batch.
func (c *Controller) Deliver(receipts ...Receipt) []Event {
	c.mu.Lock()
	defer c.mu.Unlock()

	startReady := c.ready
	startInstance := c.instance
	restarted := false

	for _, r := range receipts {
		if r.Instance != c.instance {
			continue
		}
		if r.Type > Liveness {
			continue
		}
		// Reject results that were never dispatched and duplicates of results
		// that have already been applied.
		if r.Seq == 0 || r.Seq >= c.nextSeq[r.Type] || r.Seq < c.nextApply[r.Type] {
			continue
		}

		c.pending[r.Type][r.Seq] = r.Success
		if c.drain(r.Type) {
			restarted = true
			break
		}
	}

	if restarted {
		events := make([]Event, 0, 2)
		if startReady {
			events = append(events, Event{Kind: TrafficDisabled, Instance: startInstance})
		}
		events = append(events, Event{Kind: Restart, Instance: c.instance})
		return events
	}

	if c.ready == startReady {
		return nil
	}
	if c.ready {
		return []Event{{Kind: TrafficEnabled, Instance: c.instance}}
	}
	return []Event{{Kind: TrafficDisabled, Instance: c.instance}}
}

// drain applies all contiguous buffered results for family t in dispatch
// order. It reports whether applying a result restarted the instance.
func (c *Controller) drain(t ProbeType) bool {
	for {
		success, ok := c.pending[t][c.nextApply[t]]
		if !ok {
			return false
		}
		delete(c.pending[t], c.nextApply[t])
		c.nextApply[t]++
		if c.apply(t, success) {
			return true
		}
	}
}

// apply updates the state machine with one result. The result is guaranteed to
// be the next one in dispatch order for its family. It reports whether the
// result restarted the instance.
func (c *Controller) apply(t ProbeType, success bool) bool {
	switch t {
	case Startup:
		// Startup probing ends at the first success. In-flight startup results
		// that complete afterwards are stale and must not count toward a
		// restart.
		if c.startupDone {
			return false
		}
		if success {
			c.startupFailures = 0
			c.startupDone = true
			return false
		}
		c.startupFailures++
		if c.startupFailures >= FailureRestartThreshold {
			c.restart()
			return true
		}

	case Readiness:
		if success {
			c.readinessStreak++
			if c.readinessStreak >= ReadinessSuccessesToServe {
				c.ready = true
			}
		} else {
			c.readinessStreak = 0
			c.ready = false
		}

	case Liveness:
		if success {
			c.livenessFailures = 0
			return false
		}
		c.livenessFailures++
		if c.livenessFailures >= FailureRestartThreshold {
			c.restart()
			return true
		}
	}
	return false
}

// restart creates a new instance generation and resets all probing state. The
// new instance must start with startup probing again.
func (c *Controller) restart() {
	c.instance++
	c.startupDone = false
	c.ready = false
	c.startupFailures = 0
	c.livenessFailures = 0
	c.readinessStreak = 0
	c.resetProbeFamilies()
}

func (c *Controller) resetProbeFamilies() {
	for i := range c.nextSeq {
		c.nextSeq[i] = 1
		c.nextApply[i] = 1
		c.pending[i] = make(map[uint64]bool)
	}
}
