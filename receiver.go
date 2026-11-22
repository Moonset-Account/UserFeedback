package errfeedback

import (
	"fmt"
	"sort"
	"sync"
)

// Expect configures what a receiver expects for one step.
type Expect struct {
	Step    StepID
	Attempt uint64
	Layout  LayoutID
	Plan    PlanID
	Sources []SourceID
	Buckets []BucketID
}

// validate checks the expectation is well formed.
func (e Expect) validate() error {
	if len(e.Sources) == 0 {
		return ErrEmptySources
	}
	if len(e.Buckets) == 0 {
		return ErrInvalidPlan
	}
	seenS := make(map[SourceID]struct{}, len(e.Sources))
	for _, s := range e.Sources {
		if _, dup := seenS[s]; dup {
			return fmt.Errorf("%w: duplicate source %d", ErrInvalidSpec, s)
		}
		seenS[s] = struct{}{}
	}
	seenB := make(map[BucketID]struct{}, len(e.Buckets))
	for _, b := range e.Buckets {
		if _, dup := seenB[b]; dup {
			return fmt.Errorf("%w: duplicate bucket %d", ErrInvalidSpec, b)
		}
		seenB[b] = struct{}{}
	}
	return nil
}

// AckResult describes what a receiver did with one message.
type AckResult int

const (
	// AckAccepted means the message was newly accepted and stored.
	AckAccepted AckResult = iota
	// AckDuplicate means the (step, source, bucket) slot already held a message
	// and this one was dropped without touching the aggregate.
	AckDuplicate
	// AckRejected means the message did not match the expectation and was not
	// stored.
	AckRejected
)

func (a AckResult) String() string {
	switch a {
	case AckAccepted:
		return "accepted"
	case AckDuplicate:
		return "duplicate"
	case AckRejected:
		return "rejected"
	default:
		return fmt.Sprintf("AckResult(%d)", int(a))
	}
}

// Contribution is the aggregate a receiver publishes for one source.
type Contribution struct {
	Source  SourceID
	Entries []CoordValue // sparse, aggregated per coordinate
}

// Receiver collects messages for expected steps and publishes the aggregate
// once every (source, bucket) group has been accepted exactly once.
//
// "Same logical origin" is enforced by the (Step, Source, Bucket) key: a second
// message for a key is dropped, never summed. A message from a source that was
// never expected is rejected. A voided step ignores all arriving messages and
// keeps no residual, because the receiver holds no residual state at all; the
// sender's residual is the only residual.
type Receiver struct {
	mu sync.Mutex

	steps map[StepID]*recvStep
	order []StepID
}

type recvStep struct {
	expect    Expect
	groups    map[SourceID]map[BucketID]SparseMessage
	accepted  int
	total     int
	settled   bool
	voided    bool
	published []Contribution
}

// NewReceiver creates an empty receiver.
func NewReceiver() *Receiver {
	return &Receiver{steps: make(map[StepID]*recvStep)}
}

// Expect registers the expected groups for a step. Registering a step twice
// with the same expectation is idempotent; a different expectation is
// rejected.
func (r *Receiver) Expect(e Expect) error {
	if err := e.validate(); err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if st, ok := r.steps[e.Step]; ok {
		if st.expect.Sources == nil && st.expect.Buckets == nil {
			// A void placeholder created by AbortStep before any expectation:
			// record the expectation but keep the step voided.
			st.expect = e
			if st.groups == nil {
				st.groups = make(map[SourceID]map[BucketID]SparseMessage, len(e.Sources))
				for _, src := range e.Sources {
					st.groups[src] = make(map[BucketID]SparseMessage, len(e.Buckets))
				}
			}
			st.total = len(e.Sources) * len(e.Buckets)
			return nil
		}
		if st.expect.Attempt != e.Attempt ||
			st.expect.Layout != e.Layout ||
			st.expect.Plan != e.Plan ||
			!sameSourceSet(st.expect.Sources, e.Sources) ||
			!sameBucketSet(st.expect.Buckets, e.Buckets) {
			return fmt.Errorf("%w: conflicting expectation for step %d", ErrInvalidSpec, e.Step)
		}
		return nil
	}
	st := &recvStep{
		expect: e,
		groups: make(map[SourceID]map[BucketID]SparseMessage, len(e.Sources)),
		total:  len(e.Sources) * len(e.Buckets),
	}
	for _, src := range e.Sources {
		st.groups[src] = make(map[BucketID]SparseMessage, len(e.Buckets))
	}
	r.steps[e.Step] = st
	r.order = append(r.order, e.Step)
	return nil
}

func sameSourceSet(a, b []SourceID) bool {
	if len(a) != len(b) {
		return false
	}
	m := make(map[SourceID]int, len(a))
	for _, v := range a {
		m[v]++
	}
	for _, v := range b {
		if m[v] == 0 {
			return false
		}
		m[v]--
	}
	return true
}

func sameBucketSet(a, b []BucketID) bool {
	if len(a) != len(b) {
		return false
	}
	m := make(map[BucketID]int, len(a))
	for _, v := range a {
		m[v]++
	}
	for _, v := range b {
		if m[v] == 0 {
			return false
		}
		m[v]--
	}
	return true
}

// Deliver hands one packet to the receiver. The checksum is verified first; a
// corrupted packet is rejected and never stored. Duplicate deliveries for the
// same (step, source, bucket) are dropped. Deliver returns an AckResult plus a
// non-nil error only for rejected packets that carry an integrity or protocol
// fault.
func (r *Receiver) Deliver(m SparseMessage) (AckResult, error) {
	if m.Checksum != 0 && m.Checksum != m.ComputeChecksum() {
		return AckRejected, ErrIntegrity
	}
	r.mu.Lock()
	defer r.mu.Unlock()

	st, ok := r.steps[m.Step]
	if !ok {
		return AckRejected, fmt.Errorf("%w: %d", ErrUnknownStep, m.Step)
	}
	if st.voided {
		return AckRejected, ErrStepVoided
	}
	if st.settled {
		return AckDuplicate, ErrStepSettled
	}
	if m.Attempt != st.expect.Attempt {
		return AckRejected, fmt.Errorf("%w: got %d want %d", ErrStaleAttempt, m.Attempt, st.expect.Attempt)
	}
	if m.Layout != st.expect.Layout || m.Plan != st.expect.Plan {
		return AckRejected, ErrStaleLayout
	}
	if _, ok := st.groups[m.Source]; !ok {
		return AckRejected, fmt.Errorf("%w: %d", ErrUnknownSource, m.Source)
	}
	if !containsBucket(st.expect.Buckets, m.Bucket) {
		return AckRejected, fmt.Errorf("%w: %d", ErrUnknownGroup, m.Bucket)
	}
	if _, exists := st.groups[m.Source][m.Bucket]; exists {
		return AckDuplicate, ErrDuplicateMessage
	}

	clean := m.normalized()
	clean.Checksum = m.Checksum
	st.groups[m.Source][m.Bucket] = clean
	st.accepted++
	if st.accepted < st.total {
		return AckAccepted, nil
	}

	// Every group present: publish per-source aggregates.
	st.settled = true
	st.published = make([]Contribution, 0, len(st.expect.Sources))
	sources := append([]SourceID(nil), st.expect.Sources...)
	sort.Slice(sources, func(i, j int) bool { return sources[i] < sources[j] })
	for _, src := range sources {
		acc := make(map[Coord]int64)
		for _, b := range st.expect.Buckets {
			for _, e := range st.groups[src][b].Entries {
				sum, err := checkAdd(acc[e.Coord], e.Value)
				if err != nil {
					return AckRejected, err
				}
				acc[e.Coord] = sum
			}
		}
		coords := make([]Coord, 0, len(acc))
		for c := range acc {
			coords = append(coords, c)
		}
		sort.Slice(coords, func(i, j int) bool { return coords[i] < coords[j] })
		entries := make([]CoordValue, len(coords))
		for i, c := range coords {
			entries[i] = CoordValue{Coord: c, Value: acc[c]}
		}
		st.published = append(st.published, Contribution{Source: src, Entries: entries})
	}
	return AckAccepted, nil
}

func containsBucket(list []BucketID, id BucketID) bool {
	for _, b := range list {
		if b == id {
			return true
		}
	}
	return false
}

// Published returns the aggregate for a step once every group is accepted. The
// second return value reports whether the step is fully settled.
func (r *Receiver) Published(step StepID) ([]Contribution, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	st, ok := r.steps[step]
	if !ok || !st.settled {
		return nil, false
	}
	out := make([]Contribution, len(st.published))
	for i, c := range st.published {
		out[i] = Contribution{Source: c.Source, Entries: append([]CoordValue(nil), c.Entries...)}
	}
	return out, true
}

// AbortStep voids a step on the receiver: later packets are rejected and no
// aggregate is ever published. AbortStep is idempotent.
func (r *Receiver) AbortStep(step StepID) {
	r.mu.Lock()
	defer r.mu.Unlock()
	st, ok := r.steps[step]
	if !ok {
		st = &recvStep{groups: make(map[SourceID]map[BucketID]SparseMessage)}
		r.steps[step] = st
		r.order = append(r.order, step)
	}
	if !st.settled {
		st.voided = true
	}
}

// AcceptedCount reports how many (source, bucket) groups a step has accepted so
// far, and the total it expects.
func (r *Receiver) AcceptedCount(step StepID) (accepted, total int, ok bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	st, ok := r.steps[step]
	if !ok {
		return 0, 0, false
	}
	return st.accepted, st.total, true
}
