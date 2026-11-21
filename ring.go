package dmaring

import (
	"sync"
)

// request tracks one outstanding DMA request in the ring.
type request struct {
	id     uint64
	offset uint64 // starting slot index (ring's monotonic slot counter)
	count  int    // number of slots occupied
	done   bool
	descs  []Descriptor
}

// RequestInfo is an immutable snapshot of an outstanding request, returned by
// introspection helpers and Reap.
type RequestInfo struct {
	ID          uint64
	StartSlot   int // absolute slot index modulo Ring.Capacity()
	Count       int
	Descriptors []Descriptor
	Done        bool
}

// Ring is a fixed-capacity circular queue of DMA descriptor slots.
//
// All exported methods are safe for concurrent use.
type Ring struct {
	mu       sync.Mutex
	limits   Limits
	capacity int

	head  uint64 // monotonic count of slots consumed via Reap
	tail  uint64 // monotonic count of slots produced via Submit
	slots []Descriptor

	// outstanding requests in submission order; the first one is the oldest.
	reqs []*request
	// index lookup of outstanding requests by ID.
	byID map[uint64]*request
}

// NewRing creates a ring with capacity descriptor slots and the given hardware
// limits. capacity and limits must be valid.
func NewRing(capacity int, l Limits) (*Ring, error) {
	if capacity <= 0 {
		return nil, ErrInvalidLimits
	}
	if err := l.validate(); err != nil {
		return nil, err
	}
	return &Ring{
		limits:   l,
		capacity: capacity,
		slots:    make([]Descriptor, capacity),
		byID:     make(map[uint64]*request),
	}, nil
}

// Capacity returns the fixed number of descriptor slots.
func (r *Ring) Capacity() int { return r.capacity }

// Free returns the number of slots currently available for production.
func (r *Ring) Free() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.capacity - int(r.tail-r.head)
}

// Used returns the number of slots currently occupied by outstanding requests.
func (r *Ring) Used() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return int(r.tail - r.head)
}

// Outstanding returns the number of requests that have not been reaped yet.
func (r *Ring) Outstanding() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.reqs)
}

// Slot returns the descriptor currently stored in the physical slot index
// (0..Capacity()-1). Empty slots return a zero Descriptor.
func (r *Ring) Slot(index int) (Descriptor, error) {
	if index < 0 || index >= r.capacity {
		return Descriptor{}, ErrInvalidLimits
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.slots[index], nil
}

// Submit packs req into descriptors and reserves all of its slots atomically.
//
// On success the descriptors are written into the ring (wrapping past the tail
// slot) and the production pointer advances. When not enough slots are free the
// call returns ErrNoSpace, writes nothing and leaves the production pointer and
// every existing slot unchanged.
func (r *Ring) Submit(id uint64, req []Segment) ([]Descriptor, error) {
	descs, err := BuildDescriptors(req, r.limits)
	if err != nil {
		return nil, err
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	if _, exists := r.byID[id]; exists {
		return nil, ErrDuplicateRequest
	}
	need := len(descs)
	free := r.capacity - int(r.tail-r.head)
	if need > free {
		return nil, ErrNoSpace
	}

	// All slots are reserved together; only now does the production pointer move.
	start := r.tail
	for i, d := range descs {
		r.slots[int((start+uint64(i))%uint64(r.capacity))] = d
	}
	r.tail += uint64(need)

	reqEntry := &request{
		id:     id,
		offset: start,
		count:  need,
		done:   false,
		descs:  descs,
	}
	r.reqs = append(r.reqs, reqEntry)
	r.byID[id] = reqEntry

	// Return a copy so callers cannot mutate ring state.
	out := make([]Descriptor, len(descs))
	copy(out, descs)
	return out, nil
}

// Complete marks request id as finished by the device. Completions may arrive in
// any order; unknown or already-completed requests produce ErrUnknownRequest.
func (r *Ring) Complete(id uint64) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	reqEntry, ok := r.byID[id]
	if !ok {
		return ErrUnknownRequest
	}
	reqEntry.done = true
	return nil
}

// Reap releases every leading request that has completed, in submission order.
// It stops at the oldest request not yet completed even if younger ones are
// done. Released slots become free for subsequent Submit calls; the returned
// slice is empty when the oldest request is still outstanding.
func (r *Ring) Reap() []RequestInfo {
	r.mu.Lock()
	defer r.mu.Unlock()

	reaped := make([]RequestInfo, 0)
	for len(r.reqs) > 0 && r.reqs[0].done {
		oldest := r.reqs[0]
		reaped = append(reaped, RequestInfo{
			ID:          oldest.id,
			StartSlot:   int(oldest.offset % uint64(r.capacity)),
			Count:       oldest.count,
			Descriptors: append([]Descriptor(nil), oldest.descs...),
			Done:        true,
		})
		r.head += uint64(oldest.count)
		delete(r.byID, oldest.id)
		r.reqs = r.reqs[1:]
	}
	return reaped
}

// Snapshot returns submission-ordered information about all outstanding
// requests, including completed ones that cannot be reaped yet.
func (r *Ring) Snapshot() []RequestInfo {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]RequestInfo, 0, len(r.reqs))
	for _, reqEntry := range r.reqs {
		out = append(out, RequestInfo{
			ID:          reqEntry.id,
			StartSlot:   int(reqEntry.offset % uint64(r.capacity)),
			Count:       reqEntry.count,
			Descriptors: append([]Descriptor(nil), reqEntry.descs...),
			Done:        reqEntry.done,
		})
	}
	return out
}
