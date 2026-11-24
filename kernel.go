package dataflow

import (
	"fmt"
	"sort"
	"sync"
)

// Driver is the step-wise dataflow execution kernel. It is safe for
// concurrent use: concurrent Step calls are linearized by an internal mutex,
// so goroutine interleavings correspond exactly to sequential interleavings.
//
// A Driver must be created with NewDriver and supports at most MaxOperators
// operators.
type Driver struct {
	mu        sync.Mutex
	graph     Graph
	progs     map[OpID][]Instr
	cfg       Config
	opOrder   []OpID
	edgeOrder []EdgeID

	pc      map[OpID]int
	handles map[OpID]map[Handle]BlockID
	queues  map[EdgeID][]BlockID
	blocks  map[BlockID]*blockState
	seq     int
	records []Record
}

type blockState struct {
	id      BlockID
	refs    int
	holders map[OpID]int
}

// NewDriver validates everything and creates a fresh kernel at the initial
// state. It returns *ProgramError for invalid specifications and when the
// graph has more than MaxOperators operators.
func NewDriver(g *Graph, programs map[OpID][]Instr, cfg Config) (*Driver, error) {
	return newDriver(g, programs, cfg, true)
}

// newDriver is the shared constructor. driverLimit=false is used by the
// state-space verifier, which analyzes "small graphs" without the step
// driver's five-operator cap.
func newDriver(g *Graph, programs map[OpID][]Instr, cfg Config, driverLimit bool) (*Driver, error) {
	ops, edgeOrder, err := validateRequest(g, programs, &cfg, driverLimit)
	if err != nil {
		return nil, err
	}
	progs := make(map[OpID][]Instr, len(programs))
	for op, p := range programs {
		progs[op] = append([]Instr(nil), p...)
	}
	d := &Driver{
		graph:     *g.clone(),
		progs:     progs,
		cfg:       cfg.clone(),
		opOrder:   append([]OpID(nil), ops...),
		edgeOrder: append([]EdgeID(nil), edgeOrder...),
		pc:        map[OpID]int{},
		handles:   map[OpID]map[Handle]BlockID{},
		queues:    map[EdgeID][]BlockID{},
		blocks:    map[BlockID]*blockState{},
	}
	for _, op := range d.opOrder {
		d.pc[op] = 0
		d.handles[op] = map[Handle]BlockID{}
	}
	for _, id := range d.edgeOrder {
		d.queues[id] = []BlockID{}
	}
	return d, nil
}

// Config returns a copy of the driver's buffer configuration.
func (d *Driver) Config() Config {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.cfg.clone()
}

// OperatorDone reports whether op's finite program is fully executed.
func (d *Driver) OperatorDone(op OpID) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.pc[op] >= len(d.progs[op])
}

// AllDone reports whether every operator finished.
func (d *Driver) AllDone() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.allDoneLocked()
}

func (d *Driver) allDoneLocked() bool {
	for _, op := range d.opOrder {
		if d.pc[op] < len(d.progs[op]) {
			return false
		}
	}
	return true
}

// PoolUsed returns the number of currently occupied shared-pool slots, i.e.
// the number of live physical blocks.
func (d *Driver) PoolUsed() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.blocks)
}

// RefCount returns the outstanding reference count of a block (0 if gone).
func (d *Driver) RefCount(b BlockID) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	if bs := d.blocks[b]; bs != nil {
		return bs.refs
	}
	return 0
}

// Holders returns a copy of the current operator-held reference multiplicity
// for a block. Queued edge references are not included.
func (d *Driver) Holders(b BlockID) map[OpID]int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.holdersLocked(b)
}

func (d *Driver) holdersLocked(b BlockID) map[OpID]int {
	out := map[OpID]int{}
	if bs := d.blocks[b]; bs != nil {
		for op, n := range bs.holders {
			if n > 0 {
				out[op] = n
			}
		}
	}
	return out
}

// Queue returns a copy (head first) of the block references currently queued
// on edge e.
func (d *Driver) Queue(e EdgeID) []BlockID {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]BlockID(nil), d.queues[e]...)
}

// EdgeUsed returns the number of occupied FIFO slots on edge e.
func (d *Driver) EdgeUsed(e EdgeID) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.queues[e])
}

// Trace returns a copy of the executed records so far.
func (d *Driver) Trace() []Record {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]Record(nil), d.records...)
}

// Action is an currently executable instruction of one operator.
type Action struct {
	Op         OpID  `json:"op"`
	InstrIndex int   `json:"instr_index"`
	Instr      Instr `json:"instr"`
}

// Enabled returns all currently executable actions in deterministic
// operator declaration order.
func (d *Driver) Enabled() []Action {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.enabledLocked()
}

func (d *Driver) enabledLocked() []Action {
	var out []Action
	for _, op := range d.opOrder {
		i := d.pc[op]
		if i >= len(d.progs[op]) {
			continue
		}
		in := d.progs[op][i]
		if waits := d.waitsLocked(op, i, in); len(waits) == 0 {
			out = append(out, Action{Op: op, InstrIndex: i, Instr: in})
		}
	}
	return out
}

// Blockers returns one report per unfinished operator whose next instruction
// is currently not executable, in operator declaration order.
func (d *Driver) Blockers() []Blocker {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.blockersLocked()
}

func (d *Driver) blockersLocked() []Blocker {
	var out []Blocker
	for _, op := range d.opOrder {
		i := d.pc[op]
		if i >= len(d.progs[op]) {
			continue
		}
		in := d.progs[op][i]
		if waits := d.waitsLocked(op, i, in); len(waits) > 0 {
			out = append(out, Blocker{Op: op, InstrIndex: i, Instr: in, Waits: waits})
		}
	}
	return out
}

// waitsLocked computes the unsatisfied resources of one instruction without
// mutating anything. A nil/empty result means the instruction is executable.
func (d *Driver) waitsLocked(op OpID, i int, in Instr) []Wait {
	switch in.Kind {
	case InstrAlloc:
		if len(d.blocks) >= d.cfg.Pool {
			return []Wait{{Resource: ResourcePoolSlot, Have: len(d.blocks), Cap: d.cfg.Pool, Need: 1}}
		}
		return nil
	case InstrTake:
		q := d.queues[in.Edge]
		if len(q) == 0 {
			return []Wait{{Resource: ResourceEdgeItem, Edge: in.Edge, Have: 0, Cap: d.cfg.edgeCap(in.Edge), Need: 1}}
		}
		return nil
	case InstrPublish:
		var waits []Wait
		for _, eid := range in.Targets {
			used := len(d.queues[eid])
			cap := d.cfg.edgeCap(eid)
			if used >= cap {
				waits = append(waits, Wait{Resource: ResourceEdgeSlot, Edge: eid, Have: used, Cap: cap, Need: 1})
			}
		}
		return waits
	case InstrRelease:
		if _, ok := d.handles[op][in.Handle]; !ok {
			// Defensive: validation prevents this in normal use. Represent it
			// as a permanent program error rather than a resource wait.
			return []Wait{{Resource: "program_error_unbound_handle"}}
		}
		return nil
	default:
		return []Wait{{Resource: "program_error_unknown_instruction"}}
	}
}

// Step executes op's next instruction if it is currently executable.
//
// If the instruction is blocked, no state changes and the returned error is
// *BlockedError (use AsBlocked or errors.As). If the operator is done,
// ErrOperatorDone is returned.
func (d *Driver) Step(op OpID) (*Record, error) {
	d.mu.Lock()
	defer d.mu.Unlock()

	if _, ok := d.pc[op]; !ok {
		return nil, &ProgramError{Op: op, Msg: "unknown operator"}
	}
	i := d.pc[op]
	if i >= len(d.progs[op]) {
		return nil, ErrOperatorDone
	}
	in := d.progs[op][i]
	waits := d.waitsLocked(op, i, in)
	if len(waits) > 0 {
		if waits[0].Resource == "program_error_unbound_handle" || waits[0].Resource == "program_error_unknown_instruction" {
			return nil, &ProgramError{Op: op, Msg: fmt.Sprintf("instr %d cannot execute: %s", i, waits[0].Resource)}
		}
		// Copy waits so callers cannot mutate internal slices.
		wc := append([]Wait(nil), waits...)
		return nil, &BlockedError{Op: op, InstrIndex: i, Instr: in, Waits: wc}
	}

	rec := d.applyLocked(op, i, in)
	d.pc[op] = i + 1
	d.seq++
	rec.Seq = d.seq
	d.records = append(d.records, rec)
	return &rec, nil
}

// StepAny executes the first currently enabled action in deterministic
// operator declaration order. It returns ErrNoEnabledAction (wrapped with a
// *Deadlock detail via DeadlockReport) when no action can run.
func (d *Driver) StepAny() (*Record, error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	enabled := d.enabledLocked()
	if len(enabled) == 0 {
		if d.allDoneLocked() {
			return nil, ErrOperatorDone
		}
		return nil, &NoEnabledActionError{Blockers: d.blockersLocked()}
	}
	a := enabled[0]
	in := d.progs[a.Op][a.InstrIndex]
	rec := d.applyLocked(a.Op, a.InstrIndex, in)
	d.pc[a.Op] = a.InstrIndex + 1
	d.seq++
	rec.Seq = d.seq
	d.records = append(d.records, rec)
	return &rec, nil
}

// NoEnabledActionError reports a no-executable-action state reached through
// direct driving. errors.Is(err, ErrNoEnabledAction) is true.
type NoEnabledActionError struct {
	Blockers []Blocker
}

func (e *NoEnabledActionError) Error() string {
	return fmt.Sprintf("dataflow: reached a state with no executable action; %d operator(s) blocked", len(e.Blockers))
}

// Unwrap exposes ErrNoEnabledAction for errors.Is.
func (e *NoEnabledActionError) Unwrap() error { return ErrNoEnabledAction }

// applyLocked performs the state transition. Caller holds d.mu and has
// already checked waitsLocked()==0.
func (d *Driver) applyLocked(op OpID, i int, in Instr) Record {
	rec := Record{Op: op, InstrIndex: i, Instr: in}

	switch in.Kind {
	case InstrAlloc:
		bid := BlockID{Origin: op, Index: i}
		bs := &blockState{id: bid, refs: 1, holders: map[OpID]int{op: 1}}
		d.blocks[bid] = bs
		d.handles[op][in.As] = bid
		rec.Event = Event{Kind: EventAllocated, Block: bid, PoolUsed: len(d.blocks), PoolCap: d.cfg.Pool}

	case InstrTake:
		q := d.queues[in.Edge]
		bid := q[0]
		q = q[1:]
		d.queues[in.Edge] = q // edge slot returned immediately
		bs := d.blocks[bid]
		// The queued reference becomes an operator-held reference: refs does
		// not change.
		bs.holders[op]++
		d.handles[op][in.Into] = bid
		rec.Event = Event{Kind: EventTaken, Block: bid, Edge: in.Edge, PoolUsed: len(d.blocks), PoolCap: d.cfg.Pool}

	case InstrPublish:
		hbid := d.handles[op][in.Handle]
		bs := d.blocks[hbid]
		targets := append([]EdgeID(nil), in.Targets...)
		// Deterministic enqueue order by edge number.
		sort.Slice(targets, func(a, b int) bool { return targets[a] < targets[b] })
		for _, eid := range targets {
			d.queues[eid] = append(d.queues[eid], hbid)
			bs.refs++
		}
		rec.Event = Event{Kind: EventPublished, Block: hbid, Targets: targets, PoolUsed: len(d.blocks), PoolCap: d.cfg.Pool}

	case InstrRelease:
		bid := d.handles[op][in.Handle]
		delete(d.handles[op], in.Handle)
		bs := d.blocks[bid]
		bs.holders[op]--
		if bs.holders[op] <= 0 {
			delete(bs.holders, op)
		}
		bs.refs--
		last := false
		if bs.refs == 0 {
			last = true
			delete(d.blocks, bid) // last reference: pool slot returned
		}
		rec.Event = Event{Kind: EventReleased, Block: bid, Last: last, PoolUsed: len(d.blocks), PoolCap: d.cfg.Pool}
	}
	return rec
}

// CheckInvariants validates the internal reference/accounting invariants.
// Tests call it after scripted and concurrent runs.
func (d *Driver) CheckInvariants() error {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.invariantLocked()
}

func (d *Driver) invariantLocked() error {
	if len(d.blocks) > d.cfg.Pool {
		return fmt.Errorf("dataflow invariant: pool overcommitted: %d > %d", len(d.blocks), d.cfg.Pool)
	}
	// Count queue references.
	queued := map[BlockID]int{}
	for _, eid := range d.edgeOrder {
		q := d.queues[eid]
		if len(q) > d.cfg.edgeCap(eid) {
			return fmt.Errorf("dataflow invariant: edge %d over capacity: %d > %d", eid, len(q), d.cfg.edgeCap(eid))
		}
		for _, b := range q {
			queued[b]++
		}
	}
	held := map[BlockID]int{}
	for op, hs := range d.handles {
		for h, b := range hs {
			if _, ok := d.blocks[b]; !ok {
				return fmt.Errorf("dataflow invariant: op %q handle %q points at missing block %s", op, h, b)
			}
			held[b]++
		}
	}
	all := map[BlockID]bool{}
	for b := range queued {
		all[b] = true
	}
	for b := range held {
		all[b] = true
	}
	for b := range all {
		bs := d.blocks[b]
		if bs == nil {
			return fmt.Errorf("dataflow invariant: live reference to freed block %s", b)
		}
		want := queued[b] + held[b]
		if bs.refs != want {
			return fmt.Errorf("dataflow invariant: block %s refs=%d but queued(%d)+held(%d)=%d", b, bs.refs, queued[b], held[b], want)
		}
		hm := 0
		for _, n := range bs.holders {
			hm += n
		}
		if hm != held[b] {
			return fmt.Errorf("dataflow invariant: block %s holder multiplicity %d != handle count %d", b, hm, held[b])
		}
		if bs.refs <= 0 {
			return fmt.Errorf("dataflow invariant: block %s non-positive refs while referenced", b)
		}
	}
	for b, bs := range d.blocks {
		if bs.refs <= 0 {
			return fmt.Errorf("dataflow invariant: resident block %s with refs=%d", b, bs.refs)
		}
	}
	return nil
}
