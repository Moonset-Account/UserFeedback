// Package groupsnapshot implements a group snapshot state machine for an
// application that writes an indexed write stream to several block volumes.
//
// The application owns a shared, monotonically increasing write sequence.
// Every write declares which volumes it touches. A snapshot request pins one
// point in that sequence: every volume involved in a write up to the point
// must confirm that its relevant writes are durable and must keep the root
// corresponding to that point. Once every required volume has acknowledged,
// the whole group becomes recoverable together.
//
// A volume that has acknowledged early is free to accept later writes; queries
// against the snapshot only ever see the roots fixed at the snapshot point.
//
// Cancellation and retry are idempotent. A retry of an existing request
// returns the state already determined by that request, and an acknowledgment
// that arrives after the request was cancelled is retained under the original
// request's attribution instead of being applied to any other request.
package groupsnapshot

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

// VolumeID identifies a block volume.
type VolumeID string

// Seq is a shared, monotonically increasing write sequence number.
type Seq uint64

// Root is the immutable root pointer a volume keeps for a snapshot point.
type Root string

// State is the lifecycle state of a group snapshot request.
type State int

const (
	// StatePending means the request exists but no required volume has
	// acknowledged yet.
	StatePending State = iota
	// StateAcknowledging means at least one required volume has
	// acknowledged; the group cannot be recovered yet.
	StateAcknowledging
	// StateComplete means every required volume has acknowledged at the
	// fixed point; the whole group is recoverable.
	StateComplete
	// StateCancelled means the requester cancelled the request. The state
	// is terminal: late acknowledgments are recorded against this request
	// with their original attribution but never promote it to Complete.
	StateCancelled
)

// String returns a stable textual name for the state.
func (s State) String() string {
	switch s {
	case StatePending:
		return "pending"
	case StateAcknowledging:
		return "acknowledging"
	case StateComplete:
		return "complete"
	case StateCancelled:
		return "cancelled"
	default:
		return fmt.Sprintf("unknown-state-%d", int(s))
	}
}

// LateAckReason explains why an acknowledgment was recorded as late.
type LateAckReason string

const (
	// LateAfterCancellation marks an acknowledgment from a required volume
	// that first reached the coordinator after its request had been
	// cancelled.
	LateAfterCancellation LateAckReason = "late-after-cancellation"
)

// Sentinel errors. Callers should use errors.Is rather than comparing strings.
var (
	// ErrRequestNotFound is returned when no request carries the given ID.
	ErrRequestNotFound = errors.New("groupsnapshot: request not found")
	// ErrVolumeNotRequired is returned when a volume that is not part of
	// the snapshot's required volume set acknowledges the request.
	ErrVolumeNotRequired = errors.New("groupsnapshot: volume not required by request")
	// ErrEmptyRoot is returned when an acknowledgment carries no root.
	ErrEmptyRoot = errors.New("groupsnapshot: acknowledgment root is empty")
	// ErrConflictingRoot is returned when a volume re-acknowledges with a
	// root different from the one already recorded. The first root wins and
	// remains fixed.
	ErrConflictingRoot = errors.New("groupsnapshot: conflicting acknowledgment root")
	// ErrNotDurable is returned when a volume acknowledges before all of
	// its relevant writes up to the snapshot point have been marked
	// persisted.
	ErrNotDurable = errors.New("groupsnapshot: relevant writes not yet durable")
	// ErrRequestCancelled is returned when an acknowledgment targets a
	// request that has already been cancelled. When the acknowledgment is
	// itself valid, it is retained as a late acknowledgment before this
	// error is returned.
	ErrRequestCancelled = errors.New("groupsnapshot: request already cancelled")
	// ErrSnapshotNotRecoverable is returned by Recover while the request is
	// not in StateComplete.
	ErrSnapshotNotRecoverable = errors.New("groupsnapshot: snapshot not recoverable")
	// ErrSequenceOutOfOrder is returned when a registered write does not
	// strictly extend the shared sequence.
	ErrSequenceOutOfOrder = errors.New("groupsnapshot: write sequence out of order")
	// ErrEmptyVolumeSet is returned when a write declares no volumes.
	ErrEmptyVolumeSet = errors.New("groupsnapshot: write involves no volumes")
	// ErrWatermarkRegression is returned when a durability watermark moves
	// backwards.
	ErrWatermarkRegression = errors.New("groupsnapshot: durability watermark regression")
)

// SnapshotView is an immutable copy of a request's state as observed at one
// moment. Roots never change once recorded.
type SnapshotView struct {
	RequestID       string
	Point           Seq
	State           State
	RequiredVolumes []VolumeID
	Roots           map[VolumeID]Root
	CreatedAt       time.Time
	CompletedAt     time.Time
	CancelledAt     time.Time
}

// LateAck is an acknowledgment that arrived after its request had reached a
// terminal state. It keeps the original request attribution.
type LateAck struct {
	RequestID string
	Volume    VolumeID
	Root      Root
	Reason    LateAckReason
	At        time.Time
}

// Coordinator is the group snapshot state machine. It is safe for concurrent
// use by multiple goroutines.
type Coordinator struct {
	mu sync.Mutex

	// highestSeq is the highest sequence ever registered via AppendWrite.
	highestSeq Seq
	hasWrite   bool
	// writeVolumes maps a sequence number to the volumes that write touched.
	writeVolumes map[Seq][]VolumeID
	// durable holds, per volume, the highest sequence whose relevant
	// writes the volume has confirmed as persisted.
	durable map[VolumeID]Seq

	requests map[string]*request
}

type request struct {
	id          string
	point       Seq
	required    map[VolumeID]struct{}
	roots       map[VolumeID]Root
	state       State
	createdAt   time.Time
	completedAt time.Time
	cancelledAt time.Time
	lateAcks    []LateAck

	// done is closed exactly once, when the request reaches a terminal
	// state (Complete or Cancelled).
	done chan struct{}
}

// NewCoordinator creates an empty state machine.
func NewCoordinator() *Coordinator {
	return &Coordinator{
		writeVolumes: make(map[Seq][]VolumeID),
		durable:      make(map[VolumeID]Seq),
		requests:     make(map[string]*request),
	}
}

// AppendWrite registers an application write at seq that touches the given
// volumes. Sequence numbers must strictly increase; a write must touch at
// least one volume.
func (c *Coordinator) AppendWrite(seq Seq, volumes []VolumeID) error {
	if len(volumes) == 0 {
		return ErrEmptyVolumeSet
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.hasWrite && seq <= c.highestSeq {
		return fmt.Errorf("%w: seq %d must be greater than %d",
			ErrSequenceOutOfOrder, seq, c.highestSeq)
	}
	copied := append([]VolumeID(nil), volumes...)
	c.writeVolumes[seq] = copied
	c.highestSeq = seq
	c.hasWrite = true
	return nil
}

// MarkPersisted records that volume has persisted all of its relevant writes
// up to and including seq. Watermarks are monotonic; moving one backwards is
// rejected. Marking the same watermark twice is an idempotent no-op.
func (c *Coordinator) MarkPersisted(volume VolumeID, seq Seq) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if current, ok := c.durable[volume]; ok && seq < current {
		return fmt.Errorf("%w: volume %s at %d, got %d",
			ErrWatermarkRegression, volume, current, seq)
	}
	c.durable[volume] = seq
	return nil
}

// RequestSnapshot pins a group snapshot at point under reqID. The required
// volume set is derived once, at request time, from every registered write up
// to and including point.
//
// Retrying with the same reqID is idempotent: the existing request is returned
// with alreadyExisted == true and its current state, even if it has been
// cancelled. A retry that names a different point is rejected and leaves the
// original request untouched.
func (c *Coordinator) RequestSnapshot(reqID string, point Seq) (view SnapshotView, alreadyExisted bool, err error) {
	if reqID == "" {
		return SnapshotView{}, false, errors.New("groupsnapshot: empty request id")
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	if existing, ok := c.requests[reqID]; ok {
		if existing.point != point {
			return SnapshotView{}, true,
				fmt.Errorf("groupsnapshot: request %s already pins point %d, cannot repin to %d",
					reqID, existing.point, point)
		}
		return existing.snapshotView(), true, nil
	}

	required := c.requiredVolumesLocked(point)
	r := &request{
		id:        reqID,
		point:     point,
		required:  required,
		roots:     make(map[VolumeID]Root),
		state:     StatePending,
		createdAt: time.Now().UTC(),
		done:      make(chan struct{}),
	}
	// A point with no relevant writes has nothing to wait for: the empty
	// group is immediately recoverable.
	if len(required) == 0 {
		r.state = StateComplete
		r.completedAt = r.createdAt
		close(r.done)
	}
	c.requests[reqID] = r
	return r.snapshotView(), false, nil
}

// Ack records volume's acknowledgment of reqID with the root fixed at the
// request's point.
//
// The volume must be part of the request's required set, must not have
// persisted a conflicting root, and must already have marked all of its
// relevant writes up to the point as durable. Re-acknowledging with the same
// root is an idempotent no-op. When the last required volume acknowledges, the
// request transitions to StateComplete and the group becomes recoverable.
//
// A first-time acknowledgment that arrives after cancellation passes the same
// validation, is retained as a late acknowledgment under reqID, and returns
// ErrRequestCancelled. It is never applied to any other request.
func (c *Coordinator) Ack(reqID string, volume VolumeID, root Root) error {
	if root == "" {
		return ErrEmptyRoot
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	r, ok := c.requests[reqID]
	if !ok {
		return fmt.Errorf("%w: %s", ErrRequestNotFound, reqID)
	}
	if _, ok := r.required[volume]; !ok {
		return fmt.Errorf("%w: volume %s request %s", ErrVolumeNotRequired, volume, reqID)
	}

	switch r.state {
	case StatePending, StateAcknowledging:
		if existing, dup := r.roots[volume]; dup {
			if existing != root {
				return fmt.Errorf("%w: volume %s recorded %q, got %q",
					ErrConflictingRoot, volume, existing, root)
			}
			return nil
		}
		if err := c.checkDurableLocked(r, volume); err != nil {
			return err
		}
		r.roots[volume] = root
		if r.state == StatePending {
			r.state = StateAcknowledging
		}
		if len(r.roots) == len(r.required) {
			r.state = StateComplete
			r.completedAt = time.Now().UTC()
			close(r.done)
		}
		return nil

	case StateComplete:
		// Every required volume already acknowledged, so this can only be a
		// retransmission of the recorded root.
		existing := r.roots[volume]
		if existing != root {
			return fmt.Errorf("%w: volume %s recorded %q, got %q",
				ErrConflictingRoot, volume, existing, root)
		}
		return nil

	case StateCancelled:
		if existing, dup := r.roots[volume]; dup {
			if existing != root {
				return fmt.Errorf("%w: volume %s recorded %q, got %q",
					ErrConflictingRoot, volume, existing, root)
			}
			return nil
		}
		// A retransmission of an already retained late acknowledgment is
		// idempotent: keep one record with the original attribution.
		for _, la := range r.lateAcks {
			if la.Volume == volume {
				if la.Root != root {
					return fmt.Errorf("%w: volume %s recorded %q, got %q",
						ErrConflictingRoot, volume, la.Root, root)
				}
				return nil
			}
		}
		if err := c.checkDurableLocked(r, volume); err != nil {
			return err
		}
		r.lateAcks = append(r.lateAcks, LateAck{
			RequestID: reqID,
			Volume:    volume,
			Root:      root,
			Reason:    LateAfterCancellation,
			At:        time.Now().UTC(),
		})
		return fmt.Errorf("%w: %s", ErrRequestCancelled, reqID)

	default:
		return fmt.Errorf("groupsnapshot: request %s in unknown state %d", reqID, r.state)
	}
}

// Cancel cancels reqID. Cancellation is idempotent and cannot undo
// completion: an already Complete request stays Complete, and cancelling
// twice is a no-op.
func (c *Coordinator) Cancel(reqID string) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.requests[reqID]
	if !ok {
		return fmt.Errorf("%w: %s", ErrRequestNotFound, reqID)
	}
	if r.state == StatePending || r.state == StateAcknowledging {
		r.state = StateCancelled
		r.cancelledAt = time.Now().UTC()
		close(r.done)
	}
	return nil
}

// Snapshot returns the current immutable view of reqID.
func (c *Coordinator) Snapshot(reqID string) (SnapshotView, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.requests[reqID]
	if !ok {
		return SnapshotView{}, fmt.Errorf("%w: %s", ErrRequestNotFound, reqID)
	}
	return r.snapshotView(), nil
}

// Recover returns the fixed roots of a complete snapshot. It fails with
// ErrSnapshotNotRecoverable while the request is pending, acknowledging, or
// cancelled.
func (c *Coordinator) Recover(reqID string) (map[VolumeID]Root, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.requests[reqID]
	if !ok {
		return nil, fmt.Errorf("%w: %s", ErrRequestNotFound, reqID)
	}
	if r.state != StateComplete {
		return nil, fmt.Errorf("%w: request %s is %s",
			ErrSnapshotNotRecoverable, reqID, r.state)
	}
	return copyRoots(r.roots), nil
}

// LateAcks returns copies of the late acknowledgments recorded for reqID.
func (c *Coordinator) LateAcks(reqID string) ([]LateAck, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	r, ok := c.requests[reqID]
	if !ok {
		return nil, fmt.Errorf("%w: %s", ErrRequestNotFound, reqID)
	}
	out := make([]LateAck, len(r.lateAcks))
	copy(out, r.lateAcks)
	return out, nil
}

// Wait blocks until reqID reaches a terminal state (Complete or Cancelled) or
// ctx is done. On timeout/cancellation it returns the most recent view along
// with ctx.Err(); the request itself is not cancelled by a context expiry.
func (c *Coordinator) Wait(ctx context.Context, reqID string) (SnapshotView, error) {
	c.mu.Lock()
	r, ok := c.requests[reqID]
	if !ok {
		c.mu.Unlock()
		return SnapshotView{}, fmt.Errorf("%w: %s", ErrRequestNotFound, reqID)
	}
	done := r.done
	c.mu.Unlock()

	select {
	case <-done:
	case <-ctx.Done():
		c.mu.Lock()
		view := r.snapshotView()
		c.mu.Unlock()
		return view, ctx.Err()
	}

	c.mu.Lock()
	view := r.snapshotView()
	c.mu.Unlock()
	return view, nil
}

// requiredVolumesLocked derives the frozen required volume set for point from
// every registered write with seq <= point. Caller holds c.mu.
func (c *Coordinator) requiredVolumesLocked(point Seq) map[VolumeID]struct{} {
	required := make(map[VolumeID]struct{})
	for seq, volumes := range c.writeVolumes {
		if seq > point {
			continue
		}
		for _, v := range volumes {
			required[v] = struct{}{}
		}
	}
	return required
}

// relevantTailLocked returns the highest sequence <= point in which volume
// participated. Caller holds c.mu.
func (c *Coordinator) relevantTailLocked(volume VolumeID, point Seq) (Seq, bool) {
	var tail Seq
	found := false
	for seq, volumes := range c.writeVolumes {
		if seq > point {
			continue
		}
		for _, v := range volumes {
			if v == volume {
				if !found || seq > tail {
					tail = seq
				}
				found = true
				break
			}
		}
	}
	return tail, found
}

// checkDurableLocked verifies that volume's relevant writes up to the request
// point have all been marked persisted. Caller holds c.mu.
func (c *Coordinator) checkDurableLocked(r *request, volume VolumeID) error {
	tail, ok := c.relevantTailLocked(volume, r.point)
	if !ok {
		// Required volumes are derived from relevant writes, so this
		// should be unreachable; fail closed rather than skip the gate.
		return fmt.Errorf("%w: volume %s has no registered write up to point %d",
			ErrNotDurable, volume, r.point)
	}
	if c.durable[volume] < tail {
		return fmt.Errorf("%w: volume %s durable to %d, needs %d",
			ErrNotDurable, volume, c.durable[volume], tail)
	}
	return nil
}

// snapshotView builds an immutable copy of the request. Caller holds c.mu.
func (r *request) snapshotView() SnapshotView {
	required := make([]VolumeID, 0, len(r.required))
	for v := range r.required {
		required = append(required, v)
	}
	sort.Slice(required, func(i, j int) bool { return required[i] < required[j] })
	return SnapshotView{
		RequestID:       r.id,
		Point:           r.point,
		State:           r.state,
		RequiredVolumes: required,
		Roots:           copyRoots(r.roots),
		CreatedAt:       r.createdAt,
		CompletedAt:     r.completedAt,
		CancelledAt:     r.cancelledAt,
	}
}

func copyRoots(in map[VolumeID]Root) map[VolumeID]Root {
	out := make(map[VolumeID]Root, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}
