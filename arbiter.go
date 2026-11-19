// Package arbiter implements the peripheral interface admission decision
// module for a managed host.
//
// A managed device enumerates a set of functional interfaces on every
// insertion / mode change. Each interface belongs to a hardware isolation
// group and declares a function. The host keeps a list of functions the
// device is approved to use. Some interfaces are hardware-bonded and must
// be enabled together ("co-enabled"); such interfaces form bundles whose
// enablement is all-or-nothing.
//
// Rules enforced here:
//
//  1. An interface whose function is not approved is never enabled.
//  2. If an isolation group contains a non-isolatable unapproved function,
//     the whole group stays closed: every non-isolatable interface in that
//     group is denied. Independently isolatable approved interfaces remain
//     admissible even when their group is otherwise tainted.
//  3. Co-enabled interfaces form bundles; a bundle is admissible only when
//     every member is individually admissible, otherwise the whole bundle
//     is denied.
//  4. When the device changes working mode and re-enumerates (a new
//     enumeration under the same insertion, or a new insertion), the old
//     interfaces are torn down first and the decision is recomputed from
//     the new topology. Enable confirmations that arrive late, stamped
//     with a previous insertion/enumeration round, are rejected and can
//     never restore an old permission.
package arbiter

import (
	"errors"
)

// Sentinel errors returned by the arbiter.
var (
	// ErrInvalidEnumeration is returned when an Enumeration input is
	// malformed (empty identifiers, unknown interface references, ...).
	ErrInvalidEnumeration = errors.New("arbiter: invalid enumeration input")

	// ErrStaleRound is returned when an enable confirmation carries an
	// insertion/enumeration round that is no longer current.
	ErrStaleRound = errors.New("arbiter: stale enable confirmation")

	// ErrUnknownInterface is returned when an enable confirmation names an
	// interface that does not exist in the current enumeration.
	ErrUnknownInterface = errors.New("arbiter: unknown interface")

	// ErrInterfaceDenied is returned when an enable confirmation targets an
	// interface that the current decision did not permit.
	ErrInterfaceDenied = errors.New("arbiter: interface not permitted")
)

// Function is the functional descriptor advertised by an interface
// (for example "usb.hid.keyboard" or "usb.mass-storage").
type Function string

// Interface is one enumerated interface of a device.
type Interface struct {
	// ID is the interface identifier unique within one enumeration.
	ID string

	// Function is the function this interface advertises.
	Function Function

	// IsolationGroup identifies the hardware isolation group the
	// interface belongs to. Interfaces in the same group share hardware
	// unless an interface is independently isolatable.
	IsolationGroup string

	// IndependentlyIsolatable means the interface can be fenced off on its
	// own, so it stays admissible even when its isolation group is tainted
	// by another member's unapproved function.
	IndependentlyIsolatable bool
}

// Enumeration is one full enumeration snapshot produced by a device.
type Enumeration struct {
	// InsertionID numbers each physical insertion of the device.
	InsertionID int

	// EnumerationID numbers each (re-)enumeration within one insertion.
	// A mode change produces a new EnumerationID under the same
	// InsertionID and presents a new topology.
	EnumerationID int

	// Interfaces is the full interface list of the current topology.
	Interfaces []Interface

	// CoEnableSets lists interface IDs that are hardware-bonded and must
	// be enabled together. Transitive bonding (A-B and B-C) collapses into
	// one bundle automatically.
	CoEnableSets [][]string

	// ApprovedFunctions are the functions this device is approved to use
	// for this insertion.
	ApprovedFunctions map[Function]bool
}

// Decision is the verdict for one enumeration.
type Decision struct {
	InsertionID     int
	EnumerationID   int
	Permitted       []string // interfaces allowed to be enabled
	Denied          []string // interfaces rejected by policy
	RejectionReason map[string]error
}

// interfaceState tracks the lifecycle of one interface in the current round.
type interfaceState struct {
	iface     Interface
	permitted bool
	active    bool // a matching enable confirmation has been accepted
}

// Arbiter owns the admission state across enumerations of one managed host.
type Arbiter struct {
	insertionID   int
	enumerationID int
	interfaces    map[string]*interfaceState
	order         []string // stable interface order of the current round
}

// New returns an empty Arbiter.
func New() *Arbiter {
	return &Arbiter{
		interfaces: map[string]*interfaceState{},
	}
}

// Adjudicate evaluates one enumeration snapshot.
//
// When the round differs from the round currently installed (a new
// insertion, or a re-enumeration after a mode change), every previously
// active interface is torn down before the new topology is judged: the
// returned ActiveInterfaces reflects only confirmations accepted against
// the new decision. Re-adjudicating the exact same round is idempotent and
// returns the cached decision without tearing anything down.
func (a *Arbiter) Adjudicate(enum Enumeration) (*Decision, error) {
	if err := validate(enum); err != nil {
		return nil, err
	}

	sameRound := enum.InsertionID == a.insertionID && enum.EnumerationID == a.enumerationID
	if sameRound {
		return a.currentDecision(), nil
	}

	// The device changed mode / was re-inserted: tear down every old
	// interface before judging the new topology.
	a.interfaces = map[string]*interfaceState{}
	a.order = nil
	a.insertionID = enum.InsertionID
	a.enumerationID = enum.EnumerationID

	index := map[string]int{}
	for i, iface := range enum.Interfaces {
		index[iface.ID] = i
	}

	// Step 1: determine which isolation groups are tainted. A group is
	// tainted when it contains a non-isolatable interface whose function
	// is not approved. Independently isolatable members cannot taint the
	// shared part of the group.
	tainted := map[string]bool{}
	for _, iface := range enum.Interfaces {
		if iface.IndependentlyIsolatable {
			continue
		}
		if !enum.ApprovedFunctions[iface.Function] {
			tainted[iface.IsolationGroup] = true
		}
	}

	// Step 2: per-interface admissibility before co-enable constraints.
	admissible := map[string]bool{}
	reasons := map[string]error{}
	for _, iface := range enum.Interfaces {
		st := &interfaceState{iface: iface}
		a.interfaces[iface.ID] = st
		a.order = append(a.order, iface.ID)

		if !enum.ApprovedFunctions[iface.Function] {
			reasons[iface.ID] = ErrInterfaceDenied
			continue
		}
		if !iface.IndependentlyIsolatable && tainted[iface.IsolationGroup] {
			reasons[iface.ID] = ErrInterfaceDenied
			continue
		}
		admissible[iface.ID] = true
	}

	// Step 3: collapse the co-enable declarations into bundles via
	// union-find and enforce all-or-nothing admissibility.
	uf := newUnionFind(len(enum.Interfaces))
	for _, set := range enum.CoEnableSets {
		first := index[set[0]]
		for _, id := range set[1:] {
			uf.union(first, index[id])
		}
	}

	bundles := map[int][]string{}
	for _, id := range a.order {
		root := uf.find(index[id])
		bundles[root] = append(bundles[root], id)
	}
	// Singleton "bundles" (interfaces without any co-enable bond) keep
	// their individual admissibility; only real bonded bundles are gated.
	for root, members := range bundles {
		if uf.size[root] < 2 {
			continue
		}
		for _, id := range members {
			if !admissible[id] {
				for _, bonded := range members {
					admissible[bonded] = false
					reasons[bonded] = ErrInterfaceDenied
				}
				break
			}
		}
	}

	for _, id := range a.order {
		a.interfaces[id].permitted = admissible[id]
	}

	return a.currentDecision(), nil
}

// AckEnable handles an asynchronous enable confirmation from the device.
//
// The confirmation is accepted only when its insertion and enumeration
// identifiers match the round currently installed, the interface exists in
// that topology and the decision permitted it. A late confirmation stamped
// with a previous round is rejected with ErrStaleRound and never re-enables
// an interface that was torn down.
func (a *Arbiter) AckEnable(insertionID, enumerationID int, interfaceID string) error {
	if insertionID != a.insertionID || enumerationID != a.enumerationID {
		return ErrStaleRound
	}
	st, ok := a.interfaces[interfaceID]
	if !ok {
		return ErrUnknownInterface
	}
	if !st.permitted {
		return ErrInterfaceDenied
	}
	st.active = true
	return nil
}

// ActiveInterfaces returns the interfaces actually enabled in the current
// round, in enumeration order: permitted interfaces whose enable
// confirmation has been accepted.
func (a *Arbiter) ActiveInterfaces() []string {
	active := []string{}
	for _, id := range a.order {
		if a.interfaces[id].active {
			active = append(active, id)
		}
	}
	return active
}

// PermittedInterfaces returns the interfaces the current decision allows,
// in enumeration order, whether or not their confirmation arrived yet.
func (a *Arbiter) PermittedInterfaces() []string {
	permitted := []string{}
	for _, id := range a.order {
		if a.interfaces[id].permitted {
			permitted = append(permitted, id)
		}
	}
	return permitted
}

// CurrentRound returns the insertion and enumeration identifiers installed.
func (a *Arbiter) CurrentRound() (insertionID, enumerationID int) {
	return a.insertionID, a.enumerationID
}

func (a *Arbiter) currentDecision() *Decision {
	d := &Decision{
		InsertionID:     a.insertionID,
		EnumerationID:   a.enumerationID,
		Permitted:       []string{},
		Denied:          []string{},
		RejectionReason: map[string]error{},
	}
	for _, id := range a.order {
		st := a.interfaces[id]
		if st.permitted {
			d.Permitted = append(d.Permitted, id)
		} else {
			d.Denied = append(d.Denied, id)
			d.RejectionReason[id] = ErrInterfaceDenied
		}
	}
	return d
}

func validate(enum Enumeration) error {
	if enum.InsertionID <= 0 || enum.EnumerationID <= 0 {
		return ErrInvalidEnumeration
	}
	if len(enum.Interfaces) == 0 {
		return ErrInvalidEnumeration
	}
	seen := map[string]bool{}
	for _, iface := range enum.Interfaces {
		if iface.ID == "" || iface.IsolationGroup == "" || seen[iface.ID] {
			return ErrInvalidEnumeration
		}
		seen[iface.ID] = true
	}
	for _, set := range enum.CoEnableSets {
		if len(set) < 2 {
			return ErrInvalidEnumeration
		}
		for _, id := range set {
			if !seen[id] {
				return ErrInvalidEnumeration
			}
		}
	}
	return nil
}

// unionFind is a small integer-keyed disjoint-set structure.
type unionFind struct {
	parent []int
	rank   []int
	size   []int
}

func newUnionFind(n int) *unionFind {
	uf := &unionFind{
		parent: make([]int, n),
		rank:   make([]int, n),
		size:   make([]int, n),
	}
	for i := range uf.parent {
		uf.parent[i] = i
		uf.size[i] = 1
	}
	return uf
}

func (uf *unionFind) find(x int) int {
	root := x
	for uf.parent[root] != root {
		root = uf.parent[root]
	}
	for uf.parent[x] != x {
		next := uf.parent[x]
		uf.parent[x] = root
		x = next
	}
	return root
}

func (uf *unionFind) union(x, y int) {
	rx, ry := uf.find(x), uf.find(y)
	if rx == ry {
		return
	}
	if uf.rank[rx] < uf.rank[ry] {
		rx, ry = ry, rx
	}
	uf.parent[ry] = rx
	uf.size[rx] += uf.size[ry]
	if uf.rank[rx] == uf.rank[ry] {
		uf.rank[rx]++
	}
}
