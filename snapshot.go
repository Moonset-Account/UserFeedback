package dataflow

import (
	"fmt"
	"sort"
	"strings"
)

// BlockSnapshot is the serializable ownership/refcount view of one live
// physical block.
type BlockSnapshot struct {
	ID      BlockID      `json:"id"`
	Refs    int          `json:"refs"`
	Holders map[OpID]int `json:"holders"`
}

// Snapshot is a fully serializable kernel state. It is enough to restore
// every holder of every physical block from the middle of a trace.
type Snapshot struct {
	Config    Config                      `json:"config"`
	PC        map[OpID]int                `json:"pc"`
	Handles   map[OpID]map[Handle]BlockID `json:"handles"`
	Queues    map[EdgeID][]BlockID        `json:"queues"`
	Blocks    []BlockSnapshot             `json:"blocks"`
	StepCount int                         `json:"step_count"`
}

// Export returns a deep-copied, JSON-friendly snapshot of the current state.
func (d *Driver) Export() Snapshot {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.exportLocked()
}

func (d *Driver) exportLocked() Snapshot {
	s := Snapshot{
		Config:    d.cfg.clone(),
		PC:        map[OpID]int{},
		Handles:   map[OpID]map[Handle]BlockID{},
		Queues:    map[EdgeID][]BlockID{},
		StepCount: d.seq,
	}
	for _, op := range d.opOrder {
		s.PC[op] = d.pc[op]
		hm := map[Handle]BlockID{}
		for h, b := range d.handles[op] {
			hm[h] = b
		}
		s.Handles[op] = hm
	}
	for _, eid := range d.edgeOrder {
		s.Queues[eid] = append([]BlockID(nil), d.queues[eid]...)
	}
	ids := make([]BlockID, 0, len(d.blocks))
	for id := range d.blocks {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool {
		if ids[i].Origin != ids[j].Origin {
			return ids[i].Origin < ids[j].Origin
		}
		return ids[i].Index < ids[j].Index
	})
	for _, id := range ids {
		bs := d.blocks[id]
		bsc := BlockSnapshot{ID: id, Refs: bs.refs, Holders: map[OpID]int{}}
		for op, n := range bs.holders {
			if n > 0 {
				bsc.Holders[op] = n
			}
		}
		s.Blocks = append(s.Blocks, bsc)
	}
	return s
}

// NewFromSnapshot rebuilds a step-driver from a snapshot produced for the
// same graph/programs. It enforces the MaxOperators driver cap and validates
// the snapshot's accounting invariants.
func NewFromSnapshot(g *Graph, programs map[OpID][]Instr, snap Snapshot) (*Driver, error) {
	return newFromSnapshot(g, programs, snap, true)
}

func newFromSnapshot(g *Graph, programs map[OpID][]Instr, snap Snapshot, driverLimit bool) (*Driver, error) {
	d, err := newDriver(g, programs, snap.Config, driverLimit)
	if err != nil {
		return nil, err
	}
	// Restore PCs/handles/queues after structural validation.
	for _, op := range d.opOrder {
		pc, ok := snap.PC[op]
		if !ok {
			return nil, &ProgramError{Op: op, Msg: "snapshot missing pc"}
		}
		if pc < 0 || pc > len(d.progs[op]) {
			return nil, &ProgramError{Op: op, Msg: fmt.Sprintf("snapshot pc %d out of range [0,%d]", pc, len(d.progs[op]))}
		}
		d.pc[op] = pc
		if hm, ok := snap.Handles[op]; ok {
			for h, b := range hm {
				d.handles[op][h] = b
			}
		}
	}
	for _, eid := range d.edgeOrder {
		q, ok := snap.Queues[eid]
		if !ok {
			return nil, &ProgramError{Msg: fmt.Sprintf("snapshot missing queue for edge %d", eid)}
		}
		d.queues[eid] = append([]BlockID(nil), q...)
	}
	for _, bs := range snap.Blocks {
		if _, dup := d.blocks[bs.ID]; dup {
			return nil, &ProgramError{Msg: fmt.Sprintf("snapshot duplicate block %s", bs.ID)}
		}
		holders := map[OpID]int{}
		for op, n := range bs.Holders {
			if n <= 0 {
				continue
			}
			if _, ok := d.pc[op]; !ok {
				return nil, &ProgramError{Op: op, Msg: "snapshot holder unknown operator"}
			}
			holders[op] = n
		}
		d.blocks[bs.ID] = &blockState{id: bs.ID, refs: bs.Refs, holders: holders}
	}
	d.seq = snap.StepCount
	if err := d.invariantLocked(); err != nil {
		return nil, fmt.Errorf("dataflow: refusing invalid snapshot: %w", err)
	}
	return d, nil
}

// Restore replaces this driver's state with snap. Graph and programs must
// match; the snapshot configuration is applied as well.
func (d *Driver) Restore(snap Snapshot) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	fresh, err := NewFromSnapshot(&d.graph, d.progs, snap)
	if err != nil {
		return err
	}
	fresh.mu.Lock()
	defer fresh.mu.Unlock()
	d.cfg = fresh.cfg
	d.pc = fresh.pc
	d.handles = fresh.handles
	d.queues = fresh.queues
	d.blocks = fresh.blocks
	d.seq = fresh.seq
	d.records = nil // restored point has no in-memory suffix records
	return nil
}

// Replay rebuilds a driver by executing the first upto records of trace
// (upto == len(trace), or a negative value, replays all of them). Each
// record names the operator to step; its instruction index and resulting
// block are checked against the re-executed step. This reconstructs all
// holders from the middle of a trajectory.
func Replay(g *Graph, programs map[OpID][]Instr, cfg Config, trace []Record, upto int) (*Driver, error) {
	if upto < 0 || upto > len(trace) {
		upto = len(trace)
	}
	d, err := NewDriver(g, programs, cfg)
	if err != nil {
		return nil, err
	}
	for k := 0; k < upto; k++ {
		want := trace[k]
		rec, err := d.Step(want.Op)
		if err != nil {
			return nil, fmt.Errorf("dataflow: replay of trace record %d (op %q) failed: %w", k, want.Op, err)
		}
		if rec.InstrIndex != want.InstrIndex {
			return nil, fmt.Errorf("dataflow: trace record %d mismatch: instr index %d != expected %d", k, rec.InstrIndex, want.InstrIndex)
		}
		if rec.Event.Kind != want.Event.Kind || rec.Event.Block != want.Event.Block {
			return nil, fmt.Errorf("dataflow: trace record %d event mismatch: got %s/%s want %s/%s",
				k, rec.Event.Kind, rec.Event.Block, want.Event.Kind, want.Event.Block)
		}
	}
	return d, nil
}

// CanonicalKey serializes the scheduling-relevant state. Block identities
// are allocation origins, which are invariant across interleavings, and all
// maps are emitted in deterministic order. The key covers PCs, held handles
// and FIFO queues; refcounts/holders/pool occupancy are derived from those.
func (s Snapshot) CanonicalKey(edgeOrder []EdgeID) string {
	var b strings.Builder
	ops := make([]OpID, 0, len(s.PC))
	for op := range s.PC {
		ops = append(ops, op)
	}
	sort.Slice(ops, func(i, j int) bool { return ops[i] < ops[j] })
	for _, op := range ops {
		fmt.Fprintf(&b, "op=%s pc=%d", op, s.PC[op])
		hs := s.Handles[op]
		names := make([]Handle, 0, len(hs))
		for h := range hs {
			names = append(names, h)
		}
		sort.Slice(names, func(i, j int) bool { return names[i] < names[j] })
		for _, h := range names {
			fmt.Fprintf(&b, " h:%s=%s", h, hs[h])
		}
		b.WriteByte(';')
	}
	for _, eid := range edgeOrder {
		fmt.Fprintf(&b, "edge=%d q=", eid)
		q := s.Queues[eid]
		for i, blk := range q {
			if i > 0 {
				b.WriteByte(',')
			}
			b.WriteString(blk.String())
		}
		b.WriteByte(';')
	}
	return b.String()
}
