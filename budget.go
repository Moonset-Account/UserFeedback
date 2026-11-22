// Package budget implements hierarchical request budgets for a fan-out RPC
// tree.
//
// A foreground request opens a Root with a fixed action quota and an absolute
// deadline. Every RPC (and every RPC issued by a sub-service) must first
// reserve its maximum usable quota from its parent before it may run. A
// reservation can only hand out quota from its own remaining balance, so the
// tree can never over-allocate the root quota.
//
// Reservations, retries and settlements carry independent identifiers. A
// branch that times out is only marked "unsettled": its quota stays held. The
// balance is returned to the parent only when a valid actual usage is reported
// and every child reservation is settled. Settlement has effect at most once,
// even when a duplicate or late report arrives. Cancelling a branch refunds
// only that branch's own unused quota and never touches quota held by sibling
// reservations that are still in flight. Once the root deadline passes, every
// new action (reserve, retry) is rejected, while reconciliation (timeout,
// settle, cancel) keeps working so already consumed quota can be squared away.
package budget

import (
	"errors"
	"fmt"
	"sync"
	"time"
)

// State is the lifecycle state of a reservation.
type State string

const (
	// StateActive means the reservation is running and may issue children.
	StateActive State = "active"
	// StateUnsettled means the branch timed out and is awaiting a valid
	// actual-usage report. Its quota remains held by its parent.
	StateUnsettled State = "unsettled"
	// StateSettled means a final actual usage was accepted exactly once and
	// the unused balance was returned to the parent.
	StateSettled State = "settled"
	// StateCancelled means the branch was cancelled and its unused balance
	// was returned to the parent.
	StateCancelled State = "cancelled"
)

// Sentinel errors. Use errors.Is to inspect returned errors.
var (
	// ErrDeadlineExceeded reports that a new action was attempted at or
	// after the root deadline.
	ErrDeadlineExceeded = errors.New("budget: root deadline exceeded")
	// ErrInsufficientBudget reports that the requested reservation does not
	// fit into the parent's remaining available quota.
	ErrInsufficientBudget = errors.New("budget: insufficient available quota")
	// ErrInvalidAmount reports a non-positive reservation or a negative
	// usage report.
	ErrInvalidAmount = errors.New("budget: invalid amount")
	// ErrInvalidState reports an operation that the current state forbids.
	ErrInvalidState = errors.New("budget: invalid reservation state")
	// ErrChildrenUnsettled reports a settlement attempt while at least one
	// direct child reservation is still active or unsettled.
	ErrChildrenUnsettled = errors.New("budget: child reservations not settled")
	// ErrChildrenOutstanding reports a cancellation attempt while at least
	// one direct child reservation is still active or unsettled.
	ErrChildrenOutstanding = errors.New("budget: child reservations still outstanding")
	// ErrUsageExceedsReservation reports an actual usage that does not fit
	// into this reservation after its settled children are charged.
	ErrUsageExceedsReservation = errors.New("budget: usage exceeds reservation")
	// ErrAlreadySettled reports a duplicate or late settlement. The first
	// accepted settlement stays in force; the duplicate has no effect.
	ErrAlreadySettled = errors.New("budget: reservation already settled")
	// ErrInvalidConfiguration reports invalid NewRoot arguments.
	ErrInvalidConfiguration = errors.New("budget: invalid root configuration")
)

// Clock supplies the current time. time.Now satisfies it.
type Clock func() time.Time

// Attempt identifies one execution attempt of a reservation. The first
// attempt receives an identifier when the reservation is created, and every
// successful Retry appends another one with a fresh identifier.
type Attempt struct {
	// ID is the independent retry/attempt identifier.
	ID string
	// ReservationID links the attempt to its reservation.
	ReservationID string
	// Seq is 1 for the initial attempt and grows by one per retry.
	Seq int
	// At is the time the attempt was issued.
	At time.Time
}

// Settlement is the single accepted settlement of a reservation.
type Settlement struct {
	// ID is the independent settlement identifier.
	ID string
	// ReservationID links the settlement to its reservation.
	ReservationID string
	// Usage is the accepted actual usage of this reservation itself. Usage
	// already charged by settled children is not included.
	Usage int64
	// Refund is the unused quota returned to the parent:
	// reservation limit minus (own usage plus settled children's charges).
	Refund int64
	// At is the time the settlement was accepted.
	At time.Time
}

// Root is the budget of one foreground request. All methods are safe for
// concurrent use.
type Root struct {
	mu       sync.Mutex
	id       string
	limit    int64
	deadline time.Time
	clock    Clock

	charged int64 // charged quota returned by resolved top-level branches
	held    int64 // limits of top-level branches that have not resolved yet

	nextReserve    uint64
	nextAttempt    uint64
	nextSettlement uint64

	branches []*Reservation
	byID     map[string]*Reservation
}

// Reservation is one reserved quota envelope inside the request tree. It is
// created by Root.Reserve or by Reservation.Reserve and must not be copied.
type Reservation struct {
	root   *Root
	parent *Reservation

	reserveID string
	limit     int64
	state     State

	ownUsage     int64 // accepted own usage, > 0 only once settled
	childCharged int64 // charges of settled or cancelled direct children
	childHeld    int64 // limits of direct children that have not resolved

	attempts   []Attempt
	settlement *Settlement
	children   []*Reservation
}

// NodeAudit is a point-in-time snapshot of one reservation subtree.
//
// For an open node (active or unsettled) the following holds:
//
//	Limit == Charged + Held + Available && Refunded == 0
//
// For a closed node (settled or cancelled) the following holds:
//
//	Limit == Charged + Refunded && Held == 0 && Available == 0
type NodeAudit struct {
	ReserveID string
	State     State
	Limit     int64
	OwnUsage  int64
	Charged   int64 // OwnUsage plus charges of settled descendants
	Held      int64 // limits of unresolved direct children
	Available int64
	Refunded  int64
	Children  []NodeAudit
}

// Audit is a point-in-time snapshot of the whole request budget.
//
// The root invariant Limit == Charged + Held + Available always holds.
type Audit struct {
	RootID    string
	Limit     int64
	Charged   int64
	Held      int64
	Available int64
	Branches  []NodeAudit
}

// NewRoot creates a root request budget. limit is the action quota of the
// whole tree, deadline is the absolute cutoff, and clock may be nil to use
// time.Now. The deadline must be non-zero and limit must be non-negative.
func NewRoot(id string, limit int64, deadline time.Time, clock Clock) (*Root, error) {
	if id == "" {
		return nil, fmt.Errorf("%w: empty root id", ErrInvalidConfiguration)
	}
	if limit < 0 {
		return nil, fmt.Errorf("%w: limit %d must be non-negative", ErrInvalidConfiguration, limit)
	}
	if deadline.IsZero() {
		return nil, fmt.Errorf("%w: zero deadline", ErrInvalidConfiguration)
	}
	if clock == nil {
		clock = time.Now
	}
	return &Root{
		id:       id,
		limit:    limit,
		deadline: deadline,
		clock:    clock,
		byID:     make(map[string]*Reservation),
	}, nil
}

// ID returns the root identifier.
func (r *Root) ID() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.id
}

// Limit returns the root quota.
func (r *Root) Limit() int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.limit
}

// Deadline returns the absolute root deadline.
func (r *Root) Deadline() time.Time {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.deadline
}

// Charged returns the quota charged by top-level branches that have resolved.
func (r *Root) Charged() int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.charged
}

// Held returns the sum of limits of top-level branches that are still active
// or unsettled.
func (r *Root) Held() int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.held
}

// Available returns the quota that a new top-level reservation may use.
func (r *Root) Available() int64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.availableLocked()
}

// Audit returns a consistent snapshot of the whole tree.
func (r *Root) Audit() Audit {
	r.mu.Lock()
	defer r.mu.Unlock()
	a := Audit{
		RootID:    r.id,
		Limit:     r.limit,
		Charged:   r.charged,
		Held:      r.held,
		Available: r.availableLocked(),
	}
	a.Branches = make([]NodeAudit, 0, len(r.branches))
	for _, b := range r.branches {
		a.Branches = append(a.Branches, r.auditNodeLocked(b))
	}
	return a
}

// Reserve reserves amount of quota at the root for one top-level RPC. It fails
// with ErrDeadlineExceeded at or after the root deadline and with
// ErrInsufficientBudget when the amount does not fit.
func (r *Root) Reserve(amount int64) (*Reservation, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.ensureNewActionAllowedLocked(); err != nil {
		return nil, err
	}
	if amount <= 0 {
		return nil, fmt.Errorf("%w: reservation amount %d must be positive", ErrInvalidAmount, amount)
	}
	if available := r.availableLocked(); amount > available {
		return nil, fmt.Errorf("%w: requested %d, available %d", ErrInsufficientBudget, amount, available)
	}
	return r.newReservationLocked(nil, amount), nil
}

// Reserve reserves amount of quota for a child RPC. The child may only use
// quota that this reservation has not already charged or handed to other
// unresolved children. Reserving under a timed-out, settled or cancelled
// branch is rejected.
func (n *Reservation) Reserve(amount int64) (*Reservation, error) {
	r := n.root
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.ensureNewActionAllowedLocked(); err != nil {
		return nil, err
	}
	if n.state != StateActive {
		return nil, fmt.Errorf("%w: parent %s is %s", ErrInvalidState, n.reserveID, n.state)
	}
	if amount <= 0 {
		return nil, fmt.Errorf("%w: reservation amount %d must be positive", ErrInvalidAmount, amount)
	}
	if available := n.availableLocked(); amount > available {
		return nil, fmt.Errorf("%w: requested %d, available %d in %s", ErrInsufficientBudget, amount, available, n.reserveID)
	}
	return r.newReservationLocked(n, amount), nil
}

// Retry starts a new attempt of an active reservation and returns its
// independent retry identifier. Retry is a new action: it is rejected at or
// after the root deadline and on any non-active reservation.
func (n *Reservation) Retry() (Attempt, error) {
	r := n.root
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.ensureNewActionAllowedLocked(); err != nil {
		return Attempt{}, err
	}
	if n.state != StateActive {
		return Attempt{}, fmt.Errorf("%w: retry on %s is %s", ErrInvalidState, n.reserveID, n.state)
	}
	attempt := r.newAttemptLocked(n)
	n.attempts = append(n.attempts, attempt)
	return attempt, nil
}

// Timeout reports that the branch timed out. The reservation moves from active
// to unsettled and keeps its quota held; it cannot spawn new children and it
// cannot be settled until all of its children are settled. Timeout is part of
// reconciliation and is allowed after the root deadline.
func (n *Reservation) Timeout() error {
	r := n.root
	r.mu.Lock()
	defer r.mu.Unlock()
	if n.state != StateActive {
		return fmt.Errorf("%w: timeout on %s is %s", ErrInvalidState, n.reserveID, n.state)
	}
	n.state = StateUnsettled
	return nil
}

// Settle reports the final actual usage of this reservation. It is accepted at
// most once and only when the reservation state is active or unsettled, the
// usage is non-negative, it fits into what the settled children left over, and
// every child reservation is settled (or cancelled). On success the unused
// balance is returned to the parent. Settlement is reconciliation and is
// allowed after the root deadline; a duplicate or late settlement returns
// ErrAlreadySettled without changing any accounting.
func (n *Reservation) Settle(usage int64) (Settlement, error) {
	r := n.root
	r.mu.Lock()
	defer r.mu.Unlock()

	switch n.state {
	case StateSettled:
		return Settlement{}, fmt.Errorf("%w: duplicate or late settlement for %s", ErrAlreadySettled, n.reserveID)
	case StateCancelled:
		return Settlement{}, fmt.Errorf("%w: settlement on cancelled %s", ErrInvalidState, n.reserveID)
	}
	if usage < 0 {
		return Settlement{}, fmt.Errorf("%w: usage %d must be non-negative", ErrInvalidAmount, usage)
	}
	if outstanding := n.unresolvedChildrenLocked(); outstanding > 0 {
		return Settlement{}, fmt.Errorf("%w: %d child reservation(s) still open under %s", ErrChildrenUnsettled, outstanding, n.reserveID)
	}
	maxOwn := n.limit - n.childCharged
	if usage > maxOwn {
		return Settlement{}, fmt.Errorf("%w: usage %d, at most %d under %s", ErrUsageExceedsReservation, usage, maxOwn, n.reserveID)
	}

	n.ownUsage = usage
	n.state = StateSettled
	refund := n.limit - n.chargedLocked()
	n.detachLocked()
	settlement := Settlement{
		ID:            r.newSettlementIDLocked(),
		ReservationID: n.reserveID,
		Usage:         usage,
		Refund:        refund,
		At:            r.nowLocked(),
	}
	n.settlement = &settlement
	return settlement, nil
}

// Cancel closes an active reservation without a usage report and returns its
// unused balance to the parent. Cancellation is refused while any direct child
// is still active or unsettled, and it never touches quota reserved by other
// branches. Cancel is reconciliation and is allowed after the root deadline.
// It returns the refunded amount.
func (n *Reservation) Cancel() (int64, error) {
	r := n.root
	r.mu.Lock()
	defer r.mu.Unlock()

	if n.state != StateActive {
		return 0, fmt.Errorf("%w: cancel on %s is %s", ErrInvalidState, n.reserveID, n.state)
	}
	if outstanding := n.unresolvedChildrenLocked(); outstanding > 0 {
		return 0, fmt.Errorf("%w: %d child reservation(s) still open under %s", ErrChildrenOutstanding, outstanding, n.reserveID)
	}
	refund := n.limit - n.chargedLocked()
	n.state = StateCancelled
	n.detachLocked()
	return refund, nil
}

// ReserveID returns the independent reservation identifier.
func (n *Reservation) ReserveID() string {
	n.root.mu.Lock()
	defer n.root.mu.Unlock()
	return n.reserveID
}

// Limit returns the reserved quota.
func (n *Reservation) Limit() int64 {
	n.root.mu.Lock()
	defer n.root.mu.Unlock()
	return n.limit
}

// State returns the current lifecycle state.
func (n *Reservation) State() State {
	n.root.mu.Lock()
	defer n.root.mu.Unlock()
	return n.state
}

// Charged returns the accepted own usage plus the charges of settled
// descendants.
func (n *Reservation) Charged() int64 {
	n.root.mu.Lock()
	defer n.root.mu.Unlock()
	return n.chargedLocked()
}

// Available returns the quota this reservation may still hand to new children.
// It is zero for closed reservations.
func (n *Reservation) Available() int64 {
	n.root.mu.Lock()
	defer n.root.mu.Unlock()
	return n.availableLocked()
}

// Held returns the sum of limits of unresolved direct children.
func (n *Reservation) Held() int64 {
	n.root.mu.Lock()
	defer n.root.mu.Unlock()
	return n.childHeld
}

// Attempts returns a copy of the attempt records.
func (n *Reservation) Attempts() []Attempt {
	n.root.mu.Lock()
	defer n.root.mu.Unlock()
	out := make([]Attempt, len(n.attempts))
	copy(out, n.attempts)
	return out
}

// Settlement returns the accepted settlement, if any.
func (n *Reservation) Settlement() (Settlement, bool) {
	n.root.mu.Lock()
	defer n.root.mu.Unlock()
	if n.settlement == nil {
		return Settlement{}, false
	}
	return *n.settlement, true
}

// Children returns the direct child reservations in creation order.
func (n *Reservation) Children() []*Reservation {
	n.root.mu.Lock()
	defer n.root.mu.Unlock()
	out := make([]*Reservation, len(n.children))
	copy(out, n.children)
	return out
}

// --- internal helpers -------------------------------------------------------

func (r *Root) nowLocked() time.Time {
	return r.clock()
}

func (r *Root) ensureNewActionAllowedLocked() error {
	now := r.nowLocked()
	if !now.Before(r.deadline) {
		return fmt.Errorf("%w: now %s, deadline %s", ErrDeadlineExceeded,
			now.Format(time.RFC3339Nano), r.deadline.Format(time.RFC3339Nano))
	}
	return nil
}

func (r *Root) availableLocked() int64 {
	return r.limit - r.charged - r.held
}

func (n *Reservation) chargedLocked() int64 {
	return n.childCharged + n.ownUsage
}

func (n *Reservation) availableLocked() int64 {
	if n.state == StateSettled || n.state == StateCancelled {
		return 0
	}
	return n.limit - n.chargedLocked() - n.childHeld
}

func (n *Reservation) unresolvedChildrenLocked() int {
	count := 0
	for _, c := range n.children {
		if c.state == StateActive || c.state == StateUnsettled {
			count++
		}
	}
	return count
}

// detachLocked removes this reservation's limit from the parent and adds its
// accepted charges instead. It must be called exactly once when the
// reservation closes.
func (n *Reservation) detachLocked() {
	charged := n.chargedLocked()
	if n.parent == nil {
		n.root.held -= n.limit
		n.root.charged += charged
		return
	}
	n.parent.childHeld -= n.limit
	n.parent.childCharged += charged
}

func (r *Root) newReservationLocked(parent *Reservation, amount int64) *Reservation {
	r.nextReserve++
	n := &Reservation{
		root:      r,
		parent:    parent,
		reserveID: fmt.Sprintf("res-%d", r.nextReserve),
		limit:     amount,
		state:     StateActive,
	}
	n.attempts = append(n.attempts, r.newAttemptLocked(n))
	if parent == nil {
		r.branches = append(r.branches, n)
		r.held += amount
	} else {
		parent.children = append(parent.children, n)
		parent.childHeld += amount
	}
	r.byID[n.reserveID] = n
	return n
}

func (r *Root) newAttemptLocked(n *Reservation) Attempt {
	r.nextAttempt++
	return Attempt{
		ID:            fmt.Sprintf("try-%d", r.nextAttempt),
		ReservationID: n.reserveID,
		Seq:           len(n.attempts) + 1,
		At:            r.nowLocked(),
	}
}

func (r *Root) newSettlementIDLocked() string {
	r.nextSettlement++
	return fmt.Sprintf("set-%d", r.nextSettlement)
}

func (r *Root) auditNodeLocked(n *Reservation) NodeAudit {
	a := NodeAudit{
		ReserveID: n.reserveID,
		State:     n.state,
		Limit:     n.limit,
		OwnUsage:  n.ownUsage,
		Charged:   n.chargedLocked(),
		Held:      n.childHeld,
	}
	if n.state == StateSettled || n.state == StateCancelled {
		a.Refunded = n.limit - a.Charged
	} else {
		a.Available = n.limit - a.Charged - n.childHeld
	}
	a.Children = make([]NodeAudit, 0, len(n.children))
	for _, c := range n.children {
		a.Children = append(a.Children, r.auditNodeLocked(c))
	}
	return a
}
