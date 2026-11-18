// Package logrot implements a fixed-capacity local audit-log rotation
// module. Events are appended into segments; sealed segments are digested
// and archive confirmations bind to that digest. Segments holding
// unconfirmed security events are never reclaimed; confirmed events may
// only be dropped with their segment after the retention period ends.
// Ordinary (non-security) expired segments are reclaimed first when space
// runs out. Rotation and deletion are journaled so that recovery after an
// interrupted rotation can tell committed new segments apart from old
// segments pending deletion.
package logrot

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"time"
)

// EventKind classifies an audit event.
type EventKind int

const (
	// KindNormal is an ordinary log event.
	KindNormal EventKind = iota
	// KindSecurity is a security event: its segment must be retained
	// until the event's archival has been confirmed.
	KindSecurity
)

// Event is a single audit record. Size is its encoded size in bytes and
// counts against the fixed terminal capacity.
type Event struct {
	ID        string
	Kind      EventKind
	Timestamp time.Time
	Size      int
	Payload   string
}

// SegmentState is the lifecycle state of a segment.
type SegmentState int

const (
	// StateActive: the segment is open for appends.
	StateActive SegmentState = iota
	// StateSealed: the segment is closed and digested; confirmations
	// bind to its digest.
	StateSealed
	// StatePendingDelete: deletion was journaled but not finished;
	// recovery will complete it.
	StatePendingDelete
)

// Segment is a sealed-or-active batch of events.
type Segment struct {
	ID        string
	State     SegmentState
	Committed bool // journaled commit received; uncommitted segments are dropped on recovery
	Events    []Event
	Confirmed map[string]bool // event ID -> archive confirmed (bound to Digest)
	Digest    string          // set when the segment is sealed
	SealedAt  time.Time
}

func (s *Segment) size() int {
	n := 0
	for _, ev := range s.Events {
		n += ev.Size
	}
	return n
}

func (s *Segment) hasSecurity() bool {
	for _, ev := range s.Events {
		if ev.Kind == KindSecurity {
			return true
		}
	}
	return false
}

func (s *Segment) hasUnconfirmedSecurity() bool {
	for _, ev := range s.Events {
		if ev.Kind == KindSecurity && !s.Confirmed[ev.ID] {
			return true
		}
	}
	return false
}

// NoSpaceError is returned by Append when reclaiming expired segments
// cannot free enough room. It is retryable: callers may retry after
// confirmations arrive or the retention period elapses.
type NoSpaceError struct {
	Needed    int
	Available int
}

func (e *NoSpaceError) Error() string {
	return fmt.Sprintf("logrot: insufficient space: need %d bytes, %d available (retryable)", e.Needed, e.Available)
}

// Retryable reports that the rejection may succeed if retried later.
func (e *NoSpaceError) Retryable() bool { return true }

var (
	// ErrDigestMismatch: the confirmation was issued for a different
	// segment digest (e.g. the segment was re-segmented meanwhile).
	ErrDigestMismatch = errors.New("logrot: confirmation digest does not match segment digest")
	// ErrSegmentNotFound: the segment does not exist (already removed).
	ErrSegmentNotFound = errors.New("logrot: segment not found")
	// ErrSegmentNotSealed: confirmations only bind to sealed segments.
	ErrSegmentNotSealed = errors.New("logrot: segment is not sealed")
	// ErrEventNotFound: the confirmed event is not in the segment.
	ErrEventNotFound = errors.New("logrot: event not found in segment")
	// ErrBadSplit: re-segmentation split point out of range.
	ErrBadSplit = errors.New("logrot: split point out of range")
	// ErrEventTooLarge: the event can never fit in a segment; not retryable.
	ErrEventTooLarge = errors.New("logrot: event exceeds max segment size")
)

// Config tunes the store.
type Config struct {
	CapacityBytes   int           // fixed total space for local audit logs
	MaxSegmentBytes int           // rotation threshold per segment
	Retention       time.Duration // confirmed events may only be dropped after this
}

type journalOp string

const (
	opCreate        journalOp = "create"
	opCommit        journalOp = "commit"
	opActivate      journalOp = "activate"
	opSeal          journalOp = "seal"
	opPendingDelete journalOp = "pending_delete"
	opDelete        journalOp = "delete"
)

// JournalEntry is one write-ahead log record.
type JournalEntry struct {
	Op        journalOp
	SegmentID string
}

// Store is the rotation module. It is not safe for concurrent use.
type Store struct {
	cfg      Config
	now      func() time.Time
	segments map[string]*Segment
	order    []string // segment IDs in creation order
	activeID string
	nextID   int
	journal  []JournalEntry
	hook     func(JournalEntry) // test hook: fired after each journal append
}

// New creates an empty store. now supplies the clock (injectable for tests).
func New(cfg Config, now func() time.Time) *Store {
	return &Store{
		cfg:      cfg,
		now:      now,
		segments: make(map[string]*Segment),
		nextID:   1,
	}
}

func (s *Store) appendJournal(op journalOp, id string) {
	s.journal = append(s.journal, JournalEntry{Op: op, SegmentID: id})
	if s.hook != nil {
		s.hook(s.journal[len(s.journal)-1])
	}
}

func (s *Store) newSegmentID() string {
	id := "seg-" + strconv.Itoa(s.nextID)
	s.nextID++
	return id
}

// Used returns the bytes currently held by all live segments.
func (s *Store) Used() int {
	n := 0
	for _, seg := range s.segments {
		n += seg.size()
	}
	return n
}

// SegmentIDs lists live segment IDs in creation order.
func (s *Store) SegmentIDs() []string {
	out := make([]string, 0, len(s.order))
	for _, id := range s.order {
		if _, ok := s.segments[id]; ok {
			out = append(out, id)
		}
	}
	return out
}

// ActiveID returns the ID of the segment currently open for appends.
func (s *Store) ActiveID() string { return s.activeID }

// Segment looks up a live segment by ID.
func (s *Store) Segment(id string) (*Segment, bool) {
	seg, ok := s.segments[id]
	return seg, ok
}

// Append adds an event to the active segment, rotating first if the event
// would exceed the segment budget. If capacity is insufficient it reclaims
// expired cleanable segments (ordinary ones first); if that is not enough
// it returns a retryable *NoSpaceError.
func (s *Store) Append(ev Event) error {
	if ev.Size <= 0 {
		return fmt.Errorf("logrot: event size must be positive, got %d", ev.Size)
	}
	if ev.Size > s.cfg.MaxSegmentBytes {
		return ErrEventTooLarge
	}
	if err := s.ensureSpace(ev.Size); err != nil {
		return err
	}
	if s.activeID == "" || s.segments[s.activeID].size()+ev.Size > s.cfg.MaxSegmentBytes {
		s.rotate()
	}
	seg := s.segments[s.activeID]
	seg.Events = append(seg.Events, ev)
	return nil
}

func (s *Store) ensureSpace(extra int) error {
	if s.Used()+extra <= s.cfg.CapacityBytes {
		return nil
	}
	need := s.Used() + extra - s.cfg.CapacityBytes
	for _, seg := range s.reclaimable(s.now()) {
		if need <= 0 {
			break
		}
		need -= seg.size()
		s.deleteSegment(seg.ID)
	}
	if s.Used()+extra > s.cfg.CapacityBytes {
		return &NoSpaceError{Needed: extra, Available: s.cfg.CapacityBytes - s.Used()}
	}
	return nil
}

// rotate opens a new active segment and seals the previous one, journaling
// each step (create -> commit -> activate -> seal) so an interrupted
// rotation can be recovered: the journal records which segment was
// activated and whether the old one was sealed.
func (s *Store) rotate() {
	oldID := s.activeID
	id := s.newSegmentID()
	s.appendJournal(opCreate, id)
	seg := &Segment{ID: id, State: StateActive, Confirmed: make(map[string]bool)}
	s.segments[id] = seg
	s.appendJournal(opCommit, id)
	seg.Committed = true
	s.order = append(s.order, id)
	s.appendJournal(opActivate, id)
	s.activeID = id
	if oldID != "" {
		old := s.segments[oldID]
		s.appendJournal(opSeal, old.ID)
		sealSegment(old, s.now())
	}
}

func sealSegment(seg *Segment, t time.Time) {
	seg.State = StateSealed
	seg.SealedAt = t
	seg.Digest = computeDigest(seg.Events)
}

func computeDigest(events []Event) string {
	h := sha256.New()
	for _, ev := range events {
		fmt.Fprintf(h, "%s|%d|%d|%d|%s\n", ev.ID, ev.Kind, ev.Timestamp.UnixNano(), ev.Size, ev.Payload)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// cleanable reports whether the segment may be dropped at time now: it must
// be sealed, hold no unconfirmed security event, and its retention period
// must have ended.
func (s *Store) cleanable(seg *Segment, now time.Time) bool {
	return seg.State == StateSealed &&
		!seg.hasUnconfirmedSecurity() &&
		!now.Before(seg.SealedAt.Add(s.cfg.Retention))
}

// reclaimable lists cleanable segments, ordinary (security-free) expired
// segments first, then oldest first.
func (s *Store) reclaimable(now time.Time) []*Segment {
	var out []*Segment
	for _, id := range s.order {
		seg, ok := s.segments[id]
		if ok && s.cleanable(seg, now) {
			out = append(out, seg)
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		si, sj := out[i].hasSecurity(), out[j].hasSecurity()
		if si != sj {
			return !si // ordinary logs are reclaimed first
		}
		return out[i].SealedAt.Before(out[j].SealedAt)
	})
	return out
}

// Cleanup drops every segment that is cleanable at now and returns their IDs.
func (s *Store) Cleanup(now time.Time) []string {
	var removed []string
	for _, seg := range s.reclaimable(now) {
		removed = append(removed, seg.ID)
		s.deleteSegment(seg.ID)
	}
	return removed
}

// deleteSegment journals a two-phase delete: pending_delete, then delete.
func (s *Store) deleteSegment(id string) {
	seg, ok := s.segments[id]
	if !ok {
		return
	}
	s.appendJournal(opPendingDelete, id)
	seg.State = StatePendingDelete
	s.appendJournal(opDelete, id)
	delete(s.segments, id)
}

// ConfirmArchive records archive confirmations for events of a sealed
// segment. The confirmation binds to the segment digest: a digest that
// does not match (e.g. issued before a re-segmentation) is rejected and
// nothing is confirmed.
func (s *Store) ConfirmArchive(segmentID, digest string, eventIDs ...string) error {
	seg, ok := s.segments[segmentID]
	if !ok {
		return ErrSegmentNotFound
	}
	if seg.State != StateSealed {
		return ErrSegmentNotSealed
	}
	if seg.Digest != digest {
		return ErrDigestMismatch
	}
	for _, id := range eventIDs {
		found := false
		for _, ev := range seg.Events {
			if ev.ID == id {
				found = true
				break
			}
		}
		if !found {
			return ErrEventNotFound
		}
	}
	for _, id := range eventIDs {
		seg.Confirmed[id] = true
	}
	return nil
}

// Resegment splits a sealed segment into two new sealed segments at
// splitAt. The old segment is deleted; the new segments get fresh digests,
// so confirmations issued for the old digest cannot be replayed against
// them, and previously recorded confirmations do not carry over.
func (s *Store) Resegment(segmentID string, splitAt int) ([]string, error) {
	seg, ok := s.segments[segmentID]
	if !ok {
		return nil, ErrSegmentNotFound
	}
	if seg.State != StateSealed {
		return nil, ErrSegmentNotSealed
	}
	if splitAt <= 0 || splitAt >= len(seg.Events) {
		return nil, ErrBadSplit
	}
	parts := [][]Event{seg.Events[:splitAt], seg.Events[splitAt:]}
	ids := make([]string, 0, len(parts))
	for _, part := range parts {
		id := s.newSegmentID()
		s.appendJournal(opCreate, id)
		ns := &Segment{ID: id, State: StateActive, Confirmed: make(map[string]bool)}
		s.segments[id] = ns
		s.appendJournal(opCommit, id)
		ns.Committed = true
		s.order = append(s.order, id)
		ns.Events = append(ns.Events, part...)
		s.appendJournal(opSeal, id)
		sealSegment(ns, seg.SealedAt) // keep original sealing time for retention
		ids = append(ids, id)
	}
	s.deleteSegment(segmentID)
	return ids, nil
}

// Snapshot is a deep copy of the store's persistent state, used to
// simulate a crash and recovery.
type Snapshot struct {
	Segments map[string]*Segment
	Order    []string
	ActiveID string
	NextID   int
	Journal  []JournalEntry
}

// Snapshot captures the current persistent state.
func (s *Store) Snapshot() Snapshot {
	snap := Snapshot{
		Segments: make(map[string]*Segment, len(s.segments)),
		Order:    append([]string(nil), s.order...),
		ActiveID: s.activeID,
		NextID:   s.nextID,
		Journal:  append([]JournalEntry(nil), s.journal...),
	}
	for id, seg := range s.segments {
		cp := *seg
		cp.Events = append([]Event(nil), seg.Events...)
		cp.Confirmed = make(map[string]bool, len(seg.Confirmed))
		for k, v := range seg.Confirmed {
			cp.Confirmed[k] = v
		}
		snap.Segments[id] = &cp
	}
	return snap
}

// Recover rebuilds a store from a snapshot taken at an arbitrary point
// (e.g. mid-rotation). It finishes interrupted work according to the
// journal:
//   - segments created but never committed are discarded;
//   - the last activated segment in the journal becomes the active one;
//   - committed segments left active while a newer segment was activated
//     (rotation interrupted before sealing) are sealed now;
//   - segments journaled pending_delete are deleted for good.
func Recover(cfg Config, now func() time.Time, snap Snapshot) *Store {
	st := &Store{
		cfg:      cfg,
		now:      now,
		segments: snap.Segments,
		order:    append([]string(nil), snap.Order...),
		activeID: snap.ActiveID,
		nextID:   snap.NextID,
		journal:  append([]JournalEntry(nil), snap.Journal...),
	}
	lastOp := make(map[string]journalOp)
	committed := make(map[string]bool)
	activated := ""
	for _, e := range st.journal {
		lastOp[e.SegmentID] = e.Op
		if e.Op != opCreate {
			committed[e.SegmentID] = true // any record past create implies commit
		}
		if e.Op == opActivate {
			activated = e.SegmentID
		}
	}
	// Drop never-committed segments and complete interrupted deletes.
	for id, seg := range st.segments {
		switch {
		case !seg.Committed && !committed[id]:
			delete(st.segments, id)
		case seg.State == StatePendingDelete || lastOp[id] == opPendingDelete:
			delete(st.segments, id)
		}
	}
	// The active segment is the last one activated in the journal.
	if _, ok := st.segments[activated]; ok {
		st.activeID = activated
	} else if _, ok := st.segments[st.activeID]; !ok {
		// Repair the active pointer if it referenced a dropped segment.
		st.activeID = ""
		for i := len(st.order) - 1; i >= 0; i-- {
			if seg, ok := st.segments[st.order[i]]; ok && seg.State == StateActive {
				st.activeID = st.order[i]
				break
			}
		}
	}
	// Finish an interrupted rotation: a committed segment still active
	// while another segment is the active one was never sealed.
	for _, id := range st.order {
		seg, ok := st.segments[id]
		if ok && seg.State == StateActive && id != st.activeID {
			sealSegment(seg, now())
		}
	}
	return st
}
