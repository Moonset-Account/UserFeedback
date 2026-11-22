package errfeedback

import (
	"fmt"
	"sort"
	"sync"
)

// JournalKind tags one journal entry.
type JournalKind int

const (
	// JournalBegin records a successfully started step together with the exact
	// gradient that was passed to Begin.
	JournalBegin JournalKind = iota
	// JournalConfirm records the outcome of one Confirm call.
	JournalConfirm
	// JournalAbort records that a step was abandoned.
	JournalAbort
)

func (k JournalKind) String() string {
	switch k {
	case JournalBegin:
		return "begin"
	case JournalConfirm:
		return "confirm"
	case JournalAbort:
		return "abort"
	default:
		return fmt.Sprintf("JournalKind(%d)", int(k))
	}
}

// JournalEntry is one immutable record in a sender's journal.
//
// Each entry carries a chained checksum, so a truncated or reordered log is
// detected during recovery instead of silently replaying the wrong residual.
type JournalEntry struct {
	Kind     JournalKind
	Source   SourceID
	Step     StepID
	Attempt  uint64
	Layout   LayoutID
	Plan     PlanID
	Spec     StepSpec
	Buckets  []Bucket     // plan buckets; only used by JournalBegin
	Gradient []CoordValue // sorted by Coord; only used by JournalBegin
	Bucket   BucketID     // only used by JournalConfirm
	Outcome  ConfirmOutcome
	Prev     uint64
	Chain    uint64
}

func (e JournalEntry) computeChain() uint64 {
	const offset64 = 1469598103934665603
	const prime64 = 1099511628211
	var h uint64 = offset64 ^ e.Prev
	mix := func(v uint64) {
		for i := 0; i < 8; i++ {
			h ^= v & 0xff
			h *= prime64
			v >>= 8
		}
	}
	mix(uint64(e.Kind))
	mix(uint64(int64(e.Source)))
	mix(uint64(e.Step))
	mix(e.Attempt)
	mix(uint64(e.Layout))
	mix(uint64(e.Plan))
	mix(uint64(e.Spec.Lattice))
	mix(uint64(e.Spec.Rounding))
	mix(uint64(e.Spec.Keep))
	mix(uint64(e.Spec.MaxAbs))
	mix(uint64(len(e.Buckets)))
	for _, b := range e.Buckets {
		mix(uint64(int64(b.ID)))
		mix(uint64(len(b.Coords)))
		for _, c := range b.Coords {
			mix(uint64(int64(c)))
		}
	}
	mix(uint64(len(e.Gradient)))
	for _, cv := range e.Gradient {
		mix(uint64(int64(cv.Coord)))
		mix(uint64(cv.Value))
	}
	mix(uint64(int64(e.Bucket)))
	mix(uint64(e.Outcome))
	return h
}

func (e JournalEntry) clone() JournalEntry {
	e.Buckets = cloneBuckets(e.Buckets)
	e.Gradient = append([]CoordValue(nil), e.Gradient...)
	return e
}

// Journal records every sender decision needed to rebuild residual state and to
// resend the packets of an unconfirmed step after a crash.
type Journal struct {
	mu      sync.Mutex
	source  SourceID
	coords  []Coord
	entries []JournalEntry
	head    uint64
}

// NewJournal creates an empty journal for one sender identity.
func NewJournal(source SourceID, coords []Coord) *Journal {
	cp := append([]Coord(nil), coords...)
	sort.Slice(cp, func(i, j int) bool { return cp[i] < cp[j] })
	return &Journal{source: source, coords: cp}
}

// Source returns the journal's sender identity.
func (j *Journal) Source() SourceID { return j.source }

// Len returns the number of recorded entries.
func (j *Journal) Len() int {
	j.mu.Lock()
	defer j.mu.Unlock()
	return len(j.entries)
}

func (j *Journal) appendLocked(e JournalEntry) {
	e.Prev = j.head
	e = e.clone()
	e.Chain = e.computeChain()
	j.entries = append(j.entries, e)
	j.head = e.Chain
}

func cloneBuckets(in []Bucket) []Bucket {
	if len(in) == 0 {
		return nil
	}
	out := make([]Bucket, len(in))
	for i, b := range in {
		out[i] = Bucket{ID: b.ID, Coords: append([]Coord(nil), b.Coords...)}
	}
	return out
}

// Entries returns a deep copy of the journal.
func (j *Journal) Entries() []JournalEntry {
	j.mu.Lock()
	defer j.mu.Unlock()
	out := make([]JournalEntry, len(j.entries))
	for i, e := range j.entries {
		out[i] = e.clone()
	}
	return out
}

// Journal returns the journal currently attached to the sender, or nil.
func (s *Sender) Journal() *Journal {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.journal
}

// AttachJournal wires an existing journal to the sender. The journal identity
// must match the sender's source and coordinate set.
func (s *Sender) AttachJournal(j *Journal) error {
	if j == nil {
		return fmt.Errorf("%w: nil journal", ErrInvalidSpec)
	}
	if j.source != s.source {
		return fmt.Errorf("%w: journal source %d", ErrWrongSource, j.source)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if !equalCoords(j.coords, s.coords) {
		return fmt.Errorf("%w: journal coordinate set differs", ErrInvalidSpec)
	}
	s.journal = j
	j.mu.Lock()
	j.source = s.source
	j.coords = append([]Coord(nil), s.coords...)
	j.mu.Unlock()
	return nil
}

func equalCoords(a, b []Coord) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// recordBegin stores a Begin entry. The caller holds the sender lock; the
// journal has its own lock.
func (s *Sender) recordBegin(st *senderStep, gradient map[Coord]int64) {
	if s.journal == nil {
		return
	}
	grad := make([]CoordValue, 0, len(s.coords))
	for _, c := range s.coords {
		grad = append(grad, CoordValue{Coord: c, Value: gradient[c]})
	}
	s.journal.mu.Lock()
	defer s.journal.mu.Unlock()
	s.journal.appendLocked(JournalEntry{
		Kind:     JournalBegin,
		Source:   s.source,
		Step:     st.id,
		Attempt:  st.attempt,
		Layout:   st.plan.Layout,
		Plan:     st.plan.ID,
		Spec:     st.spec,
		Buckets:  st.plan.Buckets,
		Gradient: grad,
	})
}

func (s *Sender) recordConfirm(ack Ack, outcome ConfirmOutcome) {
	if s.journal == nil {
		return
	}
	s.journal.mu.Lock()
	defer s.journal.mu.Unlock()
	s.journal.appendLocked(JournalEntry{
		Kind:    JournalConfirm,
		Source:  ack.Source,
		Step:    ack.Step,
		Attempt: ack.Attempt,
		Layout:  ack.Layout,
		Plan:    ack.Plan,
		Bucket:  ack.Bucket,
		Outcome: outcome,
	})
}

func (s *Sender) recordAbort(step StepID) {
	if s.journal == nil {
		return
	}
	s.journal.mu.Lock()
	defer s.journal.mu.Unlock()
	s.journal.appendLocked(JournalEntry{
		Kind:   JournalAbort,
		Source: s.source,
		Step:   step,
	})
}

// RecoverSender rebuilds a sender from journal entries.
//
//   - Every recorded Begin/Confirm/Abort is replayed against a fresh sender, so
//     the confirmed residual of the recovered sender is exactly the residual the
//     original sender had after its last settled step.
//   - A step whose Begin was recorded but that never fully confirmed stays open;
//     the caller can call Packets(step) to resend it verbatim, because packets
//     are a pure function of (gradient, residual, spec, plan).
//   - Replaying a late duplicate confirmation yields ConfirmDuplicate and never
//     commits the residual a second time.
//
// The chain checksum is verified first; a tampered log returns
// ErrJournalInconsistent.
func RecoverSender(entries []JournalEntry) (*Sender, error) {
	if len(entries) == 0 {
		return nil, fmt.Errorf("%w: empty journal", ErrJournalInconsistent)
	}

	var prev uint64
	var source SourceID
	var coords []Coord
	for i, e := range entries {
		if e.Chain == 0 || e.Prev != prev {
			return nil, fmt.Errorf("%w: entry %d broken chain", ErrJournalInconsistent, i)
		}
		if e.Chain != e.computeChain() {
			return nil, fmt.Errorf("%w: entry %d checksum mismatch", ErrJournalInconsistent, i)
		}
		prev = e.Chain
		if e.Kind == JournalBegin {
			if i == 0 {
				source = e.Source
			}
			bucketCoords := make([]Coord, 0)
			for _, b := range e.Buckets {
				bucketCoords = append(bucketCoords, b.Coords...)
			}
			sort.Slice(bucketCoords, func(a, b int) bool { return bucketCoords[a] < bucketCoords[b] })
			if coords == nil {
				coords = bucketCoords
			} else if !equalCoords(coords, bucketCoords) {
				return nil, fmt.Errorf("%w: entry %d changed coordinate set", ErrJournalInconsistent, i)
			}
		}
	}
	if coords == nil {
		return nil, fmt.Errorf("%w: no begin entry", ErrJournalInconsistent)
	}

	snd, err := NewSender(source, coords)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrJournalInconsistent, err)
	}

	// Replay without a journal attached: re-recording during replay would
	// duplicate every entry. The journal is installed after replay below.
	for i, e := range entries {
		switch e.Kind {
		case JournalBegin:
			plan, err := NewStepPlan(e.Plan, e.Layout, cloneBuckets(e.Buckets)...)
			if err != nil {
				return nil, fmt.Errorf("%w: entry %d: %v", ErrJournalInconsistent, i, err)
			}
			grad := make(map[Coord]int64, len(e.Gradient))
			for _, cv := range e.Gradient {
				grad[cv.Coord] = cv.Value
			}
			if err := snd.Begin(e.Step, e.Spec, plan, grad); err != nil {
				return nil, fmt.Errorf("%w: entry %d: %v", ErrJournalInconsistent, i, err)
			}
		case JournalConfirm:
			out, err := snd.Confirm(Ack{
				Step:    e.Step,
				Source:  source,
				Bucket:  e.Bucket,
				Attempt: e.Attempt,
				Layout:  e.Layout,
				Plan:    e.Plan,
			})
			if err != nil {
				return nil, fmt.Errorf("%w: entry %d: %v", ErrJournalInconsistent, i, err)
			}
			if out != e.Outcome {
				return nil, fmt.Errorf("%w: entry %d: recorded %v replayed %v",
					ErrJournalInconsistent, i, e.Outcome, out)
			}
		case JournalAbort:
			snd.Abort(e.Step)
		default:
			return nil, fmt.Errorf("%w: entry %d: unknown kind", ErrJournalInconsistent, i)
		}
	}

	// Install a journal pre-loaded with the recovered entries so the recovered
	// sender keeps recording consistently and the chain continues from the old
	// head. The entries are copied verbatim; their chain is already verified.
	j := NewJournal(source, coords)
	j.mu.Lock()
	for _, e := range entries {
		j.entries = append(j.entries, e.clone())
	}
	j.head = prev
	j.mu.Unlock()
	if err := snd.AttachJournal(j); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrJournalInconsistent, err)
	}
	return snd, nil
}
