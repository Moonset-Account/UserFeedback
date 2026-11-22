// Package rangedeleg implements a range-write delegation coordinator for a
// shared file service.
//
// Clients hold byte-range delegations and may cache writes inside the
// delegated range. Cached writes are flushed back to the coordinator as
// writeback segments. A segment is settled at most once for a given
// (delegation generation, commit id, offset, length) tuple, so a batch may be
// split, duplicated, or delivered out of order.
//
// When the file must shrink, Truncate first recalls every active delegation
// that intersects the region being discarded. While the recall is outstanding
// the truncation is not yet effective: readers still observe the old length
// and the old bytes. Writebacks received during the recall are split at the
// truncation point; bytes below it are preserved, bytes at or above it are
// discarded for this recall. Once every recalled delegation has confirmed,
// the truncation is applied and those delegation generations may no longer
// write. Extending the file afterwards exposes a freshly zeroed region, and a
// replayed old receipt cannot resurrect the discarded bytes.
//
// All state is persisted under the coordinator directory so a controller can
// be reopened and continue tracking outstanding recalls and truncations.
package rangedeleg

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// DelegationID identifies a delegation within a coordinator. It survives
// reopen.
type DelegationID uint64

// Generation is the generation counter of a delegation. Re-granting the same
// client and byte range after a revocation reuses the DelegationID and
// increments the generation, so stale attempts can be told apart.
type Generation uint64

// CommitID identifies a writeback batch sent by a client.
type CommitID uint64

// DelegationState is the lifecycle state of a delegation.
type DelegationState string

const (
	// DelegationActive means the client may cache and flush writes.
	DelegationActive DelegationState = "active"
	// DelegationRecalling means the server has asked the client to return the
	// delegation. Writes may still be flushed until the recall is confirmed.
	DelegationRecalling DelegationState = "recalling"
	// DelegationRevoked means the recall was confirmed (or the delegation was
	// otherwise retired). The generation must not write again, even if the
	// file is extended later.
	DelegationRevoked DelegationState = "revoked"
)

// Errors returned by the coordinator.
var (
	// ErrClosed is returned after Close has been called.
	ErrClosed = errors.New("rangedeleg: coordinator is closed")
	// ErrUnknownDelegation is returned when no delegation has the given ID.
	ErrUnknownDelegation = errors.New("rangedeleg: unknown delegation")
	// ErrStaleGeneration is returned when a receipt carries a generation that
	// is not the delegation's current generation.
	ErrStaleGeneration = errors.New("rangedeleg: stale delegation generation")
	// ErrDelegationRevoked is returned when a revoked generation attempts to
	// write. Its bytes must never be applied again.
	ErrDelegationRevoked = errors.New("rangedeleg: delegation generation is revoked")
	// ErrClientMismatch is returned when a client presents another client's
	// delegation.
	ErrClientMismatch = errors.New("rangedeleg: delegation belongs to another client")
	// ErrOutsideDelegation is returned when a writeback segment falls outside
	// the delegated byte range.
	ErrOutsideDelegation = errors.New("rangedeleg: writeback segment outside delegation")
	// ErrEmptyWrite is returned for a zero-length writeback segment.
	ErrEmptyWrite = errors.New("rangedeleg: empty writeback segment")
	// ErrConflict is returned when a previously settled segment is replayed
	// with different contents.
	ErrConflict = errors.New("rangedeleg: segment already settled with different contents")
	// ErrInvalidSize is returned for negative or non-growing sizes.
	ErrInvalidSize = errors.New("rangedeleg: invalid size")
	// ErrNotShrinking is returned when Truncate is asked for a size that is
	// not smaller than the current (effective) length.
	ErrNotShrinking = errors.New("rangedeleg: truncation target is not below the current length")
	// ErrTruncationOrder is returned when queued truncations are not strictly
	// decreasing.
	ErrTruncationOrder = errors.New("rangedeleg: truncation targets must be strictly decreasing")
	// ErrTruncationPending is returned when an operation cannot run while a
	// truncation is awaiting recall confirmations.
	ErrTruncationPending = errors.New("rangedeleg: truncation is pending recall confirmation")
	// ErrOutOfBounds is returned when a read exceeds the current length.
	ErrOutOfBounds = errors.New("rangedeleg: read out of bounds")
	// ErrUnknownTruncation is returned when a truncation sequence is unknown.
	ErrUnknownTruncation = errors.New("rangedeleg: unknown truncation")
)

// Range is a half-open byte interval [Off, Off+Len).
type Range struct {
	Off int64
	Len int64
}

// End returns the exclusive end offset of the range.
func (r Range) End() int64 { return r.Off + r.Len }

// Delegation is a snapshot of a granted range delegation.
type Delegation struct {
	ID         DelegationID
	Client     string
	Off        int64
	Len        int64
	Generation Generation
	State      DelegationState
}

// DelegationRef names a specific generation of a delegation, as presented by
// a client when flushing or confirming.
type DelegationRef struct {
	ID         DelegationID
	Generation Generation
}

// WritebackResult describes how a writeback segment was settled.
type WritebackResult struct {
	// Commit is the batch identifier carried by the segment.
	Commit CommitID
	// Accepted lists the byte ranges that were applied (or preserved because
	// they fall below a pending truncation point). Empty for a duplicate.
	Accepted []Range
	// Discarded lists the byte ranges that were dropped because they fall at
	// or above a pending truncation point. Empty for a duplicate.
	Discarded []Range
	// SplitAt is the truncation point at which the segment was split, or 0
	// when no split happened.
	SplitAt int64
	// Duplicate is true when this exact segment (delegation generation,
	// commit id, offset and length) had already been settled.
	Duplicate bool
	// TruncationSeq is the sequence number of the pending truncation that
	// caused any discard, or 0 if none.
	TruncationSeq uint64
}

// TruncationStatus reports the lifecycle of one shrink request.
type TruncationStatus struct {
	Seq       uint64
	Target    int64
	Pending   []DelegationRef
	Effective bool
	// EffectiveOrder is a monotonically increasing logical timestamp assigned
	// when the truncation took effect (0 while still pending).
	EffectiveOrder uint64
}

// TruncationHandle is returned by Truncate. It can be used to observe when the
// requested shrink takes effect, including after a reopen of the coordinator
// with the same sequence number.
type TruncationHandle struct {
	c      *Coordinator
	Seq    uint64
	Target int64
}

// Status returns the current status of this truncation.
func (h *TruncationHandle) Status() (TruncationStatus, bool) {
	return h.c.TruncationStatus(h.Seq)
}

// Wait blocks until the truncation takes effect or ctx is done.
func (h *TruncationHandle) Wait(ctx context.Context) (TruncationStatus, error) {
	return h.c.WaitTruncation(ctx, h.Seq)
}

const stateVersion = 1

type diskDelegation struct {
	ID         DelegationID    `json:"id"`
	Client     string          `json:"client"`
	Off        int64           `json:"off"`
	Len        int64           `json:"len"`
	Generation Generation      `json:"generation"`
	State      DelegationState `json:"state"`
}

type settledRecord struct {
	ID         DelegationID `json:"id"`
	Generation Generation   `json:"generation"`
	Commit     CommitID     `json:"commit"`
	Off        int64        `json:"off"`
	Len        int64        `json:"len"`
	Digest     string       `json:"digest"`
	Accepted   []Range      `json:"accepted,omitempty"`
	Discarded  []Range      `json:"discarded,omitempty"`
}

type diskTruncation struct {
	Seq            uint64          `json:"seq"`
	Target         int64           `json:"target"`
	Pending        []DelegationRef `json:"pending,omitempty"`
	Effective      bool            `json:"effective"`
	EffectiveOrder uint64          `json:"effective_order,omitempty"`
}

type diskState struct {
	Version        int                       `json:"version"`
	NextID         DelegationID              `json:"next_id"`
	Length         int64                     `json:"length"`
	Data           []byte                    `json:"data"`
	NextTruncSeq   uint64                    `json:"next_trunc_seq"`
	EffectiveOrder uint64                    `json:"effective_order"`
	Delegations    map[string]diskDelegation `json:"delegations,omitempty"`
	Settled        map[string]settledRecord  `json:"settled,omitempty"`
	Truncations    []diskTruncation          `json:"truncations,omitempty"`
}

func newDiskState() *diskState {
	return &diskState{
		Version:     stateVersion,
		NextID:      1,
		Delegations: make(map[string]diskDelegation),
		Settled:     make(map[string]settledRecord),
	}
}

func (s *diskState) clone() *diskState {
	n := &diskState{
		Version:        s.Version,
		NextID:         s.NextID,
		Length:         s.Length,
		NextTruncSeq:   s.NextTruncSeq,
		EffectiveOrder: s.EffectiveOrder,
		Delegations:    make(map[string]diskDelegation, len(s.Delegations)),
		Settled:        make(map[string]settledRecord, len(s.Settled)),
	}
	if s.Data != nil {
		n.Data = make([]byte, len(s.Data))
		copy(n.Data, s.Data)
	}
	for k, v := range s.Delegations {
		n.Delegations[k] = v
	}
	for k, v := range s.Settled {
		rec := v
		rec.Accepted = append([]Range(nil), v.Accepted...)
		rec.Discarded = append([]Range(nil), v.Discarded...)
		n.Settled[k] = rec
	}
	if len(s.Truncations) > 0 {
		n.Truncations = make([]diskTruncation, len(s.Truncations))
		for i, t := range s.Truncations {
			t.Pending = append([]DelegationRef(nil), t.Pending...)
			n.Truncations[i] = t
		}
	}
	return n
}

// headIndex returns the index of the first truncation that is not yet
// effective, or -1 when no truncation is outstanding.
func (s *diskState) headIndex() int {
	for i := range s.Truncations {
		if !s.Truncations[i].Effective {
			return i
		}
	}
	return -1
}

func (s *diskState) findDelegation(id DelegationID) (diskDelegation, bool) {
	d, ok := s.Delegations[delegationKey(id)]
	return d, ok
}

func delegationKey(id DelegationID) string {
	return fmt.Sprintf("%d", uint64(id))
}

func settledKey(id DelegationID, gen Generation, commit CommitID, off, length int64) string {
	return fmt.Sprintf("%d/%d/%d/%d/%d", uint64(id), uint64(gen), uint64(commit), off, length)
}

// Coordinator is a single-writer range-write delegation coordinator backed by
// a state directory. It is safe for concurrent use.
type Coordinator struct {
	mu     sync.Mutex
	dir    string
	path   string
	st     *diskState
	closed bool
	change chan struct{}
}

// Open opens or creates a coordinator whose durable state lives in dir.
func Open(dir string) (*Coordinator, error) {
	if dir == "" {
		return nil, errors.New("rangedeleg: empty state directory")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("rangedeleg: create state dir: %w", err)
	}
	c := &Coordinator{
		dir:    dir,
		path:   filepath.Join(dir, "state.json"),
		change: make(chan struct{}),
	}
	raw, err := os.ReadFile(c.path)
	switch {
	case errors.Is(err, os.ErrNotExist):
		c.st = newDiskState()
		if err := c.saveLocked(c.st.clone()); err != nil {
			return nil, err
		}
	case err != nil:
		return nil, fmt.Errorf("rangedeleg: read state: %w", err)
	default:
		st := &diskState{}
		if err := json.Unmarshal(raw, st); err != nil {
			return nil, fmt.Errorf("rangedeleg: decode state: %w", err)
		}
		if st.Version != stateVersion {
			return nil, fmt.Errorf("rangedeleg: unsupported state version %d", st.Version)
		}
		if st.Delegations == nil {
			st.Delegations = make(map[string]diskDelegation)
		}
		if st.Settled == nil {
			st.Settled = make(map[string]settledRecord)
		}
		if st.NextID == 0 {
			st.NextID = 1
		}
		if int64(len(st.Data)) > st.Length {
			st.Data = st.Data[:st.Length]
		}
		if int64(len(st.Data)) < st.Length {
			padded := make([]byte, st.Length)
			copy(padded, st.Data)
			st.Data = padded
		}
		c.st = st
	}
	return c, nil
}

// Close releases the coordinator. Subsequent calls return ErrClosed.
func (c *Coordinator) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return nil
	}
	c.closed = true
	c.notifyLocked()
	return nil
}

// Dir returns the state directory backing the coordinator.
func (c *Coordinator) Dir() string { return c.dir }

func (c *Coordinator) saveLocked(s *diskState) error {
	raw, err := json.Marshal(s)
	if err != nil {
		return fmt.Errorf("rangedeleg: encode state: %w", err)
	}
	tmp, err := os.CreateTemp(c.dir, "state-*.json")
	if err != nil {
		return fmt.Errorf("rangedeleg: create temp state: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return fmt.Errorf("rangedeleg: chmod temp state: %w", err)
	}
	if _, err := tmp.Write(raw); err != nil {
		tmp.Close()
		return fmt.Errorf("rangedeleg: write temp state: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("rangedeleg: sync temp state: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("rangedeleg: close temp state: %w", err)
	}
	if err := os.Rename(tmpName, c.path); err != nil {
		return fmt.Errorf("rangedeleg: commit state: %w", err)
	}
	return nil
}

func (c *Coordinator) notifyLocked() {
	close(c.change)
	c.change = make(chan struct{})
}

// commitLocked persists next and installs it as the current state.
func (c *Coordinator) commitLocked(next *diskState) error {
	if err := c.saveLocked(next); err != nil {
		return err
	}
	c.st = next
	c.notifyLocked()
	return nil
}

// Grant grants a delegation over [off, off+length) to client. If the same
// client already held exactly this range and it was revoked, the grant reuses
// the DelegationID and increments its Generation; otherwise a new delegation
// is created.
func (c *Coordinator) Grant(client string, off, length int64) (Delegation, error) {
	if client == "" {
		return Delegation{}, errors.New("rangedeleg: empty client name")
	}
	if off < 0 || length <= 0 {
		return Delegation{}, fmt.Errorf("%w: off=%d len=%d", ErrInvalidSize, off, length)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return Delegation{}, ErrClosed
	}
	next := c.st.clone()

	if idx := next.headIndex(); idx >= 0 {
		head := next.Truncations[idx]
		if off+length > head.Target {
			return Delegation{}, fmt.Errorf("%w: range [%d,%d) intersects pending truncation to %d", ErrTruncationPending, off, off+length, head.Target)
		}
	}

	for _, d := range next.Delegations {
		if d.Client == client && d.Off == off && d.Len == length {
			if d.State == DelegationRecalling {
				return toDelegation(d), fmt.Errorf("%w: delegation %d is being recalled", ErrTruncationPending, d.ID)
			}
			if d.State == DelegationActive {
				return toDelegation(d), nil
			}
			nd := d
			nd.Generation++
			nd.State = DelegationActive
			next.Delegations[delegationKey(nd.ID)] = nd
			if err := c.commitLocked(next); err != nil {
				return Delegation{}, err
			}
			return toDelegation(nd), nil
		}
	}

	d := diskDelegation{
		ID:         next.NextID,
		Client:     client,
		Off:        off,
		Len:        length,
		Generation: 1,
		State:      DelegationActive,
	}
	next.NextID++
	next.Delegations[delegationKey(d.ID)] = d
	if err := c.commitLocked(next); err != nil {
		return Delegation{}, err
	}
	return toDelegation(d), nil
}

func toDelegation(d diskDelegation) Delegation {
	return Delegation{
		ID:         d.ID,
		Client:     d.Client,
		Off:        d.Off,
		Len:        d.Len,
		Generation: d.Generation,
		State:      d.State,
	}
}

// Delegation returns a snapshot of a delegation.
func (c *Coordinator) Delegation(id DelegationID) (Delegation, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	d, ok := c.st.findDelegation(id)
	if !ok {
		return Delegation{}, false
	}
	return toDelegation(d), true
}

// Writeback settles one writeback segment. The segment must lie inside the
// delegation's range. Segments of a batch may be split, duplicated or
// delivered out of order; an exact (generation, commit, offset, length)
// repeat is reported as a duplicate and is applied only once.
//
// If a truncation is awaiting recall confirmation, the segment is split at
// the truncation point: the part below it is applied, the part at or above it
// is discarded and reported in Discarded.
func (c *Coordinator) Writeback(client string, ref DelegationRef, commit CommitID, off int64, data []byte) (WritebackResult, error) {
	if len(data) == 0 {
		return WritebackResult{}, ErrEmptyWrite
	}
	if off < 0 {
		return WritebackResult{}, fmt.Errorf("%w: off=%d", ErrInvalidSize, off)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return WritebackResult{}, ErrClosed
	}

	d, ok := c.st.findDelegation(ref.ID)
	if !ok {
		return WritebackResult{}, ErrUnknownDelegation
	}
	if d.Client != client {
		return WritebackResult{}, ErrClientMismatch
	}
	if d.Generation != ref.Generation {
		return WritebackResult{}, fmt.Errorf("%w: have %d, receipt carries %d", ErrStaleGeneration, d.Generation, ref.Generation)
	}
	if d.State == DelegationRevoked {
		return WritebackResult{}, ErrDelegationRevoked
	}

	end := off + int64(len(data))
	if off < d.Off || end > d.Off+d.Len {
		return WritebackResult{}, fmt.Errorf("%w: segment [%d,%d) outside [%d,%d)", ErrOutsideDelegation, off, end, d.Off, d.Off+d.Len)
	}

	digest := sha256.Sum256(data)
	digestHex := hex.EncodeToString(digest[:])
	key := settledKey(d.ID, d.Generation, commit, off, int64(len(data)))
	if rec, seen := c.st.Settled[key]; seen {
		if rec.Digest != digestHex {
			return WritebackResult{}, fmt.Errorf("%w: commit %d [%d,%d)", ErrConflict, commit, off, end)
		}
		return WritebackResult{Commit: commit, Duplicate: true, Accepted: copyRanges(rec.Accepted), Discarded: copyRanges(rec.Discarded)}, nil
	}

	next := c.st.clone()
	res := WritebackResult{Commit: commit}

	apply := data
	applyEnd := end
	if idx := next.headIndex(); idx >= 0 {
		head := &next.Truncations[idx]
		target := head.Target
		switch {
		case off >= target:
			res.Discarded = []Range{{Off: off, Len: end - off}}
			res.TruncationSeq = head.Seq
			apply = nil
		case end > target:
			res.SplitAt = target
			res.TruncationSeq = head.Seq
			res.Accepted = []Range{{Off: off, Len: target - off}}
			res.Discarded = []Range{{Off: target, Len: end - target}}
			apply = data[:target-off]
			applyEnd = target
		default:
			res.Accepted = []Range{{Off: off, Len: end - off}}
		}
	} else {
		res.Accepted = []Range{{Off: off, Len: end - off}}
	}

	if len(apply) > 0 {
		next.Data = applyBytes(next.Data, off, apply)
		if applyEnd > next.Length {
			next.Length = applyEnd
		}
	}

	rec := settledRecord{
		ID:         d.ID,
		Generation: d.Generation,
		Commit:     commit,
		Off:        off,
		Len:        int64(len(data)),
		Digest:     digestHex,
		Accepted:   copyRanges(res.Accepted),
		Discarded:  copyRanges(res.Discarded),
	}
	next.Settled[key] = rec

	if err := c.commitLocked(next); err != nil {
		return WritebackResult{}, err
	}
	return res, nil
}

func copyRanges(in []Range) []Range {
	if len(in) == 0 {
		return nil
	}
	out := make([]Range, len(in))
	copy(out, in)
	return out
}

func applyBytes(data []byte, off int64, src []byte) []byte {
	end := off + int64(len(src))
	if end > int64(len(data)) {
		grown := make([]byte, end)
		copy(grown, data)
		data = grown
	}
	copy(data[off:end], src)
	return data
}

// Truncate requests that the file shrink to size. It returns immediately; the
// truncation takes effect once every delegation intersecting the discarded
// region has confirmed the recall. Reads continue to observe the old length
// and old bytes until then. Truncation targets must be strictly decreasing
// when several are outstanding.
func (c *Coordinator) Truncate(size int64) (*TruncationHandle, error) {
	if size < 0 {
		return nil, fmt.Errorf("%w: size=%d", ErrInvalidSize, size)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return nil, ErrClosed
	}
	if size >= c.st.Length {
		return nil, fmt.Errorf("%w: size=%d length=%d", ErrNotShrinking, size, c.st.Length)
	}
	for i := range c.st.Truncations {
		t := c.st.Truncations[i]
		if t.Effective {
			continue
		}
		if size >= t.Target {
			return nil, fmt.Errorf("%w: new target %d not below outstanding target %d", ErrTruncationOrder, size, t.Target)
		}
	}

	next := c.st.clone()
	next.NextTruncSeq++
	t := diskTruncation{Seq: next.NextTruncSeq, Target: size}
	next.Truncations = append(next.Truncations, t)
	idx := len(next.Truncations) - 1
	if idx == next.headIndex() {
		c.issueRecallsLocked(next, idx)
	}
	c.advanceTruncationsLocked(next)

	if err := c.commitLocked(next); err != nil {
		return nil, err
	}
	return &TruncationHandle{c: c, Seq: t.Seq, Target: size}, nil
}

// issueRecallsLocked marks every active delegation intersecting
// [target, current effective length) as recalling and records the confirmation
// that is still outstanding.
func (c *Coordinator) issueRecallsLocked(s *diskState, idx int) {
	t := &s.Truncations[idx]
	t.Pending = nil
	if s.Length <= t.Target {
		return
	}
	for key, d := range s.Delegations {
		if d.State != DelegationActive {
			continue
		}
		// Anything that overlaps the discarded region [target, +inf) must be
		// recalled, including delegations granted beyond the current length.
		if d.Off+d.Len <= t.Target {
			continue
		}
		d.State = DelegationRecalling
		s.Delegations[key] = d
		t.Pending = append(t.Pending, DelegationRef{ID: d.ID, Generation: d.Generation})
	}
}

// advanceTruncationsLocked applies every head truncation whose recalls are
// all confirmed, then issues recalls for the next queued target.
func (c *Coordinator) advanceTruncationsLocked(s *diskState) {
	for {
		idx := s.headIndex()
		if idx < 0 {
			return
		}
		t := &s.Truncations[idx]
		if len(t.Pending) > 0 {
			return
		}
		if int64(len(s.Data)) > t.Target {
			s.Data = s.Data[:t.Target]
		}
		if s.Length > t.Target {
			s.Length = t.Target
		}
		t.Effective = true
		s.EffectiveOrder++
		t.EffectiveOrder = s.EffectiveOrder

		if nextIdx := s.headIndex(); nextIdx >= 0 {
			c.issueRecallsLocked(s, nextIdx)
		}
	}
}

// ConfirmRecall records that a client has returned the delegation and will not
// write with it again. The generation becomes revoked. When the last
// outstanding recall for the head truncation is confirmed, the truncation is
// applied.
func (c *Coordinator) ConfirmRecall(client string, ref DelegationRef) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return ErrClosed
	}
	d, ok := c.st.findDelegation(ref.ID)
	if !ok {
		return ErrUnknownDelegation
	}
	if d.Client != client {
		return ErrClientMismatch
	}
	if d.Generation != ref.Generation {
		return fmt.Errorf("%w: have %d, receipt carries %d", ErrStaleGeneration, d.Generation, ref.Generation)
	}
	if d.State == DelegationRevoked {
		return nil
	}
	if d.State != DelegationRecalling {
		return fmt.Errorf("rangedeleg: delegation %d is not being recalled", d.ID)
	}

	next := c.st.clone()
	nd := next.Delegations[delegationKey(d.ID)]
	nd.State = DelegationRevoked
	next.Delegations[delegationKey(d.ID)] = nd

	if idx := next.headIndex(); idx >= 0 {
		t := &next.Truncations[idx]
		kept := t.Pending[:0]
		for _, p := range t.Pending {
			if p.ID == d.ID && p.Generation == d.Generation {
				continue
			}
			kept = append(kept, p)
		}
		t.Pending = append([]DelegationRef(nil), kept...)
	}
	c.advanceTruncationsLocked(next)

	return c.commitLocked(next)
}

// Extend grows the file to size. The newly exposed region reads as zeros. It
// is rejected while a truncation is pending.
func (c *Coordinator) Extend(size int64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return ErrClosed
	}
	if size <= c.st.Length {
		return fmt.Errorf("%w: size=%d length=%d", ErrInvalidSize, size, c.st.Length)
	}
	if c.st.headIndex() >= 0 {
		return ErrTruncationPending
	}
	next := c.st.clone()
	grown := make([]byte, size)
	copy(grown, next.Data)
	next.Data = grown
	next.Length = size
	return c.commitLocked(next)
}

// Read returns n bytes starting at off from the currently visible file image.
func (c *Coordinator) Read(off int64, n int) ([]byte, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return nil, ErrClosed
	}
	if off < 0 || n < 0 || off+int64(n) > c.st.Length {
		return nil, fmt.Errorf("%w: off=%d n=%d length=%d", ErrOutOfBounds, off, n, c.st.Length)
	}
	out := make([]byte, n)
	copy(out, c.st.Data[off:off+int64(n)])
	return out, nil
}

// Length returns the currently visible file length. While a truncation awaits
// recall confirmation this is still the pre-truncation length.
func (c *Coordinator) Length() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.st.Length
}

// Snapshot returns a copy of the currently visible file contents.
func (c *Coordinator) Snapshot() []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]byte, len(c.st.Data))
	copy(out, c.st.Data)
	return out
}

// TruncationStatus returns the status of the truncation with the given
// sequence number. The bool is false when the sequence is unknown.
func (c *Coordinator) TruncationStatus(seq uint64) (TruncationStatus, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, t := range c.st.Truncations {
		if t.Seq == seq {
			st := TruncationStatus{
				Seq:            t.Seq,
				Target:         t.Target,
				Pending:        append([]DelegationRef(nil), t.Pending...),
				Effective:      t.Effective,
				EffectiveOrder: t.EffectiveOrder,
			}
			return st, true
		}
	}
	return TruncationStatus{}, false
}

// PendingTruncations returns the outstanding truncations in request order.
func (c *Coordinator) PendingTruncations() []TruncationStatus {
	c.mu.Lock()
	defer c.mu.Unlock()
	var out []TruncationStatus
	for _, t := range c.st.Truncations {
		if t.Effective {
			continue
		}
		out = append(out, TruncationStatus{
			Seq:     t.Seq,
			Target:  t.Target,
			Pending: append([]DelegationRef(nil), t.Pending...),
		})
	}
	return out
}

// WaitTruncation blocks until the truncation with the given sequence number is
// effective or ctx is done. It works across reopen because it polls durable
// state and the coordinator's change notifications.
func (c *Coordinator) WaitTruncation(ctx context.Context, seq uint64) (TruncationStatus, error) {
	for {
		c.mu.Lock()
		if c.closed {
			c.mu.Unlock()
			return TruncationStatus{}, ErrClosed
		}
		var found *TruncationStatus
		for _, t := range c.st.Truncations {
			if t.Seq == seq {
				st := TruncationStatus{
					Seq:            t.Seq,
					Target:         t.Target,
					Pending:        append([]DelegationRef(nil), t.Pending...),
					Effective:      t.Effective,
					EffectiveOrder: t.EffectiveOrder,
				}
				found = &st
				break
			}
		}
		ch := c.change
		c.mu.Unlock()
		if found == nil {
			return TruncationStatus{}, ErrUnknownTruncation
		}
		if found.Effective {
			return *found, nil
		}
		select {
		case <-ctx.Done():
			return *found, ctx.Err()
		case <-ch:
		}
	}
}
