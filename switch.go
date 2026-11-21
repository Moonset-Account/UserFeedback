// Package l2switch implements a small layer-2 learning-switch model with
// per-port ingress/egress label mappings, per-(port, bridge-domain) states
// and MAC learning scoped to a bridge domain.
//
// A frame arrives on a physical port carrying either an explicit line-side
// label (for example VLAN 10) or no label. The ingress mapping of that port
// determines which bridge domain the frame belongs to. Forwarding decisions
// are made inside the bridge domain. When a frame leaves a port, the
// port-specific egress mapping of that bridge domain determines the
// line-side label of the emitted frame, which may also be "no label".
package l2switch

import (
	"errors"
	"fmt"
	"sort"
	"sync"
)

// Label is a line-side frame label. The zero value means "no label", which
// is a valid mapping value both for ingress and for egress.
type Label int

// PortID identifies a physical switch port.
type PortID string

// BridgeDomain identifies a broadcast/learning domain inside the switch.
type BridgeDomain string

// PortState is the state of one port inside one bridge domain.
type PortState int

const (
	// StateBlocking drops every frame that enters this (port, bridge
	// domain). The port never sends frames of that bridge domain either.
	StateBlocking PortState = iota

	// StateLearnOnly accepts a frame only to refresh the source-MAC
	// learning entry of this bridge domain; the frame is never forwarded.
	StateLearnOnly

	// StateForwarding learns the source MAC and then performs normal
	// destination lookup / flooding inside the bridge domain.
	StateForwarding
)

// String renders a PortState for diagnostics.
func (s PortState) String() string {
	switch s {
	case StateBlocking:
		return "blocking"
	case StateLearnOnly:
		return "learn-only"
	case StateForwarding:
		return "forwarding"
	default:
		return fmt.Sprintf("unknown-state(%d)", int(s))
	}
}

// MAC is a 6-byte Ethernet MAC address.
type MAC [6]byte

// ParseMAC parses the canonical "xx:xx:xx:xx:xx:xx" form. An empty string is
// not a valid MAC; the all-zero MAC must be written explicitly.
func ParseMAC(s string) (MAC, error) {
	var m MAC
	if len(s) != 17 {
		return m, fmt.Errorf("l2switch: malformed MAC %q: expected 17 characters", s)
	}
	for i := 0; i < 6; i++ {
		if i > 0 {
			if s[i*3-1] != ':' {
				return m, fmt.Errorf("l2switch: malformed MAC %q: expected ':' at position %d", s, i*3-1)
			}
		}
		hi, ok1 := hexVal(s[i*3])
		lo, ok2 := hexVal(s[i*3+1])
		if !ok1 || !ok2 {
			return m, fmt.Errorf("l2switch: malformed MAC %q: non-hex byte %d", s, i)
		}
		m[i] = hi<<4 | lo
	}
	return m, nil
}

// MustParseMAC parses s or panics. It is intended for test fixtures and
// literals.
func MustParseMAC(s string) MAC {
	m, err := ParseMAC(s)
	if err != nil {
		panic(err)
	}
	return m
}

func hexVal(b byte) (byte, bool) {
	switch {
	case b >= '0' && b <= '9':
		return b - '0', true
	case b >= 'a' && b <= 'f':
		return b - 'a' + 10, true
	case b >= 'A' && b <= 'F':
		return b - 'A' + 10, true
	default:
		return 0, false
	}
}

// String renders the canonical colon-separated form.
func (m MAC) String() string {
	return fmt.Sprintf("%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5])
}

// BroadcastMAC is the Ethernet broadcast destination address.
var BroadcastMAC = MAC{0xff, 0xff, 0xff, 0xff, 0xff, 0xff}

// Frame is a frame presented to a port together with its line-side label.
// HasLabel is distinguished explicitly so that "no label" (HasLabel ==
// false) is a real input value rather than label 0.
type Frame struct {
	Ingress  PortID
	InLabel  Label
	HasLabel bool
	SrcMAC   MAC
	DstMAC   MAC
	Payload  string
}

// EgressFrame is a frame emitted from a port, carrying the egress label
// produced by that port's bridge-domain egress mapping.
type EgressFrame struct {
	Egress   PortID
	OutLabel Label
	HasLabel bool
	SrcMAC   MAC
	DstMAC   MAC
	Payload  string
}

// Switch is the learning switch. All operations are safe for concurrent use.
type Switch struct {
	mu sync.RWMutex

	name string

	// ports records which physical ports exist.
	ports map[PortID]bool

	// ingress maps (port, line-side input) to a bridge domain.
	ingress map[ingressKey]BridgeDomain

	// egress maps (port, bridge domain) to a line-side output label.
	egress map[egressKey]labelValue

	// states holds the per-(port, bridge-domain) state.
	states map[memberKey]PortState

	// macTable maps (bridge domain, source MAC) to the port that learned it.
	macTable map[learnKey]PortID
}

type ingressKey struct {
	port     PortID
	label    Label
	hasLabel bool
}

type egressKey struct {
	port PortID
	bd   BridgeDomain
}

type labelValue struct {
	label    Label
	hasLabel bool
}

type memberKey struct {
	port PortID
	bd   BridgeDomain
}

type learnKey struct {
	bd  BridgeDomain
	mac MAC
}

// NewSwitch creates an empty switch with the given diagnostic name.
func NewSwitch(name string) *Switch {
	return &Switch{
		name:     name,
		ports:    make(map[PortID]bool),
		ingress:  make(map[ingressKey]BridgeDomain),
		egress:   make(map[egressKey]labelValue),
		states:   make(map[memberKey]PortState),
		macTable: make(map[learnKey]PortID),
	}
}

// Name returns the diagnostic name.
func (s *Switch) Name() string {
	return s.name
}

// Common errors returned by configuration and forwarding operations.
var (
	ErrUnknownPort      = errors.New("l2switch: unknown port")
	ErrNoIngressMapping = errors.New("l2switch: no ingress mapping for port/label")
	ErrNoEgressMapping  = errors.New("l2switch: no egress mapping for port/bridge-domain")
	ErrNoPortState      = errors.New("l2switch: port is not a member of the bridge domain")
	ErrInvalidPortState = errors.New("l2switch: invalid port state")
)

// AddPort registers a physical port. Adding an existing port is a no-op.
func (s *Switch) AddPort(p PortID) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ports[p] = true
}

func (s *Switch) portKnownLocked(p PortID) bool {
	return s.ports[p]
}

// SetIngressMapping maps a labeled or unlabeled line-side input of a port
// into a bridge domain, and joins the port to that bridge domain.
//
// The port initially joins in StateBlocking unless a state was already
// configured. A convenient side effect installs a default egress mapping for
// the same (port, bridge domain) using the same line-side label, so the
// common symmetric case needs only one call; SetEgressMapping can override
// it afterwards (in particular to emit frames without a label).
func (s *Switch) SetIngressMapping(p PortID, in Label, hasLabel bool, bd BridgeDomain) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.portKnownLocked(p) {
		return fmt.Errorf("%w: %q", ErrUnknownPort, p)
	}
	s.ingress[ingressKey{port: p, label: in, hasLabel: hasLabel}] = bd
	s.joinLocked(p, bd)
	ek := egressKey{port: p, bd: bd}
	if _, exists := s.egress[ek]; !exists {
		s.egress[ek] = labelValue{label: in, hasLabel: hasLabel}
	}
	return nil
}

// SetEgressMapping configures the line-side label attached to frames of bd
// when they leave p. hasLabel == false means the frame is sent untagged.
func (s *Switch) SetEgressMapping(p PortID, bd BridgeDomain, out Label, hasLabel bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.portKnownLocked(p) {
		return fmt.Errorf("%w: %q", ErrUnknownPort, p)
	}
	s.egress[egressKey{port: p, bd: bd}] = labelValue{label: out, hasLabel: hasLabel}
	return nil
}

// joinLocked records membership of p in bd without changing an existing
// state or learned entries.
func (s *Switch) joinLocked(p PortID, bd BridgeDomain) {
	k := memberKey{port: p, bd: bd}
	if _, exists := s.states[k]; !exists {
		s.states[k] = StateBlocking
	}
}

// SetState changes the state of port p inside bridge domain bd.
//
// Transitioning to StateBlocking removes every source-MAC learning entry of
// bd that currently points at p, matching the requirement that a blocked
// port forgets what it learned. States of other bridge domains on the same
// port, and learning entries of other bridge domains, are untouched.
func (s *Switch) SetState(p PortID, bd BridgeDomain, state PortState) error {
	if state != StateBlocking && state != StateLearnOnly && state != StateForwarding {
		return fmt.Errorf("%w: %d", ErrInvalidPortState, int(state))
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.portKnownLocked(p) {
		return fmt.Errorf("%w: %q", ErrUnknownPort, p)
	}
	k := memberKey{port: p, bd: bd}
	if _, ok := s.states[k]; !ok {
		return fmt.Errorf("%w: port %q, bridge-domain %q", ErrNoPortState, p, bd)
	}
	s.states[k] = state
	if state == StateBlocking {
		for lk, learned := range s.macTable {
			if lk.bd == bd && learned == p {
				delete(s.macTable, lk)
			}
		}
	}
	return nil
}

// State returns the state of p inside bd.
func (s *Switch) State(p PortID, bd BridgeDomain) (PortState, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if !s.portKnownLocked(p) {
		return StateBlocking, fmt.Errorf("%w: %q", ErrUnknownPort, p)
	}
	st, ok := s.states[memberKey{port: p, bd: bd}]
	if !ok {
		return StateBlocking, fmt.Errorf("%w: port %q, bridge-domain %q", ErrNoPortState, p, bd)
	}
	return st, nil
}

// Receive processes one frame and returns the frames emitted from other
// ports. A nil/empty slice means the frame was dropped or consumed without
// being forwarded.
//
// Processing order inside a bridge domain:
//  1. resolve the ingress mapping; an unknown input drops the frame;
//  2. a blocking ingress port drops the frame;
//  3. learn-only and forwarding ingress ports learn the source MAC;
//  4. only a forwarding ingress port looks up the destination MAC:
//     a hit tries the single learned egress port, an unknown destination
//     (including broadcast) floods to every other forwarding port.
func (s *Switch) Receive(f Frame) ([]EgressFrame, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if !s.portKnownLocked(f.Ingress) {
		return nil, fmt.Errorf("%w: %q", ErrUnknownPort, f.Ingress)
	}

	bd, ok := s.ingress[ingressKey{port: f.Ingress, label: f.InLabel, hasLabel: f.HasLabel}]
	if !ok {
		return nil, fmt.Errorf("%w: port %q, label present=%v value=%d",
			ErrNoIngressMapping, f.Ingress, f.HasLabel, f.InLabel)
	}

	inState, ok := s.states[memberKey{port: f.Ingress, bd: bd}]
	if !ok {
		// Defensive: SetIngressMapping always joins the port, so this means
		// configuration was never installed.
		return nil, fmt.Errorf("%w: port %q, bridge-domain %q", ErrNoPortState, f.Ingress, bd)
	}

	// Blocking ingress: drop before any learning.
	if inState == StateBlocking {
		return nil, nil
	}

	// Learn-only and forwarding both learn the source MAC. The all-zero
	// source is not a learnable address.
	if f.SrcMAC != (MAC{}) {
		s.macTable[learnKey{bd: bd, mac: f.SrcMAC}] = f.Ingress
	}

	// Learn-only: update this domain's source MAC and stop.
	if inState == StateLearnOnly {
		return nil, nil
	}

	// Broadcast is always flooded; unicast uses the learned entry if any.
	if f.DstMAC == BroadcastMAC {
		return s.floodLocked(bd, f), nil
	}
	if dst, hit := s.macTable[learnKey{bd: bd, mac: f.DstMAC}]; hit {
		// The egress must be forwarding and must differ from the ingress.
		if dst == f.Ingress {
			return nil, nil
		}
		if s.states[memberKey{port: dst, bd: bd}] != StateForwarding {
			return nil, nil
		}
		return s.emitLocked(dst, bd, f)
	}
	return s.floodLocked(bd, f), nil
}

// floodLocked sends f to every port of bd other than the ingress that is in
// StateForwarding. Ports are visited in sorted PortID order so output order
// is deterministic for tests and operators.
func (s *Switch) floodLocked(bd BridgeDomain, f Frame) []EgressFrame {
	var members []PortID
	for k, st := range s.states {
		if k.bd == bd && st == StateForwarding && k.port != f.Ingress {
			members = append(members, k.port)
		}
	}
	sort.Slice(members, func(i, j int) bool { return members[i] < members[j] })

	var out []EgressFrame
	for _, p := range members {
		emitted, err := s.emitLocked(p, bd, f)
		if err != nil {
			// A forwarding member always has an egress mapping in valid
			// configurations; skip rather than panic if one is missing.
			continue
		}
		out = append(out, emitted...)
	}
	return out
}

// emitLocked builds one frame for egress port p using its egress label.
func (s *Switch) emitLocked(p PortID, bd BridgeDomain, f Frame) ([]EgressFrame, error) {
	lv, ok := s.egress[egressKey{port: p, bd: bd}]
	if !ok {
		return nil, fmt.Errorf("%w: port %q, bridge-domain %q", ErrNoEgressMapping, p, bd)
	}
	return []EgressFrame{{
		Egress:   p,
		OutLabel: lv.label,
		HasLabel: lv.hasLabel,
		SrcMAC:   f.SrcMAC,
		DstMAC:   f.DstMAC,
		Payload:  f.Payload,
	}}, nil
}

// LookupLearned returns the port that learned mac inside bd. The boolean is
// false when no entry exists. It is intended for verification/diagnostics.
func (s *Switch) LookupLearned(bd BridgeDomain, mac MAC) (PortID, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	p, ok := s.macTable[learnKey{bd: bd, mac: mac}]
	return p, ok
}

// LearnedCount returns the number of MAC entries in a bridge domain.
func (s *Switch) LearnedCount(bd BridgeDomain) int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	n := 0
	for k := range s.macTable {
		if k.bd == bd {
			n++
		}
	}
	return n
}
