// Package scaler implements a deterministic scale controller for a workload
// that consumes a queue.
//
// The controller is fed samples of the current queue backlog. For every valid
// sample it derives a replica target by dividing the backlog by the per-replica
// target and rounding up, then clamping the result into the configured replica
// bounds. Scale-up is immediate and counts replicas that were only requested
// (pending) towards the target. Scale-down is conservative: it follows the
// highest target observed among valid samples inside the last
// [ScaleDownWindow], and a sample that is exactly ScaleDownWindow old has
// already left the window. A sample without a backlog measurement keeps the
// current scale.
//
// Replica identities are stable, monotonically increasing ("replica-1",
// "replica-2", ...) and are never reused. Replayed samples and duplicate
// acknowledgements are idempotent. A failed create releases its reservation.
// Scale-down first cancels the most recently requested pending replicas and
// then terminates ready replicas in ascending replica-number order. Replicas
// that already received a termination intent take no further part in
// computation.
package scaler

import (
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

// ScaleDownWindow is the look-back window used to pick the scale-down target.
// Samples whose timestamp equals now-ScaleDownWindow are outside the window.
const ScaleDownWindow = 60 * time.Second

// Sentinel values reported in Result when no measurement / no window data is
// available.
const noTarget = -1

var (
	// ErrInvalidSpec is returned when the replica bounds or the per-replica
	// target are not usable.
	ErrInvalidSpec = errors.New("scaler: invalid spec")
	// ErrMissingSampleID is returned when a sample does not carry an identity,
	// which makes idempotent replay impossible.
	ErrMissingSampleID = errors.New("scaler: sample id is required")
	// ErrInvalidBacklog is returned for negative backlog measurements.
	ErrInvalidBacklog = errors.New("scaler: backlog must be non-negative")
	// ErrUnknownReplica is returned when an acknowledgement or failure report
	// names a replica the controller does not track.
	ErrUnknownReplica = errors.New("scaler: unknown replica")
	// ErrInvalidTransition is returned when a replica cannot perform the
	// requested transition in its current state.
	ErrInvalidTransition = errors.New("scaler: invalid replica transition")
)

// Spec configures the controller bounds and the per-replica backlog target.
type Spec struct {
	// MinReplicas is the lower bound applied to every computed target.
	MinReplicas int
	// MaxReplicas is the upper bound applied to every computed target.
	MaxReplicas int
	// TargetPerReplica is the backlog one replica is expected to handle. It
	// must be positive.
	TargetPerReplica int
}

// Validate reports whether the spec can be used.
func (s Spec) Validate() error {
	if s.TargetPerReplica <= 0 {
		return fmt.Errorf("%w: TargetPerReplica must be positive, got %d", ErrInvalidSpec, s.TargetPerReplica)
	}
	if s.MinReplicas < 0 {
		return fmt.Errorf("%w: MinReplicas must be >= 0, got %d", ErrInvalidSpec, s.MinReplicas)
	}
	if s.MaxReplicas < s.MinReplicas {
		return fmt.Errorf("%w: MaxReplicas (%d) must be >= MinReplicas (%d)", ErrInvalidSpec, s.MaxReplicas, s.MinReplicas)
	}
	return nil
}

// ReplicaState is the lifecycle state of one replica.
type ReplicaState string

const (
	// ReplicaPending means a create intent was issued but readiness has not
	// been acknowledged yet.
	ReplicaPending ReplicaState = "pending"
	// ReplicaReady means the replica is serving.
	ReplicaReady ReplicaState = "ready"
	// ReplicaTerminating means a termination intent was issued.
	ReplicaTerminating ReplicaState = "terminating"
)

// ActionKind enumerates the actions recorded by the controller.
type ActionKind string

const (
	// ActionCreate is a create intent for a new replica.
	ActionCreate ActionKind = "create"
	// ActionCancel releases a replica reservation without terminating a
	// running replica (create failure or a scale-down of pending replicas).
	ActionCancel ActionKind = "cancel_create"
	// ActionTerminate is a termination intent for a ready replica.
	ActionTerminate ActionKind = "terminate"
)

// Action is one entry of the append-only action log.
type Action struct {
	Kind       ActionKind
	ReplicaID  string
	ReplicaNum int
	At         time.Time
	SampleID   string
	Reason     string
}

// Sample is one backlog observation. A nil Backlog means the measurement is
// missing and the current scale must be maintained.
type Sample struct {
	// ID identifies the sample and is required for idempotent replay.
	ID string
	// At is the observation time. The zero value is interpreted as the
	// controller's current time.
	At time.Time
	// Backlog is the queue depth, if it could be measured.
	Backlog *int
}

// ReplicaView is a read-only snapshot of a tracked replica.
type ReplicaView struct {
	ID          string
	Number      int
	State       ReplicaState
	RequestedAt time.Time
}

// Counts summarises the tracked replicas. Terminating replicas are not part of
// Assigned because they already left the computation.
type Counts struct {
	Pending     int
	Ready       int
	Terminating int
	Assigned    int
}

// Result is the outcome of applying one sample.
type Result struct {
	SampleID string
	// Duplicate is true when the sample ID was already processed; in that case
	// no state was changed and no actions were emitted.
	Duplicate bool
	// Observed is true when the sample carried a backlog measurement that is
	// still inside the scale-down window and was therefore acted on.
	Observed bool
	// Stale is true when the sample carried a backlog measurement but its
	// timestamp is already outside the scale-down window. Such a sample cannot
	// influence the scale and is not recorded.
	Stale bool
	// Target is the per-sample target after ceiling and clamping, or -1 when
	// the sample had no measurement (or was stale).
	Target int
	// WindowMax is the highest target among valid samples still inside the
	// scale-down window, or -1 when the window is empty.
	WindowMax int
	// WindowSamples is the number of valid samples inside the window.
	WindowSamples int
	// Assigned is the number of replicas counted after the actions ran
	// (ready plus pending, excluding terminating).
	Assigned int
	// Actions lists the actions caused by this sample only.
	Actions []Action
}

type replica struct {
	id          string
	num         int
	state       ReplicaState
	requestedAt time.Time
}

type windowSample struct {
	at     time.Time
	target int
}

// Controller is the scale controller. It is safe for concurrent use.
type Controller struct {
	mu       sync.Mutex
	spec     Spec
	now      func() time.Time
	nextNum  int
	replicas map[string]*replica
	samples  []windowSample
	seen     map[string]struct{}
	log      []Action
}

// Option customises a Controller.
type Option func(*Controller)

// WithClock injects the time source. It is primarily useful for tests.
func WithClock(now func() time.Time) Option {
	return func(c *Controller) {
		if now != nil {
			c.now = now
		}
	}
}

// New validates the spec and returns a ready controller.
func New(spec Spec, opts ...Option) (*Controller, error) {
	if err := spec.Validate(); err != nil {
		return nil, err
	}
	c := &Controller{
		spec:     spec,
		now:      time.Now,
		nextNum:  1,
		replicas: make(map[string]*replica),
		seen:     make(map[string]struct{}),
	}
	for _, opt := range opts {
		opt(c)
	}
	return c, nil
}

// Apply feeds one backlog sample to the controller.
//
// A sample with a backlog measurement is recorded in the scale-down window and
// may trigger scale-up or scale-down. A sample without a measurement never
// changes the replica count. A replayed sample ID is acknowledged as a
// duplicate and changes nothing.
func (c *Controller) Apply(s Sample) (Result, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if s.ID == "" {
		return Result{}, fmt.Errorf("%w: empty sample id", ErrMissingSampleID)
	}

	now := c.now()
	if s.At.IsZero() {
		s.At = now
	}

	if _, ok := c.seen[s.ID]; ok {
		windowMax, windowSamples := c.windowMaxAtLocked(now)
		return Result{
			SampleID:      s.ID,
			Duplicate:     true,
			Target:        noTarget,
			WindowMax:     windowMax,
			WindowSamples: windowSamples,
			Assigned:      c.assignedLocked(),
		}, nil
	}

	if s.Backlog != nil && *s.Backlog < 0 {
		return Result{}, fmt.Errorf("%w: got %d", ErrInvalidBacklog, *s.Backlog)
	}

	c.seen[s.ID] = struct{}{}

	res := Result{SampleID: s.ID, Target: noTarget, WindowMax: noTarget}
	// A measurement that is already older than the window cannot influence any
	// decision: it would be pruned immediately. Report its target for
	// diagnostics but do not record it and do not act on it.
	fresh := s.At.After(now.Add(-ScaleDownWindow))
	if s.Backlog != nil {
		res.Observed = true
		res.Target = c.targetLocked(*s.Backlog)
		if fresh {
			c.samples = append(c.samples, windowSample{at: s.At, target: res.Target})
		} else {
			res.Target = noTarget
			res.Observed = false
			res.Stale = true
		}
	}

	c.pruneLocked(now)
	res.WindowMax, res.WindowSamples = c.windowMaxLocked()

	if res.Observed {
		assigned := c.assignedLocked()
		switch {
		case res.Target > assigned:
			actions := make([]Action, 0, res.Target-assigned)
			for i := 0; i < res.Target-assigned; i++ {
				actions = append(actions, c.createLocked(now, s.ID))
			}
			c.log = append(c.log, actions...)
			res.Actions = actions
		case res.WindowMax >= 0 && assigned > res.WindowMax:
			actions := c.scaleDownLocked(assigned, res.WindowMax, now, s.ID)
			c.log = append(c.log, actions...)
			res.Actions = actions
		}
	}

	res.Assigned = c.assignedLocked()
	return res, nil
}

// MarkReady acknowledges that a replica is ready to serve. Duplicate
// acknowledgements are harmless. An acknowledgement for a replica that already
// received a termination intent is ignored because the replica has left the
// computation.
func (c *Controller) MarkReady(replicaID string) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	r, ok := c.replicas[replicaID]
	if !ok {
		return fmt.Errorf("%w: %q", ErrUnknownReplica, replicaID)
	}
	if r.state == ReplicaReady || r.state == ReplicaTerminating {
		return nil
	}
	r.state = ReplicaReady
	return nil
}

// FailCreate reports that the infrastructure could not create a pending
// replica. The reservation is released, the replica is removed, and the action
// is recorded. The controller does not retry by itself; the next sample
// reconciles again and will allocate a fresh, never reused replica ID.
func (c *Controller) FailCreate(replicaID string) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	r, ok := c.replicas[replicaID]
	if !ok {
		return fmt.Errorf("%w: %q", ErrUnknownReplica, replicaID)
	}
	if r.state != ReplicaPending {
		return fmt.Errorf("%w: replica %q is %s", ErrInvalidTransition, replicaID, r.state)
	}
	delete(c.replicas, replicaID)
	c.log = append(c.log, Action{
		Kind:       ActionCancel,
		ReplicaID:  r.id,
		ReplicaNum: r.num,
		At:         c.now(),
		Reason:     "create_failed",
	})
	return nil
}

// Replicas returns a snapshot sorted by replica number.
func (c *Controller) Replicas() []ReplicaView {
	c.mu.Lock()
	defer c.mu.Unlock()

	out := make([]ReplicaView, 0, len(c.replicas))
	for _, r := range c.replicas {
		out = append(out, ReplicaView{ID: r.id, Number: r.num, State: r.state, RequestedAt: r.requestedAt})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Number < out[j].Number })
	return out
}

// Counts returns the current replica summary.
func (c *Controller) Counts() Counts {
	c.mu.Lock()
	defer c.mu.Unlock()

	var counts Counts
	for _, r := range c.replicas {
		switch r.state {
		case ReplicaPending:
			counts.Pending++
		case ReplicaReady:
			counts.Ready++
		case ReplicaTerminating:
			counts.Terminating++
		}
	}
	counts.Assigned = counts.Ready + counts.Pending
	return counts
}

// Actions returns a copy of the append-only action log.
func (c *Controller) Actions() []Action {
	c.mu.Lock()
	defer c.mu.Unlock()

	out := make([]Action, len(c.log))
	copy(out, c.log)
	return out
}

func (c *Controller) targetLocked(backlog int) int {
	target := ceilDiv(backlog, c.spec.TargetPerReplica)
	if target < c.spec.MinReplicas {
		target = c.spec.MinReplicas
	}
	if target > c.spec.MaxReplicas {
		target = c.spec.MaxReplicas
	}
	return target
}

func ceilDiv(a, b int) int {
	if a <= 0 {
		return 0
	}
	return 1 + (a-1)/b
}

func (c *Controller) pruneLocked(now time.Time) {
	cutoff := now.Add(-ScaleDownWindow)
	kept := c.samples[:0]
	for _, s := range c.samples {
		if s.at.After(cutoff) {
			kept = append(kept, s)
		}
	}
	c.samples = kept
}

func (c *Controller) windowMaxLocked() (int, int) {
	return c.windowMaxAtLocked(c.now())
}

// windowMaxAtLocked reports the highest target and the number of samples that
// are strictly newer than now-ScaleDownWindow. It does not mutate the buffer,
// so it is safe to call for duplicate samples that must change no state.
func (c *Controller) windowMaxAtLocked(now time.Time) (int, int) {
	cutoff := now.Add(-ScaleDownWindow)
	best := noTarget
	count := 0
	for _, s := range c.samples {
		if !s.at.After(cutoff) {
			continue
		}
		count++
		if best == noTarget || s.target > best {
			best = s.target
		}
	}
	return best, count
}

func (c *Controller) assignedLocked() int {
	n := 0
	for _, r := range c.replicas {
		if r.state != ReplicaTerminating {
			n++
		}
	}
	return n
}

func (c *Controller) createLocked(now time.Time, sampleID string) Action {
	num := c.nextNum
	c.nextNum++
	id := fmt.Sprintf("replica-%d", num)
	c.replicas[id] = &replica{
		id:          id,
		num:         num,
		state:       ReplicaPending,
		requestedAt: now,
	}
	return Action{Kind: ActionCreate, ReplicaID: id, ReplicaNum: num, At: now, SampleID: sampleID}
}

func (c *Controller) scaleDownLocked(assigned, want int, now time.Time, sampleID string) []Action {
	deficit := assigned - want
	actions := make([]Action, 0, deficit)

	// Cancel the most recently requested pending replicas first. Creation order
	// is captured by requestedAt and, as a deterministic tie-breaker, by the
	// strictly increasing replica number.
	pending := c.byStateLocked(ReplicaPending)
	sort.Slice(pending, func(i, j int) bool {
		if !pending[i].requestedAt.Equal(pending[j].requestedAt) {
			return pending[i].requestedAt.After(pending[j].requestedAt)
		}
		return pending[i].num > pending[j].num
	})
	for _, r := range pending {
		if deficit == 0 {
			break
		}
		delete(c.replicas, r.id)
		actions = append(actions, Action{
			Kind:       ActionCancel,
			ReplicaID:  r.id,
			ReplicaNum: r.num,
			At:         now,
			SampleID:   sampleID,
			Reason:     "scale_down",
		})
		deficit--
	}

	if deficit > 0 {
		ready := c.byStateLocked(ReplicaReady)
		sort.Slice(ready, func(i, j int) bool { return ready[i].num < ready[j].num })
		for _, r := range ready {
			if deficit == 0 {
				break
			}
			r.state = ReplicaTerminating
			actions = append(actions, Action{
				Kind:       ActionTerminate,
				ReplicaID:  r.id,
				ReplicaNum: r.num,
				At:         now,
				SampleID:   sampleID,
				Reason:     "scale_down",
			})
			deficit--
		}
	}

	return actions
}

func (c *Controller) byStateLocked(state ReplicaState) []*replica {
	out := make([]*replica, 0, len(c.replicas))
	for _, r := range c.replicas {
		if r.state == state {
			out = append(out, r)
		}
	}
	return out
}
