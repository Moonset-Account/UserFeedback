package cloneplan

import (
	"fmt"
	"sort"
	"sync"
	"sync/atomic"
)

// snapshotEntry is the internal immutable registration record.
type snapshotEntry struct {
	ref     SnapshotRef
	parent  *SnapshotRef
	records []BlockRecord
}

// Store is the in-memory snapshot registry plus the clone publication
// journal. It is safe for concurrent use.
//
// Publication is a two-phase, single-commit action:
//
//	Stage   holds a plan as an invisible draft;
//	Commit  re-walks the parent chain from the registry (the final
//	        parent-chain version verification) and only on success moves the
//	        draft into the visible published set under one lock hold.
//
// A failed commit deletes the draft and never creates a visible entry. Source
// snapshots are never modified by the publication path.
type Store struct {
	mu sync.RWMutex

	snapshots map[SnapshotRef]*snapshotEntry

	drafts     map[string]*ClonePlan
	published  map[string]*PublishedClone
	sequences  []string // ordered PlanIDs of visible publications
	nextSeq    int64
	planIDSeed uint64
}

// NewStore creates an empty registry/publication store.
func NewStore() *Store {
	return &Store{
		snapshots: make(map[SnapshotRef]*snapshotEntry),
		drafts:    make(map[string]*ClonePlan),
		published: make(map[string]*PublishedClone),
	}
}

// Register adds an immutable fixed-version snapshot layer. The parent (when
// present) must already be registered. Records are validated, sorted and
// rejected when overlapping.
func (s *Store) Register(snap Snapshot) error {
	if snap.Ref.SnapshotID == "" || snap.Ref.Version == "" {
		return fmt.Errorf("%w: snapshot id and version are required", ErrInvalidRange)
	}
	for _, rec := range snap.Records {
		if !rec.Range.Valid() {
			return fmt.Errorf("%w: record %d..%d in %s", ErrInvalidRange, rec.Range.Start, rec.Range.End, snap.Ref.String())
		}
		if rec.Discardable && rec.Classification != ClassSensitive {
			return fmt.Errorf("%w: non-sensitive record %d..%d in %s cannot be discardable",
				ErrInvalidRange, rec.Range.Start, rec.Range.End, snap.Ref.String())
		}
	}

	records := append([]BlockRecord(nil), snap.Records...)
	sort.Slice(records, func(i, j int) bool { return records[i].Range.Start < records[j].Range.Start })
	for i := 1; i < len(records); i++ {
		if records[i-1].Range.End > records[i].Range.Start {
			return fmt.Errorf("%w: %s records %d..%d and %d..%d",
				ErrOverlappingRecords, snap.Ref.String(),
				records[i-1].Range.Start, records[i-1].Range.End,
				records[i].Range.Start, records[i].Range.End)
		}
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	if _, exists := s.snapshots[snap.Ref]; exists {
		return fmt.Errorf("%w: %s", ErrDuplicateSnapshot, snap.Ref.String())
	}
	if snap.Parent != nil {
		if _, ok := s.snapshots[*snap.Parent]; !ok {
			return fmt.Errorf("%w: parent %s of %s", ErrParentNotFound, snap.Parent.String(), snap.Ref.String())
		}
	}
	if err := s.cycleLocked(snap.Ref, snap.Parent); err != nil {
		return err
	}

	var parentCopy *SnapshotRef
	if snap.Parent != nil {
		p := *snap.Parent
		parentCopy = &p
	}
	s.snapshots[snap.Ref] = &snapshotEntry{
		ref:     snap.Ref,
		parent:  parentCopy,
		records: records,
	}
	return nil
}

func (s *Store) cycleLocked(ref SnapshotRef, parent *SnapshotRef) error {
	visited := map[SnapshotRef]bool{ref: true}
	cur := parent
	for cur != nil {
		if visited[*cur] {
			return fmt.Errorf("%w: %s", ErrSnapshotCycle, cur.String())
		}
		visited[*cur] = true
		entry, ok := s.snapshots[*cur]
		if !ok {
			return fmt.Errorf("%w: %s", ErrSnapshotNotFound, cur.String())
		}
		cur = entry.parent
	}
	return nil
}

// Get returns a defensive copy of a registered snapshot.
func (s *Store) Get(ref SnapshotRef) (Snapshot, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	entry, ok := s.snapshots[ref]
	if !ok {
		return Snapshot{}, false
	}
	return cloneSnapshot(entry), true
}

func cloneSnapshot(entry *snapshotEntry) Snapshot {
	out := Snapshot{
		Ref:     entry.ref,
		Records: append([]BlockRecord(nil), entry.records...),
	}
	if entry.parent != nil {
		p := *entry.parent
		out.Parent = &p
	}
	return out
}

// Stage holds an accepted, non-blocked plan as an invisible draft. The draft
// remains invisible through ListPublished/GetPublished until Commit succeeds.
func (s *Store) Stage(plan *ClonePlan) error {
	if plan == nil {
		return fmt.Errorf("%w: nil plan", ErrPlanNotFound)
	}
	if plan.Blocked() {
		return fmt.Errorf("%w: %s", ErrPlanBlocked, plan.PlanID)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.drafts[plan.PlanID]; ok {
		return fmt.Errorf("%w: draft %s", ErrAlreadyStaged, plan.PlanID)
	}
	if _, ok := s.published[plan.PlanID]; ok {
		return fmt.Errorf("%w: committed %s", ErrAlreadyStaged, plan.PlanID)
	}
	s.drafts[plan.PlanID] = clonePlan(plan)
	return nil
}

// Commit performs the final parent-chain version verification and the
// publication visibility swap as one atomic action. Any verification failure
// removes the draft and leaves no visible trace; source snapshots stay
// readable and usable.
func (s *Store) Commit(planID string) (*PublishedClone, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	plan, ok := s.drafts[planID]
	if !ok {
		if _, pub := s.published[planID]; pub {
			return nil, fmt.Errorf("%w: %s already committed", ErrAlreadyStaged, planID)
		}
		return nil, fmt.Errorf("%w: %s", ErrPlanNotFound, planID)
	}

	// Final verification against the live registry: the parent chain pinned
	// at planning time must still resolve to the same fixed versions and the
	// effective block versions must be unchanged.
	if err := s.verifyChainLocked(plan); err != nil {
		delete(s.drafts, planID)
		return nil, err
	}

	seq := atomic.AddInt64(&s.nextSeq, 1)
	pub := &PublishedClone{
		PlanID:       plan.PlanID,
		Sequence:     seq,
		TargetTenant: plan.TargetTenant,
		Leaf:         plan.Leaf,
		CloneRange:   plan.CloneRange,
		ParentChain:  append([]SnapshotRef(nil), plan.PinnedChain...),
		Segments:     append([]PlannedSegment(nil), plan.Segments...),
	}
	s.published[planID] = pub
	s.sequences = append(s.sequences, planID)
	delete(s.drafts, planID)
	return clonePublished(pub), nil
}

// verifyChainLocked re-derives the chain from the live registry and compares
// fixed versions and every effective block version with the pinned plan.
func (s *Store) verifyChainLocked(plan *ClonePlan) error {
	if _, ok := s.snapshots[plan.Leaf]; !ok {
		return fmt.Errorf("%w: leaf %s no longer registered", ErrChainMismatch, plan.Leaf.String())
	}
	liveChain, err := chainLocked(s, plan.Leaf)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrChainMismatch, err)
	}
	if len(liveChain) != len(plan.PinnedChain) {
		return fmt.Errorf("%w: chain length changed for leaf %s", ErrChainMismatch, plan.Leaf.String())
	}
	for i := range liveChain {
		if liveChain[i] != plan.PinnedChain[i] {
			return fmt.Errorf("%w: layer %d changed: live=%s pinned=%s",
				ErrChainMismatch, i, liveChain[i].String(), plan.PinnedChain[i].String())
		}
	}

	liveResolved := resolveLocked(liveChain, s.snapshots, plan.CloneRange)
	if !resolvedEqual(liveResolved, plan.Resolved) {
		return fmt.Errorf("%w: effective block versions changed for leaf %s",
			ErrChainMismatch, plan.Leaf.String())
	}
	return nil
}

func resolvedEqual(a, b []ResolvedSegment) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		x, y := a[i], b[i]
		if x.Range != y.Range ||
			x.EffectiveVersion != y.EffectiveVersion ||
			x.Zero != y.Zero ||
			x.ExplicitZero != y.ExplicitZero ||
			x.Classification != y.Classification ||
			x.OwnerTenant != y.OwnerTenant ||
			x.Discardable != y.Discardable ||
			x.Reason != y.Reason {
			return false
		}
		if (x.Origin == nil) != (y.Origin == nil) {
			return false
		}
		if x.Origin != nil && *x.Origin != *y.Origin {
			return false
		}
	}
	return true
}

// RetireVersion removes a fixed snapshot version from the registry, simulating
// fixed-version rotation. Open plans whose chain references it fail their
// final commit verification; already-published clones keep their pinned chain.
func (s *Store) RetireVersion(ref SnapshotRef) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.snapshots[ref]; !ok {
		return fmt.Errorf("%w: %s", ErrSnapshotNotFound, ref.String())
	}
	delete(s.snapshots, ref)
	return nil
}

// GetDraft returns a copy of an invisible staged draft.
func (s *Store) GetDraft(planID string) (*ClonePlan, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	plan, ok := s.drafts[planID]
	if !ok {
		return nil, false
	}
	return clonePlan(plan), true
}

// GetPublished returns a visible committed clone by plan id.
func (s *Store) GetPublished(planID string) (*PublishedClone, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	pub, ok := s.published[planID]
	if !ok {
		return nil, false
	}
	return clonePublished(pub), true
}

// ListPublished returns all visible clones in commit order.
func (s *Store) ListPublished() []*PublishedClone {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*PublishedClone, 0, len(s.sequences))
	for _, id := range s.sequences {
		out = append(out, clonePublished(s.published[id]))
	}
	return out
}

func (s *Store) nextPlanIDLocked() string {
	n := atomic.AddUint64(&s.planIDSeed, 1)
	return fmt.Sprintf("plan-%06d", n)
}

func clonePlan(p *ClonePlan) *ClonePlan {
	cp := *p
	cp.PinnedChain = append([]SnapshotRef(nil), p.PinnedChain...)
	cp.Resolved = append([]ResolvedSegment(nil), p.Resolved...)
	cp.Segments = append([]PlannedSegment(nil), p.Segments...)
	cp.Receipts = append([]SanitizationReceipt(nil), p.Receipts...)
	cp.BlockedReasons = append([]string(nil), p.BlockedReasons...)
	for i := range cp.Resolved {
		if p.Resolved[i].Origin != nil {
			ref := *p.Resolved[i].Origin
			cp.Resolved[i].Origin = &ref
		}
	}
	for i := range cp.Segments {
		if p.Segments[i].Origin != nil {
			ref := *p.Segments[i].Origin
			cp.Segments[i].Origin = &ref
		}
	}
	return &cp
}

func clonePublished(p *PublishedClone) *PublishedClone {
	cp := *p
	cp.ParentChain = append([]SnapshotRef(nil), p.ParentChain...)
	cp.Segments = append([]PlannedSegment(nil), p.Segments...)
	for i := range cp.Segments {
		if p.Segments[i].Origin != nil {
			ref := *p.Segments[i].Origin
			cp.Segments[i].Origin = &ref
		}
	}
	return &cp
}
