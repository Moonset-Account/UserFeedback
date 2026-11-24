package flowctl

import (
	"errors"
	"fmt"
)

// Bytes is an integer byte count.
type Bytes int64

// PortID identifies an ingress port.
type PortID int

// Priority is the frame priority / PFC class.
type Priority int

// Key identifies the flow that can receive a PFC pause or resume.
type Key struct {
	Port     PortID   `json:"port"`
	Priority Priority `json:"priority"`
}

func (k Key) String() string {
	return fmt.Sprintf("port=%d/priority=%d", k.Port, k.Priority)
}

// Frame is one indivisible ingress frame.
type Frame struct {
	Bytes    Bytes    `json:"bytes"`
	Port     PortID   `json:"port"`
	Priority Priority `json:"priority"`
}

func (f Frame) key() Key { return Key{Port: f.Port, Priority: f.Priority} }

func (f Frame) validate() error {
	if f.Bytes < 0 {
		return fmt.Errorf("frame bytes must be non-negative, got %d", f.Bytes)
	}
	if f.Port < 0 {
		return fmt.Errorf("port id must be non-negative, got %d", f.Port)
	}
	if f.Priority < 0 {
		return fmt.Errorf("priority must be non-negative, got %d", f.Priority)
	}
	return nil
}

// Departure attributes sent bytes to a buffered ingress/priority flow.
type Departure struct {
	Key   Key   `json:"key"`
	Bytes Bytes `json:"bytes"`
}

func (d Departure) validate() error {
	if d.Bytes < 0 {
		return fmt.Errorf("departure bytes must be non-negative, got %d", d.Bytes)
	}
	if d.Key.Port < 0 || d.Key.Priority < 0 {
		return errors.New("departure key contains a negative id")
	}
	return nil
}

// AdmissionPolicy selects whether reservations are only reported or also
// enforced as allocatable headroom.
type AdmissionPolicy int

const (
	// PhysicalOnly drops a frame only when the actual shared buffer cannot
	// hold the complete frame. This is the literal "really does not fit"
	// model. Reservations remain visible to control decisions.
	PhysicalOnly AdmissionPolicy = iota

	// ProtectReservations additionally refuses a frame whose unreserved
	// excess would consume headroom promised to another flow. Bytes covered
	// by the same flow's reservation write that reservation off.
	ProtectReservations
)

// DrainOrder is the deterministic allocation order for EgressAvailable.
type DrainOrder int

const (
	// DrainKeyAsc sorts by port and then priority.
	DrainKeyAsc DrainOrder = iota
	// DrainPriorityDesc sorts higher priority first, then port ascending.
	DrainPriorityDesc
)

// Config configures the shared-buffer flow controller.
type Config struct {
	// Capacity is the total shared buffer size in bytes.
	Capacity Bytes

	// PauseThreshold is the per-flow observed high watermark. A pause is
	// emitted when non-paused flow usage is >= this value.
	PauseThreshold Bytes

	// ResumeThreshold is the per-flow observed low watermark. A paused flow
	// can resume only when usage is <= this value.
	ResumeThreshold Bytes

	// InFlight is the default bound on bytes that can still arrive after a
	// pause is sent to an ingress.
	InFlight Bytes

	// InFlightByPort overrides InFlight for individual ingress ports.
	InFlightByPort map[PortID]Bytes

	// Admission defaults to PhysicalOnly.
	Admission AdmissionPolicy

	// DrainOrder defaults to DrainKeyAsc.
	DrainOrder DrainOrder
}

func (c Config) validate() error {
	if c.Capacity <= 0 {
		return fmt.Errorf("capacity must be positive, got %d", c.Capacity)
	}
	if c.PauseThreshold <= 0 {
		return fmt.Errorf("pause threshold must be positive, got %d", c.PauseThreshold)
	}
	if c.ResumeThreshold < 0 {
		return fmt.Errorf("resume threshold must be non-negative, got %d", c.ResumeThreshold)
	}
	if c.ResumeThreshold >= c.PauseThreshold {
		return fmt.Errorf("resume threshold %d must be below pause threshold %d", c.ResumeThreshold, c.PauseThreshold)
	}
	if c.PauseThreshold > c.Capacity {
		return fmt.Errorf("pause threshold %d must not exceed capacity %d", c.PauseThreshold, c.Capacity)
	}
	if c.InFlight < 0 {
		return fmt.Errorf("in-flight bytes must be non-negative, got %d", c.InFlight)
	}
	for port, n := range c.InFlightByPort {
		if port < 0 {
			return fmt.Errorf("in-flight map has negative port id %d", port)
		}
		if n < 0 {
			return fmt.Errorf("in-flight bytes for port %d must be non-negative", port)
		}
	}
	if c.Admission != PhysicalOnly && c.Admission != ProtectReservations {
		return fmt.Errorf("unknown admission policy %d", c.Admission)
	}
	if c.DrainOrder != DrainKeyAsc && c.DrainOrder != DrainPriorityDesc {
		return fmt.Errorf("unknown drain order %d", c.DrainOrder)
	}
	return nil
}

func (c Config) inFlight(port PortID) Bytes {
	if n, ok := c.InFlightByPort[port]; ok {
		return n
	}
	return c.InFlight
}

// DropReason explains why a complete frame was recorded as not admitted.
type DropReason string

const (
	// DropFrameTooLarge means the frame is larger than the whole buffer.
	DropFrameTooLarge DropReason = "frame_too_large"

	// DropBufferFull means current physical occupancy leaves insufficient room.
	DropBufferFull DropReason = "buffer_full"

	// DropReservedHeadroom means physical room exists, but accepting the frame
	// would consume headroom promised to another flow under ProtectReservations.
	DropReservedHeadroom DropReason = "reserved_headroom_exhausted"
)

// DroppedFrame is an immutable record of a frame that was not admitted.
type DroppedFrame struct {
	Frame                 Frame      `json:"frame"`
	Reason                DropReason `json:"reason"`
	OccupancyBefore       Bytes      `json:"occupancy_before"`
	ReservedBefore        Bytes      `json:"reserved_before"`
	SharedMarginBefore    Bytes      `json:"shared_margin_before"`
	AvailableToFlowBefore Bytes      `json:"available_to_flow_before"`
	Sequence              int64      `json:"sequence"`
}

// Directive is a pause or resume decision.
type Directive struct {
	Kind             string `json:"kind"` // "pause" or "resume"
	Key              Key    `json:"key"`
	InFlight         Bytes  `json:"in_flight"`
	Occupancy        Bytes  `json:"occupancy"`
	Reservation      Bytes  `json:"reservation"`
	UsedTotal        Bytes  `json:"used_total"`
	ReservedTotal    Bytes  `json:"reserved_total"`
	SharedMargin     Bytes  `json:"shared_margin"`
	UnbackedInFlight Bytes  `json:"unbacked_in_flight"`
	Sequence         int64  `json:"sequence"`
}

// FlowSnapshot is a point-in-time copy of one ingress/priority flow.
type FlowSnapshot struct {
	Key             Key   `json:"key"`
	Used            Bytes `json:"used"`
	Reservation     Bytes `json:"reservation"`
	Paused          bool  `json:"paused"`
	InFlight        Bytes `json:"in_flight"`
	PauseThreshold  Bytes `json:"pause_threshold"`
	ResumeThreshold Bytes `json:"resume_threshold"`
	ProjectedPeak   Bytes `json:"projected_peak"`
	AvailableToFlow Bytes `json:"available_to_flow"`
}

// Snapshot is a point-in-time copy of controller state.
type Snapshot struct {
	Capacity     Bytes          `json:"capacity"`
	Used         Bytes          `json:"used"`
	Reserved     Bytes          `json:"reserved"`
	SharedMargin Bytes          `json:"shared_margin"`
	PhysicalFree Bytes          `json:"physical_free"`
	Unbacked     Bytes          `json:"unbacked"`
	Flows        []FlowSnapshot `json:"flows"`
	DropCount    int            `json:"drop_count"`
	PauseCount   int            `json:"pause_count"`
	ResumeCount  int            `json:"resume_count"`
	EventCount   int            `json:"event_count"`
}

// EventKind identifies a chronological event.
type EventKind string

const (
	EventAccepted EventKind = "accepted"
	EventDropped  EventKind = "dropped"
	EventDeparted EventKind = "departed"
	EventPaused   EventKind = "paused"
	EventResumed  EventKind = "resumed"
)

// Event is a compact chronological state-transition record.
type Event struct {
	Kind       EventKind  `json:"kind"`
	Sequence   int64      `json:"sequence"`
	Round      int        `json:"round"`
	Key        Key        `json:"key"`
	Bytes      Bytes      `json:"bytes,omitempty"`
	WrittenOff Bytes      `json:"written_off,omitempty"`
	Reason     DropReason `json:"reason,omitempty"`
	Directive  *Directive `json:"directive,omitempty"`
}
