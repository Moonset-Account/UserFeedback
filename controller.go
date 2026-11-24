package flowctl

import (
	"fmt"
	"sort"
)

type flowState struct {
	used        Bytes
	reservation Bytes
	paused      bool
	pauseCount  int
	resumeCount int
}

// Controller is a deterministic shared-buffer PFC-style flow controller.
// It is not safe for concurrent use.
type Controller struct {
	cfg Config

	flows map[Key]*flowState
	used  Bytes

	// reserved is the sum of all flowState.reservation values.
	reserved Bytes

	seq         int64
	round       int
	pauseCount  int
	resumeCount int

	drops  []DroppedFrame
	events []Event
}

// New validates cfg and creates an empty controller.
func New(cfg Config) (*Controller, error) {
	if err := cfg.validate(); err != nil {
		return nil, err
	}
	if cfg.InFlightByPort != nil {
		copied := make(map[PortID]Bytes, len(cfg.InFlightByPort))
		for k, v := range cfg.InFlightByPort {
			copied[k] = v
		}
		cfg.InFlightByPort = copied
	}
	return &Controller{
		cfg:   cfg,
		flows: make(map[Key]*flowState),
	}, nil
}

// Config returns a copy of the controller configuration.
func (c *Controller) Config() Config {
	cfg := c.cfg
	if c.cfg.InFlightByPort != nil {
		cfg.InFlightByPort = make(map[PortID]Bytes, len(c.cfg.InFlightByPort))
		for k, v := range c.cfg.InFlightByPort {
			cfg.InFlightByPort[k] = v
		}
	}
	return cfg
}

func maxBytes(a, b Bytes) Bytes {
	if a > b {
		return a
	}
	return b
}

func minBytes(a, b Bytes) Bytes {
	if a < b {
		return a
	}
	return b
}

func (c *Controller) physicalFree() Bytes { return c.cfg.Capacity - c.used }

// SharedMargin is capacity minus observed bytes and all promised reservations.
// It may be negative when pause commitments oversubscribe the shared pool.
func (c *Controller) SharedMargin() Bytes {
	return c.cfg.Capacity - c.used - c.reserved
}

func (c *Controller) unbacked() Bytes {
	return maxBytes(0, c.used+c.reserved-c.cfg.Capacity)
}

func (c *Controller) state(key Key) *flowState {
	fs := c.flows[key]
	if fs == nil {
		fs = &flowState{}
		c.flows[key] = fs
	}
	return fs
}

func (c *Controller) existingState(key Key) *flowState {
	return c.flows[key]
}

func (c *Controller) sortedKeys() []Key {
	keys := make([]Key, 0, len(c.flows))
	for k := range c.flows {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		a, b := keys[i], keys[j]
		if c.cfg.DrainOrder == DrainPriorityDesc {
			if a.Priority != b.Priority {
				return a.Priority > b.Priority
			}
		} else if a.Port != b.Port {
			return a.Port < b.Port
		}
		if a.Priority != b.Priority {
			return a.Priority < b.Priority
		}
		return a.Port < b.Port
	})
	return keys
}

func (c *Controller) recordEvent(kind EventKind, round int, key Key, n, writtenOff Bytes, reason DropReason, d *Directive) Event {
	c.seq++
	ev := Event{
		Kind:       kind,
		Sequence:   c.seq,
		Round:      round,
		Key:        key,
		Bytes:      n,
		WrittenOff: writtenOff,
		Reason:     reason,
	}
	if d != nil {
		d.Sequence = c.seq
		dCopy := *d
		ev.Directive = &dCopy
	}
	c.events = append(c.events, ev)
	return ev
}

func (c *Controller) makeDirective(kind string, key Key, inFlight Bytes, fs *flowState) *Directive {
	d := &Directive{
		Kind:             kind,
		Key:              key,
		InFlight:         inFlight,
		UsedTotal:        c.used,
		ReservedTotal:    c.reserved,
		SharedMargin:     c.SharedMargin(),
		UnbackedInFlight: c.unbacked(),
	}
	if fs != nil {
		d.Occupancy = fs.used
		d.Reservation = fs.reservation
	}
	return d
}

// ArrivalResult records admission, write-off, and any immediate pause.
type ArrivalResult struct {
	Accepted   bool          `json:"accepted"`
	Frame      Frame         `json:"frame"`
	Used       Bytes         `json:"used"`
	WrittenOff Bytes         `json:"written_off"`
	Pause      *Directive    `json:"pause,omitempty"`
	Drop       *DroppedFrame `json:"drop,omitempty"`
}

// Arrive admits one complete frame if the configured policy can place it.
// An admitted frame increments observed occupancy and writes off up to the
// same flow's outstanding reservation. Pause evaluation is immediate, so the
// new reservation is visible to the next interleaved frame.
func (c *Controller) Arrive(frame Frame) (*ArrivalResult, error) {
	if err := frame.validate(); err != nil {
		return nil, err
	}
	key := frame.key()
	fsBefore := c.existingState(key)
	ownReservation := Bytes(0)
	if fsBefore != nil {
		ownReservation = fsBefore.reservation
	}

	occupancyBefore := c.used
	reservedBefore := c.reserved
	marginBefore := c.SharedMargin()
	availableToFlow := marginBefore + ownReservation

	drop := c.admissionDrop(frame, occupancyBefore, reservedBefore, marginBefore, availableToFlow)
	if drop != nil {
		ev := c.recordEvent(EventDropped, c.round, key, frame.Bytes, 0, drop.Reason, nil)
		drop.Sequence = ev.Sequence
		c.drops = append(c.drops, *drop)
		return &ArrivalResult{Accepted: false, Frame: frame, Used: c.used, Drop: drop}, nil
	}

	fs := c.state(key)
	fs.used += frame.Bytes
	c.used += frame.Bytes

	writtenOff := minBytes(frame.Bytes, fs.reservation)
	fs.reservation -= writtenOff
	c.reserved -= writtenOff

	c.recordEvent(EventAccepted, c.round, key, frame.Bytes, writtenOff, "", nil)

	res := &ArrivalResult{
		Accepted:   true,
		Frame:      frame,
		Used:       fs.used,
		WrittenOff: writtenOff,
	}
	if p := c.maybePause(key); p != nil {
		res.Pause = p
	}
	return res, nil
}

func (c *Controller) admissionDrop(frame Frame, usedBefore, reservedBefore, marginBefore, availableToFlow Bytes) *DroppedFrame {
	newUsed := usedBefore + frame.Bytes
	if newUsed > c.cfg.Capacity {
		reason := DropBufferFull
		if frame.Bytes > c.cfg.Capacity {
			reason = DropFrameTooLarge
		}
		return &DroppedFrame{
			Frame:                 frame,
			Reason:                reason,
			OccupancyBefore:       usedBefore,
			ReservedBefore:        reservedBefore,
			SharedMarginBefore:    marginBefore,
			AvailableToFlowBefore: availableToFlow,
		}
	}

	if c.cfg.Admission == ProtectReservations {
		own := Bytes(0)
		if fs := c.existingState(frame.key()); fs != nil {
			own = fs.reservation
		}
		unreservedNeed := maxBytes(0, frame.Bytes-own)
		if unreservedNeed > maxBytes(0, marginBefore) {
			return &DroppedFrame{
				Frame:                 frame,
				Reason:                DropReservedHeadroom,
				OccupancyBefore:       usedBefore,
				ReservedBefore:        reservedBefore,
				SharedMarginBefore:    marginBefore,
				AvailableToFlowBefore: marginBefore + own,
			}
		}
	}
	return nil
}

func (c *Controller) maybePause(key Key) *Directive {
	fs := c.existingState(key)
	if fs == nil || fs.paused || fs.used < c.cfg.PauseThreshold {
		return nil
	}

	inFlight := c.cfg.inFlight(key.Port)
	fs.paused = true
	fs.pauseCount++
	c.pauseCount++

	// A pause commits the full port-specific in-flight bound. A residual
	// reservation from an earlier cycle is preserved, so commitments are not
	// silently erased.
	fs.reservation += inFlight
	c.reserved += inFlight

	d := c.makeDirective("pause", key, inFlight, fs)
	c.recordEvent(EventPaused, c.round, key, inFlight, 0, "", d)
	// recordEvent copies the directive after assigning sequence; return the
	// event's copy so callers observe the same sequence number.
	last := c.events[len(c.events)-1]
	paused := *last.Directive
	return &paused
}

// ResumeEligibility reports the defensive low-water/shared-margin checks.
type ResumeEligibility struct {
	Eligible        bool   `json:"eligible"`
	Reason          string `json:"reason,omitempty"`
	Key             Key    `json:"key"`
	Paused          bool   `json:"paused"`
	Occupancy       Bytes  `json:"occupancy"`
	ResumeThreshold Bytes  `json:"resume_threshold"`
	InFlight        Bytes  `json:"in_flight"`
	SharedMargin    Bytes  `json:"shared_margin"`
}

// CheckResume evaluates whether key can receive a resume without changing state.
func (c *Controller) CheckResume(key Key) ResumeEligibility {
	fs := c.existingState(key)
	chk := ResumeEligibility{
		Key:             key,
		ResumeThreshold: c.cfg.ResumeThreshold,
		InFlight:        c.cfg.inFlight(key.Port),
		SharedMargin:    c.SharedMargin(),
	}
	if fs == nil {
		chk.Reason = "unknown flow"
		return chk
	}
	chk.Paused = fs.paused
	chk.Occupancy = fs.used
	if !fs.paused {
		chk.Reason = "flow is not paused"
		return chk
	}
	if fs.used > c.cfg.ResumeThreshold {
		chk.Reason = fmt.Sprintf("occupancy %d exceeds resume threshold %d", fs.used, c.cfg.ResumeThreshold)
		return chk
	}
	if chk.SharedMargin < chk.InFlight {
		chk.Reason = fmt.Sprintf("shared margin %d is below in-flight requirement %d", chk.SharedMargin, chk.InFlight)
		return chk
	}
	chk.Eligible = true
	return chk
}

// ResumeIfEligible emits and applies a resume exactly when both required
// conditions hold. It returns nil,false when the flow is not eligible.
func (c *Controller) ResumeIfEligible(key Key) (*Directive, bool) {
	if !c.CheckResume(key).Eligible {
		return nil, false
	}
	fs := c.existingState(key)
	inFlight := c.cfg.inFlight(key.Port)
	fs.paused = false
	fs.resumeCount++
	c.resumeCount++

	d := c.makeDirective("resume", key, inFlight, fs)
	c.recordEvent(EventResumed, c.round, key, inFlight, 0, "", d)
	last := c.events[len(c.events)-1]
	resumed := *last.Directive
	return &resumed, true
}

// ReconcileResumes checks every known flow in deterministic key order and
// resumes all currently eligible paused flows.
func (c *Controller) ReconcileResumes() []Directive {
	var out []Directive
	for _, key := range c.sortedKeys() {
		if d, ok := c.ResumeIfEligible(key); ok {
			out = append(out, *d)
		}
	}
	return out
}

// Depart removes sent bytes from a named flow. Reservations are not changed:
// per the model, reservations are written off by later arrivals only.
func (c *Controller) Depart(d Departure) (Bytes, error) {
	if err := d.validate(); err != nil {
		return 0, err
	}
	if d.Bytes < 0 {
		return 0, fmt.Errorf("departure bytes must be non-negative")
	}
	fs := c.existingState(d.Key)
	if fs == nil {
		if d.Bytes == 0 {
			return 0, nil
		}
		return 0, fmt.Errorf("departure %d bytes from unknown flow %s", d.Bytes, d.Key)
	}
	if d.Bytes > fs.used {
		return 0, fmt.Errorf("departure %d exceeds used bytes %d for %s", d.Bytes, fs.used, d.Key)
	}

	fs.used -= d.Bytes
	c.used -= d.Bytes
	c.recordEvent(EventDeparted, c.round, d.Key, d.Bytes, 0, "", nil)
	return fs.used, nil
}

// DrainResult contains deterministic egress allocation and resulting resumes.
type DrainResult struct {
	Requested  Bytes       `json:"requested"`
	Sent       Bytes       `json:"sent"`
	Departures []Departure `json:"departures"`
	Resumes    []Directive `json:"resumes"`
}

// Drain removes up to maxBytes using Config.DrainOrder, then reconciles resumes.
func (c *Controller) Drain(maxBytes Bytes) (*DrainResult, error) {
	if maxBytes < 0 {
		return nil, fmt.Errorf("drain budget must be non-negative, got %d", maxBytes)
	}
	res := &DrainResult{Requested: maxBytes}
	remaining := maxBytes
	for _, key := range c.sortedKeys() {
		if remaining == 0 {
			break
		}
		fs := c.existingState(key)
		if fs == nil || fs.used == 0 {
			continue
		}
		n := minBytes(remaining, fs.used)
		fs.used -= n
		c.used -= n
		remaining -= n
		res.Sent += n
		d := Departure{Key: key, Bytes: n}
		res.Departures = append(res.Departures, d)
		c.recordEvent(EventDeparted, c.round, key, n, 0, "", nil)
	}
	res.Resumes = c.ReconcileResumes()
	return res, nil
}

// Flow returns a snapshot of one flow.
func (c *Controller) Flow(key Key) (FlowSnapshot, bool) {
	fs := c.existingState(key)
	if fs == nil {
		return FlowSnapshot{}, false
	}
	return c.flowSnapshot(key, fs), true
}

func (c *Controller) flowSnapshot(key Key, fs *flowState) FlowSnapshot {
	margin := c.SharedMargin()
	return FlowSnapshot{
		Key:             key,
		Used:            fs.used,
		Reservation:     fs.reservation,
		Paused:          fs.paused,
		InFlight:        c.cfg.inFlight(key.Port),
		PauseThreshold:  c.cfg.PauseThreshold,
		ResumeThreshold: c.cfg.ResumeThreshold,
		ProjectedPeak:   fs.used + fs.reservation,
		AvailableToFlow: margin + fs.reservation,
	}
}

// Snapshot returns sorted flow state and global accounting.
func (c *Controller) Snapshot() Snapshot {
	keys := c.sortedKeys()
	flows := make([]FlowSnapshot, 0, len(keys))
	for _, k := range keys {
		flows = append(flows, c.flowSnapshot(k, c.flows[k]))
	}
	return Snapshot{
		Capacity:     c.cfg.Capacity,
		Used:         c.used,
		Reserved:     c.reserved,
		SharedMargin: c.SharedMargin(),
		PhysicalFree: c.physicalFree(),
		Unbacked:     c.unbacked(),
		Flows:        flows,
		DropCount:    len(c.drops),
		PauseCount:   c.pauseCount,
		ResumeCount:  c.resumeCount,
		EventCount:   len(c.events),
	}
}

// Drops returns a copy of all dropped-frame records.
func (c *Controller) Drops() []DroppedFrame {
	return append([]DroppedFrame(nil), c.drops...)
}

// Events returns a copy of the chronological event history.
func (c *Controller) Events() []Event {
	out := make([]Event, len(c.events))
	for i, ev := range c.events {
		out[i] = ev
		if ev.Directive != nil {
			d := *ev.Directive
			out[i].Directive = &d
		}
	}
	return out
}

// Pauses returns all pause directives in chronological order.
func (c *Controller) Pauses() []Directive {
	var out []Directive
	for _, ev := range c.events {
		if ev.Kind == EventPaused && ev.Directive != nil {
			out = append(out, *ev.Directive)
		}
	}
	return out
}

// Resumes returns all resume directives in chronological order.
func (c *Controller) Resumes() []Directive {
	var out []Directive
	for _, ev := range c.events {
		if ev.Kind == EventResumed && ev.Directive != nil {
			out = append(out, *ev.Directive)
		}
	}
	return out
}

// CheckInvariants validates derived sums and non-negative physical occupancy.
// Tests and synthetic simulations can call it after every scripted operation.
func (c *Controller) CheckInvariants() error {
	var used, reserved Bytes
	for k, fs := range c.flows {
		if fs.used < 0 || fs.reservation < 0 {
			return fmt.Errorf("negative accounting for %s", k)
		}
		used += fs.used
		reserved += fs.reservation
	}
	if used != c.used {
		return fmt.Errorf("used mismatch: global %d sum %d", c.used, used)
	}
	if reserved != c.reserved {
		return fmt.Errorf("reserved mismatch: global %d sum %d", c.reserved, reserved)
	}
	if c.used < 0 || c.used > c.cfg.Capacity {
		return fmt.Errorf("used %d outside [0,%d]", c.used, c.cfg.Capacity)
	}
	if c.reserved < 0 {
		return fmt.Errorf("reserved %d is negative", c.reserved)
	}
	return nil
}
