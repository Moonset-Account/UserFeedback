// Package forwarder implements a fixed-size bucket hash ring that forwards
// new connections to next hops, plus a bounded-round rebalancing module that
// reacts to weight, health and drain changes.
//
// Buckets are the unit of placement: every bucket permanently names one next
// hop.  A connection is bound to a bucket once, at connect time, and keeps
// that next hop until it is closed or its next hop fails.  Rebalancing only
// moves buckets, therefore it only affects connections created afterwards.
package forwarder

import (
	"errors"
	"fmt"
	"hash/fnv"
	"sort"
	"sync"
)

// Health is the operational state of a next hop.
type Health int

const (
	// Ready next hops accept new connections and may own bucket quota.
	Ready Health = iota
	// Draining next hops keep their existing connections but accept no new
	// ones.  Their effective weight, and therefore their quota, is zero.
	Draining
	// Failed next hops keep no connections: active connections are rerouted
	// on the next round.  Their effective weight and quota are zero.
	Failed
)

func (h Health) String() string {
	switch h {
	case Ready:
		return "ready"
	case Draining:
		return "draining"
	case Failed:
		return "failed"
	default:
		return fmt.Sprintf("health(%d)", int(h))
	}
}

func (h Health) valid() bool { return h >= Ready && h <= Failed }

// Node describes a next hop and its desired weight.  Weight is ignored while
// the node is draining or failed, but is retained so the node can recover.
type Node struct {
	ID     string
	Weight int
	Health Health
}

// Config controls the ring size and the per-round bucket move budget.
type Config struct {
	Buckets          int
	MaxMovesPerRound int
}

// Errors returned by the module.  They are wrapped with fmt.Errorf, use
// errors.Is to test them.
var (
	ErrInvalidConfig    = errors.New("forwarder: invalid config")
	ErrInvalidNode      = errors.New("forwarder: invalid node")
	ErrInvalidHealth    = errors.New("forwarder: invalid health")
	ErrInvalidWeight    = errors.New("forwarder: invalid weight")
	ErrDuplicateNode    = errors.New("forwarder: duplicate node")
	ErrUnknownNode      = errors.New("forwarder: unknown node")
	ErrNoEligibleTarget = errors.New("forwarder: no ready node with positive weight")
	ErrNoReadyTarget    = errors.New("forwarder: no ready target")
	ErrInvalidConnID    = errors.New("forwarder: invalid connection id")
	ErrConnectionExists = errors.New("forwarder: connection already exists")
)

// Move is one bucket reassignment performed in a round.
type Move struct {
	Bucket int
	From   string
	To     string
}

// Reroute records a connection that was moved off a failed next hop in a
// round.  Stranded means no ready bucket existed; Bucket is -1 in that case.
type Reroute struct {
	ConnID   string
	From     string
	To       string
	Bucket   int
	Stranded bool
}

// NodeStat is the per-node view of a round.
type NodeStat struct {
	ID              string
	Health          Health
	Weight          int // configured weight
	EffectiveWeight int // weight used for quotas (0 unless Ready)
	Quota           int // integer target bucket count
	Buckets         int // buckets currently pointing at this node
	Connections     int // active connections currently routed here
	Difference      int // Buckets - Quota
}

// Round is the result of one Rebalance call.
type Round struct {
	Number    int
	Moves     []Move
	Rerouted  []Reroute
	Buckets   []string // bucket index -> target ID, after the round
	Nodes     []NodeStat
	Deviation int  // sum over nodes of |Buckets - Quota|
	Stalled   bool // deviation remains but no legal move exists
}

// ConnInfo is an immutable snapshot of an active connection.
type ConnInfo struct {
	ID     string
	Bucket int
	Target string
}

// Snapshot is a consistent view of the ring.
type Snapshot struct {
	Round       int
	Buckets     []string
	Nodes       []NodeStat
	Connections []ConnInfo
}

type Ring struct {
	mu       sync.Mutex
	n        int
	maxMoves int
	nodes    map[string]Node
	buckets  []string // len(buckets) == n
	conns    map[string]*Conn
	round    int
}

// New builds a ring and performs the initial layout.  The initial layout is
// unbounded by definition (there is nothing to move yet); only later rounds
// honour MaxMovesPerRound.  At least one Ready node with positive weight is
// required, otherwise buckets could not be placed.
func New(cfg Config, nodes []Node) (*Ring, error) {
	if cfg.Buckets <= 0 {
		return nil, fmt.Errorf("%w: Buckets must be > 0, got %d", ErrInvalidConfig, cfg.Buckets)
	}
	if cfg.MaxMovesPerRound <= 0 {
		return nil, fmt.Errorf("%w: MaxMovesPerRound must be > 0, got %d", ErrInvalidConfig, cfg.MaxMovesPerRound)
	}
	if len(nodes) == 0 {
		return nil, fmt.Errorf("%w: empty node set", ErrInvalidConfig)
	}

	state := make(map[string]Node, len(nodes))
	for _, n := range nodes {
		if err := validateNode(n); err != nil {
			return nil, err
		}
		if _, dup := state[n.ID]; dup {
			return nil, fmt.Errorf("%w: %q", ErrDuplicateNode, n.ID)
		}
		state[n.ID] = n
	}

	quota := computeQuotas(state, cfg.Buckets)
	assigned := 0
	for _, q := range quota {
		assigned += q
	}
	if assigned == 0 {
		return nil, ErrNoEligibleTarget
	}

	r := &Ring{
		n:        cfg.Buckets,
		maxMoves: cfg.MaxMovesPerRound,
		nodes:    state,
		buckets:  make([]string, cfg.Buckets),
		conns:    make(map[string]*Conn),
	}

	// Deterministic bootstrap layout: node IDs ascending, each node receives
	// one contiguous run of buckets equal to its quota.
	next := 0
	for _, id := range sortedNodeIDs(state) {
		for k := 0; k < quota[id]; k++ {
			r.buckets[next] = id
			next++
		}
	}
	if next != cfg.Buckets {
		return nil, fmt.Errorf("%w: internal layout mismatch (%d != %d)", ErrInvalidConfig, next, cfg.Buckets)
	}
	return r, nil
}

func validateNode(n Node) error {
	if n.ID == "" {
		return fmt.Errorf("%w: empty ID", ErrInvalidNode)
	}
	if !n.Health.valid() {
		return fmt.Errorf("%w: %s %s", ErrInvalidHealth, n.ID, n.Health)
	}
	if n.Weight < 0 {
		return fmt.Errorf("%w: %s weight %d", ErrInvalidWeight, n.ID, n.Weight)
	}
	return nil
}

// AddNode registers a new next hop.  Buckets move towards its quota over the
// following rounds.
func (r *Ring) AddNode(n Node) error {
	if err := validateNode(n); err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, exists := r.nodes[n.ID]; exists {
		return fmt.Errorf("%w: %q", ErrDuplicateNode, n.ID)
	}
	r.nodes[n.ID] = n
	return nil
}

// SetWeight changes a node's configured weight.
func (r *Ring) SetWeight(id string, weight int) error {
	if weight < 0 {
		return fmt.Errorf("%w: %s weight %d", ErrInvalidWeight, id, weight)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	n, ok := r.nodes[id]
	if !ok {
		return fmt.Errorf("%w: %q", ErrUnknownNode, id)
	}
	n.Weight = weight
	r.nodes[id] = n
	return nil
}

// SetHealth changes a node's health.  Draining and failed nodes immediately
// stop receiving new connections.  Failed nodes' active connections are
// rerouted by the next Rebalance.
func (r *Ring) SetHealth(id string, h Health) error {
	if !h.valid() {
		return fmt.Errorf("%w: %s %s", ErrInvalidHealth, id, h)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	n, ok := r.nodes[id]
	if !ok {
		return fmt.Errorf("%w: %q", ErrUnknownNode, id)
	}
	n.Health = h
	r.nodes[id] = n
	return nil
}

// BucketFor returns the preferred bucket for a connection ID.  It is a pure
// function of the ID and the fixed bucket count.
func (r *Ring) BucketFor(id string) int {
	return bucketIndex(id, r.n)
}

func bucketIndex(id string, n int) int {
	h := fnv.New64a()
	_, _ = h.Write([]byte(id))
	return int(h.Sum64() % uint64(n))
}

// Connect binds a connection to the first Ready bucket at or after its
// preferred bucket, scanning modulo the fixed bucket count.  Buckets that
// still point at draining or failed nodes are skipped until they are moved.
func (r *Ring) Connect(id string) (*Conn, error) {
	if id == "" {
		return nil, ErrInvalidConnID
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, exists := r.conns[id]; exists {
		return nil, fmt.Errorf("%w: %q", ErrConnectionExists, id)
	}
	b, target, ok := r.findReadyBucketLocked(bucketIndex(id, r.n))
	if !ok {
		return nil, fmt.Errorf("%w: connection %q", ErrNoReadyTarget, id)
	}
	c := &Conn{id: id, bucket: b, target: target, ring: r}
	r.conns[id] = c
	return c, nil
}

// Conn returns the current handle for an active connection.
func (r *Ring) Conn(id string) (*Conn, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	c, ok := r.conns[id]
	return c, ok
}

// Connections returns active connections sorted by ID.
func (r *Ring) Connections() []ConnInfo {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.connInfosLocked()
}

// Snapshot returns the current bucket map, statistics and connections.
func (r *Ring) Snapshot() Snapshot {
	r.mu.Lock()
	defer r.mu.Unlock()
	quota := computeQuotas(r.nodes, r.n)
	return Snapshot{
		Round:       r.round,
		Buckets:     append([]string(nil), r.buckets...),
		Nodes:       r.statsLocked(quota),
		Connections: r.connInfosLocked(),
	}
}

// Rebalance runs one round:
//
//  1. reroute active connections sitting on failed next hops, using only
//     buckets whose target is Ready (listed separately in Round.Rerouted);
//  2. move at most MaxMovesPerRound buckets towards the integer quotas.
//
// Rerouting happens before bucket moves so every rerouted connection is
// placed according to the bucket map that was live when the failure was
// detected; the moves made in the same round only apply to connections
// created afterwards.
//
// Within a round each bucket move is chosen to minimise the sum of
// |Buckets - Quota| across all nodes.  Every legal move goes from an
// over-quota node to an under-quota node and lowers that sum by exactly two,
// so one move is always the minimum; ties are broken by lower bucket index,
// then by lexicographically smaller destination ID.
func (r *Ring) Rebalance() Round {
	r.mu.Lock()
	defer r.mu.Unlock()

	r.round++
	round := Round{Number: r.round, Rerouted: r.rerouteFailedLocked()}

	quota := computeQuotas(r.nodes, r.n)
	counts := r.bucketCountsLocked()
	stalled := false

	for len(round.Moves) < r.maxMoves {
		mv, ok := r.nextMoveLocked(quota, counts)
		if !ok {
			stalled = true
			break
		}
		r.buckets[mv.Bucket] = mv.To
		counts[mv.From]--
		counts[mv.To]++
		round.Moves = append(round.Moves, mv)
	}

	round.Deviation = deviation(counts, quota)
	round.Stalled = stalled && round.Deviation > 0
	round.Buckets = append([]string(nil), r.buckets...)
	round.Nodes = r.statsLocked(quota)
	return round
}

func (r *Ring) nextMoveLocked(quota, counts map[string]int) (Move, bool) {
	current := deviation(counts, quota)
	if current == 0 {
		return Move{}, false
	}
	ids := sortedNodeIDs(r.nodes)
	var best Move
	bestDev := 0
	found := false

	for b := 0; b < r.n; b++ {
		from := r.buckets[b]
		if counts[from] <= quota[from] {
			continue // only over-quota nodes have buckets to give away
		}
		for _, to := range ids {
			if to == from {
				continue
			}
			if r.nodes[to].Health != Ready {
				continue // never move a bucket onto a draining/failed node
			}
			if counts[to] >= quota[to] {
				continue // would overshoot the destination quota
			}
			counts[from]--
			counts[to]++
			d := deviation(counts, quota)
			counts[from]++
			counts[to]--

			if d >= current {
				continue // only strictly improving moves
			}
			if !found || d < bestDev ||
				(d == bestDev && (b < best.Bucket || (b == best.Bucket && to < best.To))) {
				best = Move{Bucket: b, From: from, To: to}
				bestDev = d
				found = true
			}
		}
	}
	return best, found
}

// rerouteFailedLocked moves every active connection whose current target is
// Failed onto the first Ready bucket starting from its preferred bucket.  The
// old handle is invalidated by replacing it in the connection map; closing it
// later is a no-op and cannot decrement the new target's counter.
func (r *Ring) rerouteFailedLocked() []Reroute {
	var ids []string
	for id, c := range r.conns {
		if r.nodes[c.target].Health == Failed {
			ids = append(ids, id)
		}
	}
	if len(ids) == 0 {
		return nil
	}
	sort.Strings(ids)

	out := make([]Reroute, 0, len(ids))
	for _, id := range ids {
		old := r.conns[id]
		b, target, ok := r.findReadyBucketLocked(bucketIndex(id, r.n))
		if !ok {
			out = append(out, Reroute{ConnID: id, From: old.target, Bucket: -1, Stranded: true})
			continue
		}
		old.markStale()
		r.conns[id] = &Conn{id: id, bucket: b, target: target, ring: r}
		out = append(out, Reroute{ConnID: id, From: old.target, To: target, Bucket: b})
	}
	return out
}

func (r *Ring) findReadyBucketLocked(start int) (int, string, bool) {
	for i := 0; i < r.n; i++ {
		b := (start + i) % r.n
		target := r.buckets[b]
		if target == "" {
			continue
		}
		if n, ok := r.nodes[target]; ok && n.Health == Ready {
			return b, target, true
		}
	}
	return 0, "", false
}

func (r *Ring) bucketCountsLocked() map[string]int {
	counts := make(map[string]int, len(r.nodes))
	for _, target := range r.buckets {
		counts[target]++
	}
	return counts
}

func (r *Ring) connCountsLocked() map[string]int {
	counts := make(map[string]int, len(r.nodes))
	for _, c := range r.conns {
		counts[c.target]++
	}
	return counts
}

func (r *Ring) connInfosLocked() []ConnInfo {
	ids := make([]string, 0, len(r.conns))
	for id := range r.conns {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]ConnInfo, 0, len(ids))
	for _, id := range ids {
		c := r.conns[id]
		out = append(out, ConnInfo{ID: id, Bucket: c.bucket, Target: c.target})
	}
	return out
}

func (r *Ring) statsLocked(quota map[string]int) []NodeStat {
	buckets := r.bucketCountsLocked()
	conns := r.connCountsLocked()
	ids := sortedNodeIDs(r.nodes)
	out := make([]NodeStat, 0, len(ids))
	for _, id := range ids {
		n := r.nodes[id]
		eff := 0
		if n.Health == Ready {
			eff = n.Weight
		}
		out = append(out, NodeStat{
			ID:              id,
			Health:          n.Health,
			Weight:          n.Weight,
			EffectiveWeight: eff,
			Quota:           quota[id],
			Buckets:         buckets[id],
			Connections:     conns[id],
			Difference:      buckets[id] - quota[id],
		})
	}
	return out
}

// computeQuotas distributes the fixed bucket count proportionally to the
// effective weights using the largest-remainder (Hamilton) method.  Exact
// remainders are compared as integers, and ties are broken by ascending node
// ID, so the result is fully deterministic.
func computeQuotas(nodes map[string]Node, buckets int) map[string]int {
	quota := make(map[string]int, len(nodes))
	var totalWeight int64
	for _, n := range nodes {
		quota[n.ID] = 0
		if n.Health == Ready {
			totalWeight += int64(n.Weight)
		}
	}
	if totalWeight == 0 {
		return quota
	}

	type remainder struct {
		id  string
		rem int64
	}
	var rems []remainder
	assigned := 0
	for _, n := range nodes {
		if n.Health != Ready || n.Weight == 0 {
			continue
		}
		num := int64(buckets) * int64(n.Weight)
		q := int(num / totalWeight)
		quota[n.ID] = q
		assigned += q
		if rem := num % totalWeight; rem != 0 {
			rems = append(rems, remainder{id: n.ID, rem: rem})
		}
	}

	leftover := buckets - assigned
	sort.Slice(rems, func(i, j int) bool {
		if rems[i].rem != rems[j].rem {
			return rems[i].rem > rems[j].rem
		}
		return rems[i].id < rems[j].id
	})
	for i := 0; i < leftover && i < len(rems); i++ {
		quota[rems[i].id]++
	}
	return quota
}

func deviation(counts, quota map[string]int) int {
	total := 0
	for id, q := range quota {
		d := counts[id] - q
		if d < 0 {
			d = -d
		}
		total += d
	}
	return total
}

func sortedNodeIDs(nodes map[string]Node) []string {
	ids := make([]string, 0, len(nodes))
	for id := range nodes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}
