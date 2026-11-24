package reinject

import (
	"context"
	"sync"
)

// MemoryProgress is an in-memory ProgressReader used by the tests and usable
// as a local synthetic-data adapter. It never rewinds stored progress.
type MemoryProgress struct {
	mu       sync.Mutex
	progress map[Group]map[string]AckProgress
}

// NewMemoryProgress creates an empty progress store.
func NewMemoryProgress() *MemoryProgress {
	return &MemoryProgress{progress: make(map[Group]map[string]AckProgress)}
}

// Set stores (or replaces) the progress record for p.Group on p.Key.
func (m *MemoryProgress) Set(p AckProgress) {
	m.mu.Lock()
	defer m.mu.Unlock()
	byKey, ok := m.progress[p.Group]
	if !ok {
		byKey = make(map[string]AckProgress)
		m.progress[p.Group] = byKey
	}
	skips := make(map[uint64]bool, len(p.SkippedSeq))
	for seq, skipped := range p.SkippedSeq {
		skips[seq] = skipped
	}
	p.SkippedSeq = skips
	byKey[p.Key] = p
}

// MarkSkip records an irrevocable skip of seq for group on key, preserving
// any previously acknowledged progress.
func (m *MemoryProgress) MarkSkip(group Group, key string, seq uint64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	byKey, ok := m.progress[group]
	if !ok {
		byKey = make(map[string]AckProgress)
		m.progress[group] = byKey
	}
	p := byKey[key]
	p.Group = group
	p.Key = key
	if p.SkippedSeq == nil {
		p.SkippedSeq = make(map[uint64]bool)
	}
	p.SkippedSeq[seq] = true
	byKey[key] = p
}

// Progress returns a defensive copy of the stored record. A missing record is
// reported as a group that has never consumed the key.
func (m *MemoryProgress) Progress(_ context.Context, group Group, key string) (AckProgress, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if byKey, ok := m.progress[group]; ok {
		if p, ok := byKey[key]; ok {
			skips := make(map[uint64]bool, len(p.SkippedSeq))
			for seq, skipped := range p.SkippedSeq {
				skips[seq] = skipped
			}
			p.SkippedSeq = skips
			return p, nil
		}
	}
	return AckProgress{Group: group, Key: key, SkippedSeq: map[uint64]bool{}}, nil
}

type reinstateCall struct {
	group   Group
	id      MessageID
	key     string
	seq     uint64
	payload []byte
}

type deliverCall struct {
	id         MessageID
	key        string
	seq        uint64
	customerID string
	payload    []byte
}

// FakeBroker records queue side effects instead of touching a real broker.
type FakeBroker struct {
	mu                sync.Mutex
	reinstated        []reinstateCall
	delivered         []deliverCall
	failReinstateOnce error
	failDeliverOnce   error
}

// FailNextReinstate makes the next ReinstateAt call fail with err once.
func (b *FakeBroker) FailNextReinstate(err error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.failReinstateOnce = err
}

// FailNextDeliver makes the next Deliver call fail with err once.
func (b *FakeBroker) FailNextDeliver(err error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.failDeliverOnce = err
}

// ReinstateAt records an in-place re-injection.
func (b *FakeBroker) ReinstateAt(_ context.Context, group Group, id MessageID, key string, seq uint64, payload []byte) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.failReinstateOnce != nil {
		err := b.failReinstateOnce
		b.failReinstateOnce = nil
		return err
	}
	b.reinstated = append(b.reinstated, reinstateCall{
		group:   group,
		id:      id,
		key:     key,
		seq:     seq,
		payload: append([]byte(nil), payload...),
	})
	return nil
}

// Deliver records a normal delivery.
func (b *FakeBroker) Deliver(_ context.Context, id MessageID, key string, seq uint64, customerID string, payload []byte) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.failDeliverOnce != nil {
		err := b.failDeliverOnce
		b.failDeliverOnce = nil
		return err
	}
	b.delivered = append(b.delivered, deliverCall{
		id:         id,
		key:        key,
		seq:        seq,
		customerID: customerID,
		payload:    append([]byte(nil), payload...),
	})
	return nil
}

// Reinstated returns a snapshot of recorded in-place re-injections.
func (b *FakeBroker) Reinstated() []reinstateCall {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]reinstateCall, len(b.reinstated))
	copy(out, b.reinstated)
	return out
}

// Delivered returns a snapshot of recorded normal deliveries.
func (b *FakeBroker) Delivered() []deliverCall {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]deliverCall, len(b.delivered))
	copy(out, b.delivered)
	return out
}

type reviewCall struct {
	group      Group
	id         MessageID
	key        string
	seq        uint64
	customerID string
	payload    []byte
}

// FakeReview records repaired content sent to the pending-review source.
type FakeReview struct {
	mu           sync.Mutex
	calls        []reviewCall
	failSendOnce error
}

// FailNextSend makes the next SendForReview call fail with err once.
func (r *FakeReview) FailNextSend(err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.failSendOnce = err
}

// SendForReview records a review routing.
func (r *FakeReview) SendForReview(_ context.Context, group Group, id MessageID, key string, seq uint64, customerID string, payload []byte) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.failSendOnce != nil {
		err := r.failSendOnce
		r.failSendOnce = nil
		return err
	}
	r.calls = append(r.calls, reviewCall{
		group:      group,
		id:         id,
		key:        key,
		seq:        seq,
		customerID: customerID,
		payload:    append([]byte(nil), payload...),
	})
	return nil
}

// Calls returns a snapshot of recorded review routings.
func (r *FakeReview) Calls() []reviewCall {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]reviewCall, len(r.calls))
	copy(out, r.calls)
	return out
}
