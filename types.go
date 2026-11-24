// Package dataflow implements a small dataflow execution kernel and a buffer
// configurator for tiny operator graphs (at most five operators for the
// step-wise driver).
//
// # Execution model
//
// Operators run straight-line, finite instruction sequences:
//
//   - InstrTake ("取块") pops the oldest reference from an input edge FIFO.
//     The edge slot is returned immediately, but the physical block keeps
//     occupying one shared-pool slot until its last reference is released.
//   - InstrAlloc ("申请输出槽") obtains one physical output block from the
//     shared pool and binds it to a local handle.
//   - InstrPublish ("发布给多个消费者") atomically obtains one free slot on
//     every target edge and enqueues one reference to the same block on each.
//     If any target edge is full, nothing is acquired and nothing is
//     enqueued (all-or-nothing fanout).
//   - InstrRelease ("释放先前的引用") drops a handle/reference; when the
//     last reference goes away the pool slot is returned.
//
// Scheduling is nondeterministic at operator granularity: every interleaving
// of the enabled next instructions is a legal schedule. Verify and
// Configure exhaustively explore these interleavings.
package dataflow

import (
	"fmt"
	"strconv"
	"strings"
)

// MaxOperators is the hard cap enforced by the step-wise driver.
const MaxOperators = 5

// OpID identifies an operator ("算子").
type OpID string

// EdgeID is the number of an edge ("边号"). Smaller numbers win the
// lexicographic tie-break.
type EdgeID int

// Handle is an operator-local name for a currently held block reference.
type Handle string

// BlockID uniquely identifies a physical block. Because programs are
// straight-line, an Alloc instruction executes at most once, so the
// allocation origin (operator + instruction index) is a canonical identity
// that stays stable across interleavings and replays.
type BlockID struct {
	// Origin is the operator whose Alloc instruction created the block.
	Origin OpID `json:"origin"`
	// Index is the instruction index (within Origin's program) of the Alloc.
	Index int `json:"index"`
}

// String renders "op#index".
func (b BlockID) String() string {
	return string(b.Origin) + "#" + strconv.Itoa(b.Index)
}

// MarshalText implements encoding.TextMarshaler.
func (b BlockID) MarshalText() ([]byte, error) {
	return []byte(b.String()), nil
}

// UnmarshalText implements encoding.TextUnmarshaler.
func (b *BlockID) UnmarshalText(text []byte) error {
	s := string(text)
	i := strings.LastIndex(s, "#")
	if i < 0 {
		return fmt.Errorf("dataflow: invalid block id %q", s)
	}
	n, err := strconv.Atoi(s[i+1:])
	if err != nil {
		return fmt.Errorf("dataflow: invalid block id %q: %w", s, err)
	}
	b.Origin = OpID(s[:i])
	b.Index = n
	return nil
}

// Edge is a numbered FIFO connection From a producer operator To a consumer.
type Edge struct {
	ID   EdgeID `json:"id"`
	From OpID   `json:"from"`
	To   OpID   `json:"to"`
}

// Graph is a small directed operator graph. Edges are explicitly numbered;
// declaration order does not matter for edge-number ordering.
type Graph struct {
	Ops   []OpID `json:"ops"`
	Edges []Edge `json:"edges"`
}

func (g *Graph) clone() *Graph {
	if g == nil {
		return nil
	}
	cp := &Graph{Ops: append([]OpID(nil), g.Ops...), Edges: append([]Edge(nil), g.Edges...)}
	return cp
}

func (g *Graph) edgeByID(id EdgeID) (Edge, bool) {
	for _, e := range g.Edges {
		if e.ID == id {
			return e, true
		}
	}
	return Edge{}, false
}

// InstrKind enumerates the four instruction kinds.
type InstrKind string

const (
	// InstrTake is "取块": pop a block reference from Edge into handle Into.
	InstrTake InstrKind = "take"
	// InstrAlloc is "申请输出槽": take a shared-pool slot for a fresh block
	// and bind it to handle As.
	InstrAlloc InstrKind = "alloc"
	// InstrPublish is "发布给多个消费者": atomically acquire one slot on
	// every Targets edge and publish Handle's block to all of them.
	InstrPublish InstrKind = "publish"
	// InstrRelease is "释放先前的引用": release handle Handle.
	InstrRelease InstrKind = "release"
)

// Instr is one finite instruction. Fields are used according to Kind.
type Instr struct {
	Kind    InstrKind `json:"kind"`
	Edge    EdgeID    `json:"edge,omitempty"`    // Take
	Into    Handle    `json:"into,omitempty"`    // Take
	As      Handle    `json:"as,omitempty"`      // Alloc
	Handle  Handle    `json:"handle,omitempty"`  // Publish / Release
	Targets []EdgeID  `json:"targets,omitempty"` // Publish
}

// TakeInstr builds a Take instruction (取块).
func TakeInstr(edge EdgeID, into Handle) Instr {
	return Instr{Kind: InstrTake, Edge: edge, Into: into}
}

// AllocInstr builds an Alloc instruction (申请输出槽 / pool block).
func AllocInstr(as Handle) Instr {
	return Instr{Kind: InstrAlloc, As: as}
}

// PublishInstr builds an atomic fanout Publish instruction.
func PublishInstr(handle Handle, targets ...EdgeID) Instr {
	return Instr{Kind: InstrPublish, Handle: handle, Targets: append([]EdgeID(nil), targets...)}
}

// ReleaseInstr builds a Release instruction (释放先前的引用).
func ReleaseInstr(handle Handle) Instr {
	return Instr{Kind: InstrRelease, Handle: handle}
}

// Config is one buffer configuration: shared-pool capacity and per-edge FIFO
// capacities.
type Config struct {
	Pool     int            `json:"pool"`
	EdgeCaps map[EdgeID]int `json:"edge_caps"`
}

func (c Config) edgeCap(id EdgeID) int {
	if c.EdgeCaps == nil {
		return 0
	}
	return c.EdgeCaps[id]
}

func (c Config) clone() Config {
	m := make(map[EdgeID]int, len(c.EdgeCaps))
	for k, v := range c.EdgeCaps {
		m[k] = v
	}
	return Config{Pool: c.Pool, EdgeCaps: m}
}

// EventKind classifies executed steps.
type EventKind string

const (
	EventAllocated EventKind = "allocated"
	EventTaken     EventKind = "taken"
	EventPublished EventKind = "published"
	EventReleased  EventKind = "released"
)

// Event describes the observable effect of one executed instruction.
type Event struct {
	Kind    EventKind `json:"kind"`
	Block   BlockID   `json:"block"`
	Edge    EdgeID    `json:"edge,omitempty"`
	Targets []EdgeID  `json:"targets,omitempty"`
	// Last is set by Release when this dropped reference was the final one,
	// so the shared-pool slot has just been returned.
	Last bool `json:"last,omitempty"`
	// PoolUsed / PoolCap report pool occupancy after the step.
	PoolUsed int `json:"pool_used"`
	PoolCap  int `json:"pool_cap"`
}

// Record is one entry of an execution trace ("轨迹").
type Record struct {
	Seq        int   `json:"seq"`
	Op         OpID  `json:"op"`
	InstrIndex int   `json:"instr_index"`
	Instr      Instr `json:"instr"`
	Event      Event `json:"event"`
}

// ResourceKind identifies what a blocked instruction is waiting for.
type ResourceKind string

const (
	// ResourcePoolSlot: waiting for a free shared-pool physical-block slot.
	ResourcePoolSlot ResourceKind = "pool_slot"
	// ResourceEdgeSlot: a target FIFO edge is full; waiting for the consumer
	// to take a reference so one edge slot returns.
	ResourceEdgeSlot ResourceKind = "edge_slot"
	// ResourceEdgeItem: an input edge is empty; waiting for its producer.
	ResourceEdgeItem ResourceKind = "edge_item"
)

// Wait is one concrete unsatisfied resource requirement. A fanout publish
// blocked by several full target edges reports one Wait per full edge.
type Wait struct {
	Resource ResourceKind `json:"resource"`
	// Edge is set for edge_slot / edge_item waits.
	Edge EdgeID `json:"edge,omitempty"`
	// Have and Cap describe current occupancy of the resource.
	Have int `json:"have"`
	Cap  int `json:"cap"`
	// Need is how many units the instruction still needs (always 1 per edge
	// in this model).
	Need int `json:"need"`
}

// Blocker describes one operator whose next instruction cannot execute.
type Blocker struct {
	Op         OpID   `json:"op"`
	InstrIndex int    `json:"instr_index"`
	Instr      Instr  `json:"instr"`
	Waits      []Wait `json:"waits"`
}
