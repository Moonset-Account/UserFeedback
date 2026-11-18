package bootmeasure

import "sync"

// NodeStateStore persists the highest firmware generation a node has ever
// been confirmed at. Implementations must be safe for concurrent use.
type NodeStateStore interface {
	// HighestGeneration returns the highest confirmed generation for the
	// node. A node that was never confirmed returns (0, nil).
	HighestGeneration(nodeID string) (uint64, error)

	// CommitGeneration records that the node successfully booted at gen.
	// It must be monotonic: committing a generation lower than the stored
	// one must leave the stored value untouched and return
	// ErrRollbackAttempt.
	CommitGeneration(nodeID string, gen uint64) error
}

// ErrRollbackAttempt is returned by NodeStateStore.CommitGeneration when a
// caller tries to confirm a generation below the stored one.
var ErrRollbackAttempt = stateError("generation commit would roll node state backwards")

type stateError string

func (e stateError) Error() string { return string(e) }

// MemoryNodeStateStore is an in-memory NodeStateStore suitable for single
// processes and tests. Production deployments can replace it with a durable
// implementation of the same interface.
type MemoryNodeStateStore struct {
	mu          sync.RWMutex
	generations map[string]uint64
}

// NewMemoryNodeStateStore builds an empty in-memory node-state store.
func NewMemoryNodeStateStore() *MemoryNodeStateStore {
	return &MemoryNodeStateStore{generations: make(map[string]uint64)}
}

// HighestGeneration implements NodeStateStore.
func (s *MemoryNodeStateStore) HighestGeneration(nodeID string) (uint64, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.generations[nodeID], nil
}

// CommitGeneration implements NodeStateStore with monotonic enforcement.
func (s *MemoryNodeStateStore) CommitGeneration(nodeID string, gen uint64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if current, ok := s.generations[nodeID]; ok && current > gen {
		return ErrRollbackAttempt
	}
	s.generations[nodeID] = gen
	return nil
}
