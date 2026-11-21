// Package rowlock implements a deterministic, single-process row lock manager
// with shared (S), exclusive (X) and in-place upgrade (S -> X) support.
//
// Scheduling rules:
//
//   - A request is granted only when it is compatible with every current
//     holder of the row (a transaction's own lock never blocks it) and no
//     earlier ungranted request in the row queue is incompatible with it.
//     The queue is examined strictly head-first, so a later request can never
//     jump ahead of an earlier incompatible one.
//   - Because the queue is drained head-first, a run of consecutive compatible
//     shared requests is granted together in one step.
//   - An upgrade keeps the caller's shared lock while the exclusive request
//     waits. The upgrade is granted in place once the caller is the only
//     remaining holder of the row.
//
// Deadlocks are detected on the wait-for graph built from direct blocking
// (conflicting holders plus earlier incompatible queue entries) and resolved
// by aborting the most recently started transaction of each cyclic component,
// releasing all of its locks on all rows and re-running the grant pass until no
// cycle remains. Startup order is unique and monotonic because duplicate
// transaction ids are rejected.
package rowlock

import (
	"errors"
	"fmt"
	"sort"
	"strings"
)

// TxnID identifies a transaction. Ids are supplied by the caller and must be
// unique within a Manager.
type TxnID string

// Mode is a lock mode.
type Mode int

const (
	// Shared is a read lock. Shared and Shared are compatible.
	Shared Mode = iota + 1
	// Exclusive is a write lock. It conflicts with every other mode.
	Exclusive
)

// String renders the mode as "S" or "X".
func (m Mode) String() string {
	switch m {
	case Shared:
		return "S"
	case Exclusive:
		return "X"
	default:
		return fmt.Sprintf("Mode(%d)", int(m))
	}
}

func compatible(a, b Mode) bool { return a == Shared && b == Shared }

// Status is a transaction lifecycle state.
type Status int

const (
	// Active transactions may request and hold locks.
	Active Status = iota + 1
	// Committed transactions have ended cleanly; all locks are released.
	Committed
	// Aborted transactions were deadlock victims; all locks are released.
	Aborted
)

func (s Status) String() string {
	switch s {
	case Active:
		return "active"
	case Committed:
		return "committed"
	case Aborted:
		return "aborted"
	default:
		return fmt.Sprintf("Status(%d)", int(s))
	}
}

// Errors returned by Manager.
var (
	ErrUnknownTxn      = errors.New("rowlock: unknown transaction")
	ErrDuplicateTxn    = errors.New("rowlock: duplicate transaction id")
	ErrEmptyTxnID      = errors.New("rowlock: empty transaction id")
	ErrTxnNotActive    = errors.New("rowlock: transaction is not active")
	ErrInvalidMode     = errors.New("rowlock: invalid lock mode")
	ErrNotHeld         = errors.New("rowlock: lock not held")
	ErrCannotDowngrade = errors.New("rowlock: cannot downgrade a pending request")
)

// HeldLock describes one lock owned by a transaction.
type HeldLock struct {
	Txn  TxnID
	Row  string
	Mode Mode
}

// Grant describes one request that became a holder during a step. Upgrade is
// true when the grant was an in-place S -> X upgrade.
type Grant struct {
	Txn     TxnID
	Row     string
	Mode    Mode
	Upgrade bool
}

// DeadlockEvent describes one resolution round.
type DeadlockEvent struct {
	// Cycle holds every transaction of a strongly connected component of the
	// wait-for graph (i.e. every transaction that lies on a cycle), sorted by
	// startup order.
	Cycle []TxnID
	// Victim is the most recently started transaction in Cycle.
	Victim TxnID
	// Released lists the locks the victim held on all rows, sorted by row.
	Released []HeldLock
}

// LockResult reports the outcome of a lock or upgrade request.
type LockResult struct {
	Txn      TxnID
	Row      string
	Mode     Mode
	Granted  bool // the caller now holds at least the requested mode
	Upgraded bool // the grant was an in-place S -> X upgrade
	Waiting  bool // the caller still has a queued request on the row
	Aborted  bool // the caller was selected as a deadlock victim
	// InCycle reports that the caller appeared on at least one cycle resolved
	// during this step, whether or not it was the victim.
	InCycle   bool
	Grants    []Grant
	Deadlocks []DeadlockEvent
	// Queue is the affected row's pending requests after the step, so the
	// caller can see who is still waiting and in what order.
	Queue []QueuedRequest
	// Holders is the affected row's granted locks after the step.
	Holders []Holder
}

// String explains the outcome in one line, including every deadlock round and
// the residual queue of the affected row.
func (r LockResult) String() string {
	var b strings.Builder
	fmt.Fprintf(&b, "txn %s request %s on %q: ", r.Txn, r.Mode, r.Row)
	switch {
	case r.Aborted:
		b.WriteString("aborted as deadlock victim")
	case r.Granted && r.Upgraded:
		b.WriteString("upgraded in place")
	case r.Granted:
		b.WriteString("granted")
	case r.Waiting:
		b.WriteString("waiting")
	default:
		b.WriteString("unchanged")
	}
	for _, d := range r.Deadlocks {
		fmt.Fprintf(&b, "; deadlock %v: aborted %s, released %v", d.Cycle, d.Victim, d.Released)
	}
	for _, g := range r.Grants {
		if g.Upgrade {
			fmt.Fprintf(&b, "; granted in-place upgrade %s %s on %q", g.Txn, g.Mode, g.Row)
		} else {
			fmt.Fprintf(&b, "; granted %s %s on %q", g.Txn, g.Mode, g.Row)
		}
	}
	fmt.Fprintf(&b, "; holders %v", r.Holders)
	if len(r.Queue) > 0 {
		fmt.Fprintf(&b, "; still queued %v", r.Queue)
	}
	return b.String()
}

// StepResult reports the grants and deadlock rounds caused by release or end.
type StepResult struct {
	Grants    []Grant
	Deadlocks []DeadlockEvent
}

// WaitEdge is a directed wait-for edge: Waiter is blocked by Blocker on Row.
type WaitEdge struct {
	Waiter  TxnID
	Blocker TxnID
	Row     string
}

// Holder is one granted lock in a snapshot.
type Holder struct {
	Txn  TxnID
	Mode Mode
}

// QueuedRequest is one pending request in a snapshot.
type QueuedRequest struct {
	Txn     TxnID
	Mode    Mode
	Upgrade bool
}

// RowSnapshot is the state of a single row.
type RowSnapshot struct {
	Row     string
	Holders []Holder
	Queue   []QueuedRequest
}

func (h Holder) String() string { return fmt.Sprintf("%s:%s", h.Txn, h.Mode) }

func (q QueuedRequest) String() string {
	if q.Upgrade {
		return fmt.Sprintf("%s:upgrade-%s", q.Txn, q.Mode)
	}
	return fmt.Sprintf("%s:%s", q.Txn, q.Mode)
}

type txn struct {
	id    TxnID
	start int
	state Status
}

type request struct {
	txn     TxnID
	mode    Mode
	upgrade bool // the txn already holds S and asks for X
}

type row struct {
	holders map[TxnID]Mode
	queue   []*request
}

func (r *row) indexOf(id TxnID) int {
	for i, req := range r.queue {
		if req.txn == id {
			return i
		}
	}
	return -1
}

func (r *row) removeQueued(id TxnID) {
	out := r.queue[:0]
	for _, req := range r.queue {
		if req.txn != id {
			out = append(out, req)
		}
	}
	r.queue = out
}

// grantable reports whether req conflicts with no holder other than itself.
// The caller's own held lock is deliberately ignored so that an upgrade is not
// blocked by the shared lock it is upgrading.
func (r *row) grantable(req *request) bool {
	for h, mode := range r.holders {
		if h == req.txn {
			continue
		}
		if !compatible(req.mode, mode) {
			return false
		}
	}
	return true
}

// Manager is a deterministic row lock manager. All methods are safe to drive
// in a fixed sequence from a single goroutine; no wall-clock or background
// scheduling is involved.
type Manager struct {
	rows    map[string]*row
	txns    map[TxnID]*txn
	nextSeq int
}

// NewManager creates an empty lock manager.
func NewManager() *Manager {
	return &Manager{
		rows: make(map[string]*row),
		txns: make(map[TxnID]*txn),
	}
}

// Begin registers a transaction and assigns it the next startup sequence
// number. Startup order is unique: duplicate or empty ids are rejected.
func (m *Manager) Begin(id TxnID) error {
	if id == "" {
		return ErrEmptyTxnID
	}
	if _, exists := m.txns[id]; exists {
		return fmt.Errorf("%w: %q", ErrDuplicateTxn, id)
	}
	m.nextSeq++
	m.txns[id] = &txn{id: id, start: m.nextSeq, state: Active}
	return nil
}

// StartOrder returns the unique, monotonically increasing startup number.
func (m *Manager) StartOrder(id TxnID) (int, error) {
	t, ok := m.txns[id]
	if !ok {
		return 0, fmt.Errorf("%w: %q", ErrUnknownTxn, id)
	}
	return t.start, nil
}

// Status returns the lifecycle state of a transaction.
func (m *Manager) Status(id TxnID) (Status, error) {
	t, ok := m.txns[id]
	if !ok {
		return 0, fmt.Errorf("%w: %q", ErrUnknownTxn, id)
	}
	return t.state, nil
}

func (m *Manager) startOf(id TxnID) int {
	if t, ok := m.txns[id]; ok {
		return t.start
	}
	return 1 << 30
}

func (m *Manager) rowNames() []string {
	names := make([]string, 0, len(m.rows))
	for name := range m.rows {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func (m *Manager) rowFor(name string) *row {
	r, ok := m.rows[name]
	if !ok {
		r = &row{holders: make(map[TxnID]Mode)}
		m.rows[name] = r
	}
	return r
}

// Lock requests mode on row for txn. Requesting Exclusive while holding Shared
// is an in-place upgrade: the shared lock is retained while the request waits.
// Requesting a mode already held is a no-op success. A call never errors for a
// deadlock; the outcome is reported through LockResult.
func (m *Manager) Lock(id TxnID, rowName string, mode Mode) (LockResult, error) {
	t, ok := m.txns[id]
	if !ok {
		return LockResult{}, fmt.Errorf("%w: %q", ErrUnknownTxn, id)
	}
	if t.state != Active {
		return LockResult{}, fmt.Errorf("%w: %q is %s", ErrTxnNotActive, id, t.state)
	}
	if mode != Shared && mode != Exclusive {
		return LockResult{}, fmt.Errorf("%w: %d", ErrInvalidMode, int(mode))
	}

	res := LockResult{Txn: id, Row: rowName, Mode: mode}
	r := m.rowFor(rowName)

	if held, holds := r.holders[id]; holds {
		switch {
		case held == Exclusive:
			res.Granted = true
			return res, nil
		case held == Shared && mode == Shared:
			res.Granted = true
			return res, nil
		}
	}

	if idx := r.indexOf(id); idx >= 0 {
		req := r.queue[idx]
		switch {
		case req.mode == mode:
			res.Waiting = true
			return res, nil
		case req.mode == Exclusive && mode == Shared:
			return LockResult{}, fmt.Errorf("%w: %s holds a pending X on %q", ErrCannotDowngrade, id, rowName)
		}
		// Escalate the queued request in place; it keeps its queue position.
		req.mode = mode
	} else {
		held, holds := r.holders[id]
		r.queue = append(r.queue, &request{
			txn:     id,
			mode:    mode,
			upgrade: holds && held == Shared && mode == Exclusive,
		})
	}

	grants, deadlocks := m.step()
	res.Grants = grants
	res.Deadlocks = deadlocks

	state, _ := m.Status(id)
	switch {
	case state == Aborted:
		res.Aborted = true
		res.Granted = false
		res.Waiting = false
	case r.indexOf(id) >= 0:
		res.Waiting = true
		res.Granted = false
	default:
		held, holds := r.holders[id]
		res.Granted = holds && (held == mode || held == Exclusive)
		for _, g := range grants {
			if g.Txn == id && g.Row == rowName && g.Upgrade {
				res.Upgraded = true
			}
		}
	}
	res.Holders, res.Queue = m.rowState(rowName)
	for _, d := range deadlocks {
		for _, member := range d.Cycle {
			if member == id {
				res.InCycle = true
			}
		}
	}
	return res, nil
}

// Release cancels any pending request on the row and releases a held lock. It
// is an error if the transaction neither holds nor waits on the row.
func (m *Manager) Release(id TxnID, rowName string) (StepResult, error) {
	t, ok := m.txns[id]
	if !ok {
		return StepResult{}, fmt.Errorf("%w: %q", ErrUnknownTxn, id)
	}
	if t.state != Active {
		return StepResult{}, fmt.Errorf("%w: %q is %s", ErrTxnNotActive, id, t.state)
	}
	r, ok := m.rows[rowName]
	if !ok {
		return StepResult{}, fmt.Errorf("%w: %s holds nothing on %q", ErrNotHeld, id, rowName)
	}
	_, held := r.holders[id]
	pending := r.indexOf(id) >= 0
	if !held && !pending {
		return StepResult{}, fmt.Errorf("%w: %s holds nothing on %q", ErrNotHeld, id, rowName)
	}
	delete(r.holders, id)
	r.removeQueued(id)

	grants, deadlocks := m.step()
	return StepResult{Grants: grants, Deadlocks: deadlocks}, nil
}

// End finishes a transaction and releases every lock it holds on every row.
// Ending a committed or aborted transaction is a no-op that returns nil.
func (m *Manager) End(id TxnID) (StepResult, error) {
	t, ok := m.txns[id]
	if !ok {
		return StepResult{}, fmt.Errorf("%w: %q", ErrUnknownTxn, id)
	}
	if t.state != Active {
		return StepResult{}, nil
	}
	t.state = Committed
	for _, name := range m.rowNames() {
		r := m.rows[name]
		delete(r.holders, id)
		r.removeQueued(id)
	}
	grants, deadlocks := m.step()
	return StepResult{Grants: grants, Deadlocks: deadlocks}, nil
}

// grantAll drains every row queue once, in sorted row order.
func (m *Manager) grantAll() []Grant {
	var grants []Grant
	for _, name := range m.rowNames() {
		grants = append(grants, m.grantRow(name, m.rows[name])...)
	}
	return grants
}

// grantRow drains the queue head-first. It stops at the first blocked request,
// which both prevents barging past an incompatible request and lets a run of
// consecutive compatible shared requests be granted together in one pass.
func (m *Manager) grantRow(name string, r *row) []Grant {
	var grants []Grant
	for len(r.queue) > 0 {
		req := r.queue[0]
		if !r.grantable(req) {
			break
		}
		held, holds := r.holders[req.txn]
		if holds && held == req.mode {
			// Already holds the requested mode; drop the redundant request.
			r.queue = r.queue[1:]
			continue
		}
		upgrade := holds && held == Shared && req.mode == Exclusive
		r.holders[req.txn] = req.mode
		r.queue = r.queue[1:]
		grants = append(grants, Grant{Txn: req.txn, Row: name, Mode: req.mode, Upgrade: upgrade})
	}
	return grants
}

// step runs a grant pass, then resolves deadlocks one victim at a time until
// the wait-for graph is acyclic.
func (m *Manager) step() ([]Grant, []DeadlockEvent) {
	var grants []Grant
	var events []DeadlockEvent
	for {
		grants = append(grants, m.grantAll()...)
		cycles := m.Cycles()
		if len(cycles) == 0 {
			return m.filterAborted(grants), events
		}
		cycle := cycles[0]
		victim := cycle[len(cycle)-1] // Cycles are sorted by startup order.
		released := m.abort(victim)
		events = append(events, DeadlockEvent{
			Cycle:    append([]TxnID(nil), cycle...),
			Victim:   victim,
			Released: released,
		})
	}
}

// filterAborted drops grants that were later undone by a deadlock abort in the
// same step, so a caller never reads a grant that did not survive the step.
func (m *Manager) filterAborted(grants []Grant) []Grant {
	out := grants[:0]
	for _, g := range grants {
		if t, ok := m.txns[g.Txn]; ok && t.state == Aborted {
			continue
		}
		out = append(out, g)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// abort marks id aborted and releases all of its locks and queued requests.
func (m *Manager) abort(id TxnID) []HeldLock {
	t := m.txns[id]
	t.state = Aborted
	var released []HeldLock
	for _, name := range m.rowNames() {
		r := m.rows[name]
		if mode, ok := r.holders[id]; ok {
			released = append(released, HeldLock{Txn: id, Row: name, Mode: mode})
			delete(r.holders, id)
		}
		r.removeQueued(id)
	}
	return released
}

// graph builds the wait-for graph from direct blocking only: conflicting
// holders and earlier incompatible queue entries. Locks held by the waiting
// transaction itself are excluded, so no self-edge is ever produced.
func (m *Manager) graph() (map[TxnID]map[TxnID]bool, []WaitEdge) {
	adj := make(map[TxnID]map[TxnID]bool)
	var edges []WaitEdge
	seen := make(map[WaitEdge]bool)

	add := func(waiter, blocker TxnID, rowName string) {
		if waiter == blocker {
			return
		}
		e := WaitEdge{Waiter: waiter, Blocker: blocker, Row: rowName}
		if seen[e] {
			return
		}
		seen[e] = true
		edges = append(edges, e)
		if adj[waiter] == nil {
			adj[waiter] = make(map[TxnID]bool)
		}
		adj[waiter][blocker] = true
	}

	for _, name := range m.rowNames() {
		r := m.rows[name]
		for i, req := range r.queue {
			for holder, mode := range r.holders {
				if holder == req.txn {
					continue // never blocked by its own lock
				}
				if !compatible(req.mode, mode) {
					add(req.txn, holder, name)
				}
			}
			for j := 0; j < i; j++ {
				earlier := r.queue[j]
				if earlier.txn == req.txn {
					continue
				}
				if !compatible(req.mode, earlier.mode) {
					add(req.txn, earlier.txn, name)
				}
			}
		}
	}

	sort.Slice(edges, func(i, j int) bool {
		a, b := edges[i], edges[j]
		if a.Waiter != b.Waiter {
			return m.startOf(a.Waiter) < m.startOf(b.Waiter)
		}
		if a.Blocker != b.Blocker {
			return m.startOf(a.Blocker) < m.startOf(b.Blocker)
		}
		return a.Row < b.Row
	})
	return adj, edges
}

// WaitFor returns the current wait-for edges in deterministic order.
func (m *Manager) WaitFor() []WaitEdge {
	_, edges := m.graph()
	return edges
}

// Cycles returns every cyclic strongly connected component of the wait-for
// graph. Members are sorted by startup order and components are ordered
// deterministically, so the first component is always the same for a state.
func (m *Manager) Cycles() [][]TxnID {
	adj, _ := m.graph()

	nodes := make([]TxnID, 0, len(adj))
	for n := range adj {
		nodes = append(nodes, n)
	}
	sort.Slice(nodes, func(i, j int) bool { return m.startOf(nodes[i]) < m.startOf(nodes[j]) })

	index := 0
	indices := make(map[TxnID]int, len(nodes))
	low := make(map[TxnID]int, len(nodes))
	onStack := make(map[TxnID]bool, len(nodes))
	var stack []TxnID
	var comps [][]TxnID

	var strongconnect func(v TxnID)
	strongconnect = func(v TxnID) {
		indices[v] = index
		low[v] = index
		index++
		stack = append(stack, v)
		onStack[v] = true

		succs := make([]TxnID, 0, len(adj[v]))
		for w := range adj[v] {
			succs = append(succs, w)
		}
		sort.Slice(succs, func(i, j int) bool { return m.startOf(succs[i]) < m.startOf(succs[j]) })

		for _, w := range succs {
			if _, seen := indices[w]; !seen {
				strongconnect(w)
				if low[w] < low[v] {
					low[v] = low[w]
				}
			} else if onStack[w] && indices[w] < low[v] {
				low[v] = indices[w]
			}
		}

		if low[v] != indices[v] {
			return
		}
		var comp []TxnID
		for {
			w := stack[len(stack)-1]
			stack = stack[:len(stack)-1]
			onStack[w] = false
			comp = append(comp, w)
			if w == v {
				break
			}
		}
		if len(comp) > 1 {
			sort.Slice(comp, func(i, j int) bool { return m.startOf(comp[i]) < m.startOf(comp[j]) })
			comps = append(comps, comp)
		}
	}

	for _, n := range nodes {
		if _, seen := indices[n]; !seen {
			strongconnect(n)
		}
	}

	sort.Slice(comps, func(i, j int) bool {
		a, b := comps[i], comps[j]
		for k := 0; k < len(a) && k < len(b); k++ {
			if a[k] != b[k] {
				return m.startOf(a[k]) < m.startOf(b[k])
			}
		}
		return len(a) < len(b)
	})
	return comps
}

// rowState returns the sorted holders and the queue of one row.
func (m *Manager) rowState(name string) ([]Holder, []QueuedRequest) {
	r, ok := m.rows[name]
	if !ok {
		return nil, nil
	}
	var holders []Holder
	for id, mode := range r.holders {
		holders = append(holders, Holder{Txn: id, Mode: mode})
	}
	sort.Slice(holders, func(i, j int) bool {
		return m.startOf(holders[i].Txn) < m.startOf(holders[j].Txn)
	})
	var q []QueuedRequest
	for _, req := range r.queue {
		q = append(q, QueuedRequest{Txn: req.txn, Mode: req.mode, Upgrade: req.upgrade})
	}
	return holders, q
}

// Snapshot returns the holders and queue of every row in sorted row order.
func (m *Manager) Snapshot() []RowSnapshot {
	snaps := make([]RowSnapshot, 0, len(m.rows))
	for _, name := range m.rowNames() {
		holders, q := m.rowState(name)
		snaps = append(snaps, RowSnapshot{Row: name, Holders: holders, Queue: q})
	}
	return snaps
}
