// Package officeaccess implements the trusted access binding decision engine.
//
// The engine consumes three ordered event streams:
//
//   - switch port attachment events (PortEvent),
//   - trusted allocation acknowledgements (Allocation),
//   - device self-reported address claims (Claim).
//
// A device claim enters the binding table only when its (session, segment,
// address) triple matches a currently confirmed allocation and the session is
// still the trusted attachment of the claim's port. When two confirmed
// allocations overlap on the same segment/address, the affected address is
// isolated while every unaffected address keeps working. A late release that
// belongs to an attachment displaced by a port change is ignored and can never
// remove the new attachment's binding.
package officeaccess

import (
	"errors"
	"fmt"
	"net/netip"
	"sort"
)

// Input validation errors. They describe malformed events rather than policy
// decisions; a well-formed but inadmissible claim is returned as a Decision.
var (
	ErrInvalidPortEvent = errors.New("officeaccess: invalid port event")
	ErrInvalidReceipt   = errors.New("officeaccess: invalid allocation receipt")
	ErrInvalidClaim     = errors.New("officeaccess: invalid address claim")
)

// PortEventKind identifies a switch port attachment event.
type PortEventKind string

const (
	// PortUp means a trusted access session became attached to a port.
	PortUp PortEventKind = "port_up"
	// PortDown means the current attachment left the port.
	PortDown PortEventKind = "port_down"
)

// PortEvent is a switch port attachment event.
type PortEvent struct {
	Sequence int64
	PortID   string
	Session  string
	Kind     PortEventKind
}

// Allocation is a trusted allocator acknowledgement. Segment is a CIDR prefix
// (for example "10.0.10.0/24") and Address is the single address handed to the
// session inside that segment. ReceiptID uniquely identifies the allocation
// instance: a release only takes effect when it references the receipt that is
// currently confirmed, so a late release belonging to an attachment displaced
// by a port change can never remove a new binding.
type Allocation struct {
	Sequence  int64
	ReceiptID string
	Session   string
	Segment   string
	Address   string
	Released  bool
}

// Claim is a device self-reported address declaration on a specific port.
type Claim struct {
	Sequence int64
	PortID   string
	Session  string
	Segment  string
	Address  string
}

// Binding is an effective binding returned for a port.
type Binding struct {
	PortID   string
	Session  string
	Segment  string
	Address  string
	Sequence int64 // sequence of the claim that established the binding
}

// IsolationRecord documents one triggered address isolation.
type IsolationRecord struct {
	Segment  string
	Address  string
	Sessions []string // confirmed sessions holding the overlapping address, sorted
	Sequence int64    // sequence of the acknowledgement that triggered isolation
}

// RejectedClaim records a well-formed claim that policy refused to bind.
type RejectedClaim struct {
	Claim  Claim
	Reason string
}

// Decision is the result of evaluating a device claim.
type Decision struct {
	Accepted bool
	Reason   string
	Binding  *Binding
}

// Decision reason codes.
const (
	ReasonAccepted              = "accepted"
	ReasonAlreadyBound          = "already_bound"
	ReasonSessionNotOnPort      = "session_not_current_on_port"
	ReasonNoConfirmedAllocation = "no_matching_confirmed_allocation"
	ReasonAddressIsolated       = "address_isolated"
	ReasonAddressBoundByOther   = "address_bound_by_other_session"
)

// Snapshot is the engine output: effective bindings per port plus every
// isolation that was triggered and every rejected claim.
type Snapshot struct {
	PortBindings   map[string][]Binding
	Isolations     []IsolationRecord
	RejectedClaims []RejectedClaim
}

type addrKey struct {
	segment string
	address string
}

// Engine is a single-threaded deterministic decision engine. Its methods are
// meant to be invoked in event order; it is not safe for concurrent use.
type Engine struct {
	// sessionPort maps a live session to the port it currently attaches to.
	sessionPort map[string]string
	// portSession maps a port to its current trusted session.
	portSession map[string]string
	// allocations holds confirmed (session, segment, address) tuples and the
	// receipt ID of the allocation instance currently confirmed for each one.
	allocations map[string]map[addrKey]string
	// bindings is keyed by port, then segment/address.
	bindings map[string]map[addrKey]Binding
	// isolated lists all sessions currently confirmed for an isolated address.
	isolated map[addrKey][]string
	// isolatedNow tracks whether an overlap is currently active per address, so
	// each distinct overlap epoch emits exactly one IsolationRecord even when
	// receipts are reconfirmed repeatedly.
	isolatedNow    map[addrKey]bool
	isolations     []IsolationRecord
	rejectedClaims []RejectedClaim
}

// NewEngine creates an empty decision engine.
func NewEngine() *Engine {
	return &Engine{
		sessionPort: map[string]string{},
		portSession: map[string]string{},
		allocations: map[string]map[addrKey]string{},
		bindings:    map[string]map[addrKey]Binding{},
		isolated:    map[addrKey][]string{},
		isolatedNow: map[addrKey]bool{},
	}
}

func key(segment, address string) addrKey {
	return addrKey{segment: segment, address: address}
}

// recomputeIsolation recalculates whether a segment/address is currently
// isolated from the confirmed allocation table. The address stays isolated
// only while at least two live sessions confirm it; when the overlap resolves
// (down to one or zero holders) the active isolation is removed. Historical
// IsolationRecord entries are never rewritten.
func (e *Engine) recomputeIsolation(k addrKey) {
	var holders []string
	for session, owned := range e.allocations {
		if _, holds := owned[k]; holds {
			holders = append(holders, session)
		}
	}
	if len(holders) < 2 {
		delete(e.isolated, k)
		delete(e.isolatedNow, k)
		return
	}
	sort.Strings(holders)
	e.isolated[k] = holders
	e.isolatedNow[k] = true

	// An isolated address can never remain effective: evict any existing
	// binding for it on every port. The address becomes bindable again only
	// after the overlap resolves and a fresh claim succeeds.
	for port, bs := range e.bindings {
		if _, has := bs[k]; has {
			delete(bs, k)
			if len(bs) == 0 {
				delete(e.bindings, port)
			}
		}
	}
}

func nonEmpty(fields ...string) bool {
	for _, f := range fields {
		if f == "" {
			return false
		}
	}
	return true
}

// validateReceipt verifies that the segment is a valid prefix and the address
// is a valid address contained by that prefix.
func validateReceipt(segment, address string) error {
	prefix, err := netip.ParsePrefix(segment)
	if err != nil {
		return fmt.Errorf("%w: segment %q: %v", ErrInvalidReceipt, segment, err)
	}
	addr, err := netip.ParseAddr(address)
	if err != nil {
		return fmt.Errorf("%w: address %q: %v", ErrInvalidReceipt, address, err)
	}
	if !prefix.Contains(addr) {
		return fmt.Errorf("%w: address %s outside segment %s", ErrInvalidReceipt, address, segment)
	}
	return nil
}

// detach tears down a session attachment: it removes the session/port mapping,
// all confirmed allocations still owned by the session and any bindings the
// session had established. Isolation holder lists are updated while retaining
// the historical isolation record.
func (e *Engine) detach(session string) {
	port, ok := e.sessionPort[session]
	if !ok {
		return
	}
	delete(e.sessionPort, session)
	if cur, ok := e.portSession[port]; ok && cur == session {
		delete(e.portSession, port)
	}

	// Remove every binding owned by the session on the detached port.
	for k, b := range e.bindings[port] {
		if b.Session == session {
			delete(e.bindings[port], k)
		}
	}
	if len(e.bindings[port]) == 0 {
		delete(e.bindings, port)
	}

	// Capture the affected addresses, then drop the session's allocations
	// before recomputing isolation so the detached session is not counted as a
	// live holder. Historical isolation records remain untouched.
	keys := make([]addrKey, 0, len(e.allocations[session]))
	for k := range e.allocations[session] {
		keys = append(keys, k)
	}
	delete(e.allocations, session)
	for _, k := range keys {
		if _, wasIsolated := e.isolated[k]; wasIsolated {
			e.recomputeIsolation(k)
		}
	}
}

// HandlePortEvent consumes a switch port attachment event.
//
// PortUp for a session already attached elsewhere moves the attachment: the old
// session state is torn down before the new port becomes current. PortUp on a
// port occupied by another session displaces that session. A PortDown is only
// honored when its session is still the port's current attachment; stale
// events for a displaced session are ignored.
func (e *Engine) HandlePortEvent(ev PortEvent) error {
	if !nonEmpty(ev.PortID, ev.Session) {
		return fmt.Errorf("%w: port and session are required", ErrInvalidPortEvent)
	}
	switch ev.Kind {
	case PortUp:
		// Moving the same session to a (potentially new) port ends its old
		// attachment first.
		e.detach(ev.Session)
		// A different session currently trusted on this port is displaced.
		if old, ok := e.portSession[ev.PortID]; ok && old != ev.Session {
			e.detach(old)
		}
		e.sessionPort[ev.Session] = ev.PortID
		e.portSession[ev.PortID] = ev.Session
		if _, ok := e.bindings[ev.PortID]; !ok {
			e.bindings[ev.PortID] = map[addrKey]Binding{}
		}
	case PortDown:
		if cur, ok := e.portSession[ev.PortID]; !ok || cur != ev.Session {
			// Late event for an attachment that was already displaced: ignore.
			return nil
		}
		e.detach(ev.Session)
	default:
		return fmt.Errorf("%w: unknown event kind %q", ErrInvalidPortEvent, ev.Kind)
	}
	return nil
}

// HandleAllocation consumes a trusted allocation acknowledgement.
//
// A fresh acknowledgement whose segment/address is already confirmed to another
// live session isolates that address for every session holding it. Reconfirming
// an allocation the session already holds is idempotent and never triggers a new
// isolation. A release only takes effect when the session still owns the
// confirmed allocation; releases from displaced or unknown sessions are ignored.
func (e *Engine) HandleAllocation(a Allocation) error {
	if !nonEmpty(a.ReceiptID, a.Session, a.Segment, a.Address) {
		return fmt.Errorf("%w: receipt id, session, segment and address are required", ErrInvalidReceipt)
	}
	if err := validateReceipt(a.Segment, a.Address); err != nil {
		return err
	}
	k := key(a.Segment, a.Address)

	if a.Released {
		owned, ok := e.allocations[a.Session]
		receiptID, ownsKey := owned[k]
		if !ok || !ownsKey || receiptID != a.ReceiptID {
			// Late or stale release: the allocation instance is unknown, was
			// already released, or was superseded by a newer receipt after a
			// port change. It must not touch any binding established by the
			// current attachment.
			return nil
		}
		delete(owned, k)
		if len(owned) == 0 {
			delete(e.allocations, a.Session)
		}
		// Remove a binding this session held on its current port for this
		// address; another session's binding is never reachable here.
		if port, ok := e.sessionPort[a.Session]; ok {
			delete(e.bindings[port], k)
			if len(e.bindings[port]) == 0 {
				delete(e.bindings, port)
			}
		}
		// Recompute active isolation; the historical record is retained even
		// once the overlap has resolved.
		if _, wasIsolated := e.isolated[k]; wasIsolated {
			e.recomputeIsolation(k)
		}
		return nil
	}

	// Idempotent reconfirmation of the same allocation instance.
	if owned, ok := e.allocations[a.Session]; ok {
		if receiptID, exists := owned[k]; exists && receiptID == a.ReceiptID {
			return nil
		}
	}
	if _, ok := e.allocations[a.Session]; !ok {
		e.allocations[a.Session] = map[addrKey]string{}
	}
	// A newer receipt supersedes a previous allocation instance for the same
	// session/segment/address (for example after a port change). Any binding
	// established under the old instance is removed and must be re-claimed.
	if oldReceipt, exists := e.allocations[a.Session][k]; exists && oldReceipt != a.ReceiptID {
		if port, ok := e.sessionPort[a.Session]; ok {
			delete(e.bindings[port], k)
			if len(e.bindings[port]) == 0 {
				delete(e.bindings, port)
			}
		}
	}
	e.allocations[a.Session][k] = a.ReceiptID

	// Every other live session still confirming the same address means an
	// overlap that requires isolation. Recompute from the allocation table so
	// that newly (re-)emerging overlaps are captured as distinct isolation
	// epochs; repeated confirmations inside one epoch emit nothing.
	wasIsolated := e.isolatedNow[k]
	e.recomputeIsolation(k)
	if holders, isolated := e.isolated[k]; isolated && !wasIsolated {
		e.isolations = append(e.isolations, IsolationRecord{
			Segment:  a.Segment,
			Address:  a.Address,
			Sessions: append([]string(nil), holders...),
			Sequence: a.Sequence,
		})
	}
	return nil
}

// HandleClaim evaluates a device self-reported address claim. Malformed claims
// return an error; well-formed claims that fail policy are recorded as rejected
// and returned with an explanatory reason code.
func (e *Engine) HandleClaim(c Claim) (Decision, error) {
	if !nonEmpty(c.PortID, c.Session, c.Segment, c.Address) {
		return Decision{}, fmt.Errorf("%w: port, session, segment and address are required", ErrInvalidClaim)
	}
	k := key(c.Segment, c.Address)
	reject := func(reason string) Decision {
		d := Decision{Accepted: false, Reason: reason}
		e.rejectedClaims = append(e.rejectedClaims, RejectedClaim{Claim: c, Reason: reason})
		return d
	}

	// The session must be the current trusted attachment on the claimed port.
	if cur, ok := e.portSession[c.PortID]; !ok || cur != c.Session {
		return reject(ReasonSessionNotOnPort), nil
	}

	// The exact (session, segment, address) triple must be confirmed.
	owned, ok := e.allocations[c.Session]
	if !ok {
		return reject(ReasonNoConfirmedAllocation), nil
	}
	if _, confirmed := owned[k]; !confirmed {
		return reject(ReasonNoConfirmedAllocation), nil
	}

	// Overlapping confirmed allocations keep the address isolated even though
	// this session's own allocation is valid.
	if _, isolated := e.isolated[k]; isolated {
		return reject(ReasonAddressIsolated), nil
	}

	// Defensive: a different session must never already own a binding for this
	// segment/address on the port.
	if existing, ok := e.bindings[c.PortID][k]; ok {
		if existing.Session != c.Session {
			return reject(ReasonAddressBoundByOther), nil
		}
		return Decision{
			Accepted: true,
			Reason:   ReasonAlreadyBound,
			Binding:  &existing,
		}, nil
	}

	b := Binding{
		PortID:   c.PortID,
		Session:  c.Session,
		Segment:  c.Segment,
		Address:  c.Address,
		Sequence: c.Sequence,
	}
	if _, ok := e.bindings[c.PortID]; !ok {
		e.bindings[c.PortID] = map[addrKey]Binding{}
	}
	e.bindings[c.PortID][k] = b
	return Decision{Accepted: true, Reason: ReasonAccepted, Binding: &b}, nil
}

// Snapshot returns a deterministic copy of the current decision state.
func (e *Engine) Snapshot() Snapshot {
	out := Snapshot{
		PortBindings:   map[string][]Binding{},
		Isolations:     append([]IsolationRecord(nil), e.isolations...),
		RejectedClaims: append([]RejectedClaim(nil), e.rejectedClaims...),
	}
	for port, bs := range e.bindings {
		list := make([]Binding, 0, len(bs))
		for _, b := range bs {
			list = append(list, b)
		}
		sort.Slice(list, func(i, j int) bool {
			if list[i].Segment != list[j].Segment {
				return list[i].Segment < list[j].Segment
			}
			return list[i].Address < list[j].Address
		})
		out.PortBindings[port] = list
	}
	sort.Slice(out.Isolations, func(i, j int) bool {
		if out.Isolations[i].Segment != out.Isolations[j].Segment {
			return out.Isolations[i].Segment < out.Isolations[j].Segment
		}
		if out.Isolations[i].Address != out.Isolations[j].Address {
			return out.Isolations[i].Address < out.Isolations[j].Address
		}
		return out.Isolations[i].Sequence < out.Isolations[j].Sequence
	})
	return out
}
