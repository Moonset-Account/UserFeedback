package exceptall

import (
	"context"
	"fmt"
	"sync"
)

// MemorySegmentManager allocates sequential in-memory segments.
//
// It implements SegmentManager and SegmentStats and is convenient in tests.
// The segment payload itself is outside the operator's bounded sort buffer;
// production deployments that need physical spilling should use
// FileSegmentManager or their own Segment implementation.
type MemorySegmentManager struct {
	mu       sync.Mutex
	nextID   int
	created  int
	released int
	segments map[string]*MemorySegment
}

// NewMemorySegmentManager creates an empty tracking manager.
func NewMemorySegmentManager() *MemorySegmentManager {
	return &MemorySegmentManager{segments: make(map[string]*MemorySegment)}
}

// CreateSegment implements SegmentManager.
func (m *MemorySegmentManager) CreateSegment(_ context.Context) (Segment, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	m.nextID++
	m.created++
	id := fmt.Sprintf("memory-segment-%d", m.nextID)
	s := &MemorySegment{id: id, manager: m}
	m.segments[id] = s
	return s, nil
}

// CreatedSegments returns the number of segments ever created.
func (m *MemorySegmentManager) CreatedSegments() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.created
}

// ReleasedSegments returns the number of segments whose Release completed.
func (m *MemorySegmentManager) ReleasedSegments() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.released
}

// ActiveSegments returns created but unreleased segments.
func (m *MemorySegmentManager) ActiveSegments() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.created - m.released
}

func (m *MemorySegmentManager) markReleased(id string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.segments[id]; ok {
		delete(m.segments, id)
		m.released++
	}
}

// MemorySegment stores records in order.
type MemorySegment struct {
	mu       sync.RWMutex
	id       string
	manager  *MemorySegmentManager
	records  []Record
	released bool
}

// ID implements Segment.
func (s *MemorySegment) ID() string { return s.id }

// Append implements Segment.
func (s *MemorySegment) Append(ctx context.Context, record Record) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.released {
		return fmt.Errorf("exceptall: append to released segment %s", s.id)
	}
	// Copy the pointer-bearing record. The pointed-to text is treated as
	// immutable input and is not mutated by the executor.
	s.records = append(s.records, record)
	return nil
}

// OpenReader implements Segment.
func (s *MemorySegment) OpenReader(ctx context.Context) (SegmentReader, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.released {
		return nil, fmt.Errorf("exceptall: read from released segment %s", s.id)
	}
	return &memorySegmentReader{segment: s}, nil
}

// Release implements Segment.
func (s *MemorySegment) Release() error {
	s.mu.Lock()
	if s.released {
		s.mu.Unlock()
		return nil
	}
	s.released = true
	s.records = nil
	s.mu.Unlock()

	if s.manager != nil {
		s.manager.markReleased(s.id)
	}
	return nil
}

type memorySegmentReader struct {
	segment *MemorySegment
	pos     int
	closed  bool
}

// Next implements SegmentReader.
func (r *memorySegmentReader) Next(ctx context.Context) (Record, bool, error) {
	if err := ctx.Err(); err != nil {
		return Record{}, false, err
	}
	if r.closed {
		return Record{}, false, fmt.Errorf("exceptall: read from closed reader for %s", r.segment.id)
	}

	r.segment.mu.RLock()
	defer r.segment.mu.RUnlock()
	if r.segment.released {
		return Record{}, false, fmt.Errorf("exceptall: segment %s released while reading", r.segment.id)
	}
	if r.pos >= len(r.segment.records) {
		return Record{}, false, nil
	}
	rec := r.segment.records[r.pos]
	r.pos++
	return rec, true, nil
}

// Close implements SegmentReader.
func (r *memorySegmentReader) Close() error {
	r.closed = true
	return nil
}
