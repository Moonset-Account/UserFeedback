package shutdowncoord

import (
	"fmt"
	"sync"
	"time"
)

// Store persists coordinator snapshots so a restarted controller can resume
// without replaying already-confirmed actions.
type Store interface {
	// Load returns the last saved snapshot. ok is false when none exists.
	Load() (Snapshot, bool, error)
	// Save atomically replaces the stored snapshot.
	Save(Snapshot) error
}

// Snapshot is the complete durable state of the coordinator. It is a value
// type with exported fields so it can be encoded by any storage backend.
type Snapshot struct {
	Topology  Topology
	Grace     time.Duration
	Started   bool
	Finalized bool
	Deadline  time.Time

	// Order preserves the deterministic instance order from the topology.
	Order []ContainerID

	States []InstanceSnapshot

	// Issued is every action ever emitted, in emission order.
	Issued []Action
	// Acked holds the IDs of actions the executor durably confirmed.
	Acked []string

	Incomplete []EdgeFailure
}

// InstanceSnapshot is the persisted state of one instance.
type InstanceSnapshot struct {
	Container ContainerID
	State     InstanceState
}

// MemoryStore is an in-process Store. It is safe for concurrent use and is a
// convenient way to keep the snapshot alive across Coordinator restarts inside
// one process.
type MemoryStore struct {
	mu   sync.Mutex
	snap Snapshot
	ok   bool
}

// Load implements Store.
func (m *MemoryStore) Load() (Snapshot, bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.ok {
		return Snapshot{}, false, nil
	}
	return m.snap.clone(), true, nil
}

// Save implements Store.
func (m *MemoryStore) Save(s Snapshot) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.snap = s.clone()
	m.ok = true
	return nil
}

func (s Snapshot) clone() Snapshot {
	out := s
	out.Order = append([]ContainerID(nil), s.Order...)
	out.Issued = append([]Action(nil), s.Issued...)
	out.Acked = append([]string(nil), s.Acked...)
	out.Incomplete = append([]EdgeFailure(nil), s.Incomplete...)
	out.States = append([]InstanceSnapshot(nil), s.States...)
	out.Topology = Topology{
		Instances: append([]Instance(nil), s.Topology.Instances...),
		Edges:     append([]Edge(nil), s.Topology.Edges...),
	}
	return out
}

// saveLocked writes the current state. Callers hold c.mu.
func (c *Coordinator) saveLocked() {
	_ = c.store.Save(c.snapshotLocked())
}

func (c *Coordinator) snapshotLocked() Snapshot {
	snap := Snapshot{
		Topology:   c.topo,
		Grace:      c.grace,
		Started:    c.started,
		Finalized:  c.finalized,
		Deadline:   c.deadline,
		Order:      append([]ContainerID(nil), c.order...),
		Issued:     append([]Action(nil), c.issuedList...),
		Incomplete: append([]EdgeFailure(nil), c.incomplete...),
	}
	for _, id := range c.order {
		snap.States = append(snap.States, InstanceSnapshot{
			Container: id,
			State:     *c.states[id],
		})
	}
	for _, a := range c.issuedList {
		if c.acked[a.ID] {
			snap.Acked = append(snap.Acked, a.ID)
		}
	}
	return snap
}

// hydrate restores a coordinator from a snapshot produced by snapshotLocked.
// It re-validates the topology so a corrupted snapshot is rejected rather than
// producing inconsistent behavior.
func (c *Coordinator) hydrate(snap Snapshot) error {
	if err := validateTopology(snap.Topology); err != nil {
		return err
	}
	if snap.Grace <= 0 {
		return fmt.Errorf("snapshot has non-positive grace %s", snap.Grace)
	}
	if len(snap.Order) != len(snap.Topology.Instances) {
		return fmt.Errorf("snapshot order length %d != instance count %d",
			len(snap.Order), len(snap.Topology.Instances))
	}
	c.topo = snap.Topology
	c.grace = snap.Grace
	c.buildIndexes(snap.Topology.Instances, snap.Topology.Edges)
	seen := map[ContainerID]bool{}
	for _, is := range snap.States {
		if seen[is.Container] {
			return fmt.Errorf("snapshot has duplicate state for %q", is.Container)
		}
		spec, ok := c.spec[is.Container]
		if !ok {
			return fmt.Errorf("snapshot state for unknown container %q", is.Container)
		}
		if spec.Gen != is.State.Gen {
			return fmt.Errorf("snapshot state generation for %q is %d, topology says %d",
				is.Container, is.State.Gen, spec.Gen)
		}
		seen[is.Container] = true
		st := is.State
		c.states[is.Container] = &st
	}
	for _, id := range c.order {
		if !seen[id] {
			return fmt.Errorf("snapshot missing state for %q", id)
		}
	}
	c.started = snap.Started
	c.finalized = snap.Finalized
	c.deadline = snap.Deadline
	for _, a := range snap.Issued {
		if c.issued[a.ID] {
			continue
		}
		c.issued[a.ID] = true
		c.issuedList = append(c.issuedList, a)
	}
	for _, id := range snap.Acked {
		c.acked[id] = true
	}
	c.incomplete = append([]EdgeFailure(nil), snap.Incomplete...)
	return nil
}
