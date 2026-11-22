package errfeedback

import (
	"fmt"
	"sort"
	"sync"
)

// Sender holds the confirmed residual per logical coordinate for one source.
//
// Lifecycle per step:
//
//	Begin(step, spec, plan, gradient) -> build packets, stage tentative residual
//	  Packets(step) / ActivateBucket(...) -> messages to transmit
//	  Confirm(ack)                        -> one bucket acknowledgement each
//	Abort(step)                           -> drop the step, residual unchanged
//
// The tentative residual is committed only when the last bucket is confirmed,
// so an aborted or partially confirmed step never writes compensation twice.
//
// Residuals and pending steps are keyed by logical coordinate, never by packet
// position. Changing the bucket plan between steps therefore moves
// compensation with its coordinate.
type Sender struct {
	mu sync.Mutex

	source   SourceID
	coords   []Coord
	residual map[Coord]int64
	attempt  uint64
	layouts  map[LayoutID]uint64

	pending map[StepID]*senderStep
	settled map[StepID]map[BucketID]struct{}
	voided  map[StepID]struct{}
	history []StepID

	journal *Journal
}

type senderStep struct {
	id        StepID
	attempt   uint64
	plan      StepPlan
	spec      StepSpec
	input     map[Coord]int64
	tentative map[Coord]int64
	messages  map[BucketID]SparseMessage
	confirmed map[BucketID]struct{}
	order     []BucketID
}

// NewSender creates a sender for the given logical coordinates. The coordinate
// set is fixed for the lifetime of the sender; only the bucketing changes.
func NewSender(source SourceID, coords []Coord) (*Sender, error) {
	if len(coords) == 0 {
		return nil, fmt.Errorf("%w: no coordinates", ErrInvalidSpec)
	}
	cp := append([]Coord(nil), coords...)
	sort.Slice(cp, func(i, j int) bool { return cp[i] < cp[j] })
	for i := 1; i < len(cp); i++ {
		if cp[i-1] == cp[i] {
			return nil, fmt.Errorf("%w: duplicate coordinate %d", ErrInvalidSpec, cp[i])
		}
	}
	return &Sender{
		source:   source,
		coords:   cp,
		residual: make(map[Coord]int64, len(cp)),
		layouts:  make(map[LayoutID]uint64),
		pending:  make(map[StepID]*senderStep),
		settled:  make(map[StepID]map[BucketID]struct{}),
		voided:   make(map[StepID]struct{}),
	}, nil
}

// Source returns the logical source this sender represents.
func (s *Sender) Source() SourceID { return s.source }

// Coords returns a copy of the sender's logical coordinate set.
func (s *Sender) Coords() []Coord {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]Coord(nil), s.coords...)
}

// Residual returns the currently confirmed residual for coord.
func (s *Sender) Residual(coord Coord) (int64, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.residual[coord]
	return v, ok
}

// Residuals returns a snapshot of all confirmed residuals keyed by coordinate.
func (s *Sender) Residuals() map[Coord]int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[Coord]int64, len(s.residual))
	for k, v := range s.residual {
		out[k] = v
	}
	return out
}

// Status reports the settlement state of a step.
func (s *Sender) Status(step StepID) (StepStatus, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.pending[step]; ok {
		return StepOpen, true
	}
	if _, ok := s.settled[step]; ok {
		return StepSettled, true
	}
	if _, ok := s.voided[step]; ok {
		return StepVoided, true
	}
	return StepOpen, false
}

// StepAttempt returns the attempt number assigned to step.
func (s *Sender) StepAttempt(step StepID) (uint64, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if st, ok := s.pending[step]; ok {
		return st.attempt, true
	}
	return 0, false
}

// PendingSteps returns the currently open step IDs in begin order.
func (s *Sender) PendingSteps() []StepID {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []StepID
	for _, id := range s.history {
		if _, ok := s.pending[id]; ok {
			out = append(out, id)
		}
	}
	return out
}

// Begin starts a new step with the given gradient and rule. The gradient map
// must contain exactly the sender's coordinate set. Begin fails with
// ErrStepInProgress if an earlier step is still open.
func (s *Sender) Begin(step StepID, spec StepSpec, plan StepPlan, gradient map[Coord]int64) error {
	if err := spec.Validate(); err != nil {
		return err
	}
	if err := plan.Validate(); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidPlan, err)
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	if len(s.pending) > 0 {
		return ErrStepInProgress
	}
	if _, ok := s.settled[step]; ok {
		return ErrStepSettled
	}
	if _, ok := s.voided[step]; ok {
		return fmt.Errorf("%w: %d", ErrStepVoided, step)
	}
	if s.hasHistoryLocked(step) {
		return fmt.Errorf("%w: step %d reused", ErrStaleAttempt, step)
	}
	if !plan.coversExactly(s.coords) {
		return fmt.Errorf("%w: plan does not cover the sender coordinates", ErrInvalidPlan)
	}
	if fp, ok := s.layouts[plan.Layout]; ok && fp != plan.fingerprint() {
		return fmt.Errorf("%w: layout %d reused with different partition", ErrStaleLayout, plan.Layout)
	}

	input := make(map[Coord]int64, len(s.coords))
	for _, c := range s.coords {
		g, ok := gradient[c]
		if !ok {
			return fmt.Errorf("%w: gradient missing coordinate %d", ErrInvalidSpec, c)
		}
		sum, err := checkAdd(g, s.residual[c])
		if err != nil {
			return err
		}
		input[c] = sum
	}

	st := &senderStep{
		id:        step,
		attempt:   s.attempt + 1,
		plan:      plan.clone(),
		spec:      spec,
		input:     input,
		tentative: make(map[Coord]int64, len(s.coords)),
		messages:  make(map[BucketID]SparseMessage, len(plan.Buckets)),
		confirmed: make(map[BucketID]struct{}, len(plan.Buckets)),
	}

	// Quantize every coordinate once. Quantization error is retained even for
	// coordinates that are not sent, so it re-enters the residual.
	quantized := make(map[Coord]int64, len(s.coords))
	for _, c := range s.coords {
		q, err := quantize(input[c], spec)
		if err != nil {
			return err
		}
		quantized[c] = q
	}

	for _, b := range plan.Buckets {
		candidates := make([]CoordValue, 0, len(b.Coords))
		for _, c := range b.Coords {
			candidates = append(candidates, CoordValue{Coord: c, Value: quantized[c]})
		}
		sent := selectSparse(candidates, spec.Keep)
		full := make([]CoordValue, 0, len(b.Coords))
		for _, c := range b.Coords {
			full = append(full, CoordValue{Coord: c, Value: quantized[c]})
		}
		sort.Slice(full, func(i, j int) bool { return full[i].Coord < full[j].Coord })
		msg := SparseMessage{
			Step:      step,
			Source:    s.source,
			Bucket:    b.ID,
			Attempt:   st.attempt,
			Layout:    plan.Layout,
			Plan:      plan.ID,
			Entries:   append([]CoordValue(nil), sent...),
			Quantized: full,
		}
		msg.Checksum = msg.ComputeChecksum()
		st.messages[b.ID] = msg
		st.order = append(st.order, b.ID)
	}

	// Tentative residual: unsent values keep their full quantized value, sent
	// ones keep only quantization error.
	for _, c := range s.coords {
		bid, _ := plan.BucketOf(c)
		sentValue := int64(0)
		for _, e := range st.messages[bid].Entries {
			if e.Coord == c {
				sentValue = e.Value
				break
			}
		}
		rest, err := checkSub(input[c], sentValue)
		if err != nil {
			return err
		}
		st.tentative[c] = rest
	}

	s.attempt = st.attempt
	s.layouts[plan.Layout] = plan.fingerprint()
	s.pending[step] = st
	s.history = append(s.history, step)
	s.recordBegin(st, gradient)
	return nil
}

func (s *Sender) hasHistoryLocked(step StepID) bool {
	for _, id := range s.history {
		if id == step {
			return true
		}
	}
	return false
}

// Packets returns copied packets for every bucket, in plan order, without
// consuming them. It works for an open step, including a step recovered from a
// journal, which is what resend-after-recovery uses.
func (s *Sender) Packets(step StepID) ([]SparseMessage, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	st, ok := s.pending[step]
	if !ok {
		if _, done := s.settled[step]; done {
			return nil, ErrStepSettled
		}
		if _, done := s.voided[step]; done {
			return nil, ErrStepVoided
		}
		return nil, ErrUnknownStep
	}
	out := make([]SparseMessage, 0, len(st.order))
	for _, id := range st.order {
		out = append(out, st.messages[id].clone())
	}
	return out, nil
}

// Packet returns one bucket packet without consuming it.
func (s *Sender) Packet(step StepID, bucket BucketID) (SparseMessage, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	st, ok := s.pending[step]
	if !ok {
		if _, done := s.settled[step]; done {
			return SparseMessage{}, ErrStepSettled
		}
		if _, done := s.voided[step]; done {
			return SparseMessage{}, ErrStepVoided
		}
		return SparseMessage{}, ErrUnknownStep
	}
	msg, ok := st.messages[bucket]
	if !ok {
		return SparseMessage{}, ErrUnknownGroup
	}
	return msg.clone(), nil
}

// Ack is a bucket confirmation about one message.
type Ack struct {
	Step    StepID
	Source  SourceID
	Bucket  BucketID
	Attempt uint64
	Layout  LayoutID
	Plan    PlanID
}

// AckFor builds the acknowledgement for a delivered message.
func AckFor(m SparseMessage) Ack {
	return Ack{
		Step:    m.Step,
		Source:  m.Source,
		Bucket:  m.Bucket,
		Attempt: m.Attempt,
		Layout:  m.Layout,
		Plan:    m.Plan,
	}
}

// Confirm applies one bucket acknowledgement to a step. It returns an outcome
// rather than an error for the expected stale/duplicate cases, so a late ack
// can never double-write the residual. A wrong-source ack also returns
// ErrWrongSource as an error.
func (s *Sender) Confirm(ack Ack) (ConfirmOutcome, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if ack.Source != s.source {
		return ConfirmIgnored, ErrWrongSource
	}
	outcome := s.confirmLocked(ack)
	s.recordConfirm(ack, outcome)
	return outcome, nil
}

func (s *Sender) confirmLocked(ack Ack) ConfirmOutcome {
	if _, ok := s.settled[ack.Step]; ok {
		return ConfirmDuplicate
	}
	if _, ok := s.voided[ack.Step]; ok {
		return ConfirmIgnored
	}
	st, ok := s.pending[ack.Step]
	if !ok {
		return ConfirmIgnored
	}
	if ack.Attempt != st.attempt ||
		ack.Layout != st.plan.Layout ||
		ack.Plan != st.plan.ID {
		return ConfirmIgnored
	}
	if _, ok := st.messages[ack.Bucket]; !ok {
		return ConfirmIgnored
	}
	if _, ok := st.confirmed[ack.Bucket]; ok {
		return ConfirmDuplicate
	}
	st.confirmed[ack.Bucket] = struct{}{}
	if len(st.confirmed) < len(st.order) {
		return ConfirmRecorded
	}

	// All buckets confirmed: commit the tentative residual exactly once. The
	// step is removed from pending before status is observable as settled.
	for c, v := range st.tentative {
		s.residual[c] = v
	}
	delete(s.pending, ack.Step)
	s.settled[ack.Step] = make(map[BucketID]struct{}, len(st.confirmed))
	for id := range st.confirmed {
		s.settled[ack.Step][id] = struct{}{}
	}
	return ConfirmSettled
}

// Abort abandons a step. Its residual is not committed; later confirms and
// packets for the step are ignored. Abort is idempotent.
func (s *Sender) Abort(step StepID) StepStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.settled[step]; ok {
		return StepSettled
	}
	if _, ok := s.voided[step]; ok {
		return StepVoided
	}
	if _, ok := s.pending[step]; ok {
		delete(s.pending, step)
		s.voided[step] = struct{}{}
		s.recordAbort(step)
		return StepVoided
	}
	return StepOpen
}
