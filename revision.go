package graphwalk

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"sync"
)

type PageChangeKind string

const (
	PageUnchanged PageChangeKind = "unchanged"
	PageInserted  PageChangeKind = "inserted"
	PageReplaced  PageChangeKind = "replaced"
	PageRemoved   PageChangeKind = "removed"
)

// PageChange summarizes one page identity across catalog revisions.
type PageChange struct {
	ID         PageID         `json:"id"`
	Kind       PageChangeKind `json:"kind"`
	OldVersion PageVersion    `json:"old_version,omitempty"`
	NewVersion PageVersion    `json:"new_version,omitempty"`
}

// ChangeSummary is caller-supplied page revision information.
type ChangeSummary struct {
	BaseVersion CatalogVersion `json:"base_version"`
	NewVersion  CatalogVersion `json:"new_version"`
	Changes     []PageChange   `json:"changes"`
}

// DiffCatalogs builds a summary from two fixed directory snapshots. Callers may
// instead construct ChangeSummary manually when the store supplies its own
// change feed.
func DiffCatalogs(oldCatalog, newCatalog Catalog) ChangeSummary {
	summary := ChangeSummary{}
	oldPages := map[PageID]PageInfo{}
	newPages := map[PageID]PageInfo{}
	if oldCatalog != nil {
		summary.BaseVersion = oldCatalog.Version()
		for _, p := range oldCatalog.Pages() {
			oldPages[p.ID] = p
		}
	}
	if newCatalog != nil {
		summary.NewVersion = newCatalog.Version()
		for _, p := range newCatalog.Pages() {
			newPages[p.ID] = p
		}
	}
	ids := map[PageID]struct{}{}
	for id := range oldPages {
		ids[id] = struct{}{}
	}
	for id := range newPages {
		ids[id] = struct{}{}
	}
	for id := range ids {
		op, inOld := oldPages[id]
		np, inNew := newPages[id]
		ch := PageChange{ID: id}
		switch {
		case inOld && inNew && op.Version == np.Version:
			ch.Kind = PageUnchanged
			ch.OldVersion = op.Version
			ch.NewVersion = np.Version
		case inOld && inNew:
			ch.Kind = PageReplaced
			ch.OldVersion = op.Version
			ch.NewVersion = np.Version
		case inNew:
			ch.Kind = PageInserted
			ch.NewVersion = np.Version
		default:
			ch.Kind = PageRemoved
			ch.OldVersion = op.Version
		}
		summary.Changes = append(summary.Changes, ch)
	}
	sort.Slice(summary.Changes, func(i, j int) bool { return summary.Changes[i].ID < summary.Changes[j].ID })
	return summary
}

// RevisionInput contains the explicit new directory and caller summary.
type RevisionInput struct {
	NewCatalog   Catalog
	Changes      ChangeSummary
	Reader       ReadPageFunc
	Availability func(PageRef) bool
	K            int
}

// Retraction asks a client to withdraw one previously delivered rank.
type Retraction struct {
	Rank int  `json:"rank"`
	Old  Walk `json:"old"`
}

// Reissue gives the corrected walk at one rank.
type Reissue struct {
	Rank     int  `json:"rank"`
	Replaces bool `json:"replaces_previous_rank"`
	Walk     Walk `json:"walk"`
}

// RevisionPlan is the maximal stable prefix and ordered correction sequences.
type RevisionPlan struct {
	Query         CheckpointQuery `json:"query"`
	K             int             `json:"k"`
	BaseVersion   CatalogVersion  `json:"base_version"`
	NewVersion    CatalogVersion  `json:"new_version"`
	Changes       ChangeSummary   `json:"changes"`
	PrefixLength  int             `json:"prefix_length"`
	OldEmitted    []Walk          `json:"old_emitted"`
	NewResults    []Walk          `json:"new_results"`
	Retractions   []Retraction    `json:"retractions"`
	Reissues      []Reissue       `json:"reissues"`
	FinalRank     int             `json:"final_rank"`
	RequestedRank int             `json:"requested_rank"`
}

type RevisionEventType string

const (
	EventRetract  RevisionEventType = "retract"
	EventReissue  RevisionEventType = "reissue"
	EventComplete RevisionEventType = "complete"
)

// RevisionEvent is one durable correction protocol event.
type RevisionEvent struct {
	Type      RevisionEventType `json:"type"`
	Rank      int               `json:"rank,omitempty"`
	Old       *Walk             `json:"old,omitempty"`
	New       *Walk             `json:"new,omitempty"`
	Replaces  bool              `json:"replaces,omitempty"`
	FinalRank int               `json:"final_rank,omitempty"`
}

// RevisionCursor emits retractions up to a durable acknowledgement boundary,
// then the补发/reissue sequence for the new catalog version.
type RevisionCursor struct {
	mu sync.Mutex

	plan         RevisionPlan
	proof        Proof
	ackedThrough int
	reissueIndex int
	completeSent bool
}

// DeriveRevision enumerates the new version's top K using inherited fetched
// immutable pages as a cache. It does not mutate the old cursor, which can
// continue reading its own fixed version.
func (c *Cursor) DeriveRevision(ctx context.Context, in RevisionInput) (*RevisionCursor, error) {
	if in.NewCatalog == nil {
		return nil, fmt.Errorf("graphwalk: revision NewCatalog is required")
	}

	c.mu.Lock()
	oldQuery := c.query
	oldK := c.k
	oldVersion := c.catalogVersion
	oldEmitted := make([]Walk, len(c.emitted))
	for i, w := range c.emitted {
		oldEmitted[i] = cloneWalk(w)
	}
	seed := map[PageRef]EdgePageSeed{}
	for id, fp := range c.fetched {
		_ = id
		seed[fp.Info.Ref()] = EdgePageSeed{Info: fp.Info, Edges: append([]Edge(nil), fp.Edges...)}
	}
	baseReader := c.reader
	c.mu.Unlock()

	if in.Changes.BaseVersion != "" && in.Changes.BaseVersion != oldVersion {
		return nil, fmt.Errorf("graphwalk: change summary base %q does not match cursor version %q", in.Changes.BaseVersion, oldVersion)
	}
	if in.Changes.NewVersion != "" && in.Changes.NewVersion != in.NewCatalog.Version() {
		return nil, fmt.Errorf("graphwalk: change summary new version %q does not match catalog %q", in.Changes.NewVersion, in.NewCatalog.Version())
	}
	changes := in.Changes
	changes.BaseVersion = oldVersion
	changes.NewVersion = in.NewCatalog.Version()

	targetK := oldK
	if in.K > 0 {
		targetK = in.K
	}
	if targetK < 0 {
		return nil, fmt.Errorf("graphwalk: revision K must be non-negative")
	}
	if targetK < len(oldEmitted) {
		return nil, fmt.Errorf("graphwalk: revision K=%d cannot be shorter than %d already emitted ranks", targetK, len(oldEmitted))
	}

	revReader := func(revCtx context.Context, ref PageRef) ([]Edge, error) {
		if seeded, ok := seed[ref]; ok {
			return append([]Edge(nil), seeded.Edges...), nil
		}
		if in.Reader != nil {
			return in.Reader(revCtx, ref)
		}
		return baseReader(revCtx, ref)
	}

	newQuery := Query{
		Source:     oldQuery.Source,
		Target:     oldQuery.Target,
		Expression: oldQuery.Expr.String(),
		Expr:       oldQuery.Expr,
		MaxHops:    oldQuery.MaxHops,
	}
	newCursor, err := NewCursor(newQuery, targetK, in.NewCatalog, revReader)
	if err != nil {
		return nil, err
	}
	newCursor.availability = in.Availability

	var newResults []Walk
	for len(newResults) < targetK {
		w, err := newCursor.Next(ctx)
		if errors.Is(err, ErrDone) {
			break
		}
		if err != nil {
			return nil, err
		}
		newResults = append(newResults, w)
	}

	prefix := 0
	for prefix < len(oldEmitted) && prefix < len(newResults) {
		if !walksEqual(oldEmitted[prefix], newResults[prefix]) {
			break
		}
		prefix++
	}
	// If every available new result matches but the old stream had another
	// emitted rank beyond NewResults, that old rank cannot remain a prefix.
	if prefix > len(newResults) {
		prefix = len(newResults)
	}

	plan := RevisionPlan{
		Query: CheckpointQuery{
			Source:     oldQuery.Source,
			Target:     oldQuery.Target,
			Expression: oldQuery.Expr.String(),
			MaxHops:    oldQuery.MaxHops,
		},
		K:             targetK,
		BaseVersion:   oldVersion,
		NewVersion:    in.NewCatalog.Version(),
		Changes:       changes,
		PrefixLength:  prefix,
		OldEmitted:    oldEmitted,
		NewResults:    newResults,
		FinalRank:     len(newResults),
		RequestedRank: targetK,
	}
	for r := prefix + 1; r <= len(oldEmitted); r++ {
		plan.Retractions = append(plan.Retractions, Retraction{Rank: r, Old: cloneWalk(oldEmitted[r-1])})
	}
	for r := prefix + 1; r <= len(newResults); r++ {
		plan.Reissues = append(plan.Reissues, Reissue{
			Rank:     r,
			Replaces: r <= len(oldEmitted),
			Walk:     cloneWalk(newResults[r-1]),
		})
	}

	return &RevisionCursor{
		plan:         plan,
		proof:        newCursor.Proof(),
		ackedThrough: prefix,
	}, nil
}

// EdgePageSeed is an inherited fetched page.
type EdgePageSeed struct {
	Info  PageInfo
	Edges []Edge
}

// Plan returns a copy of the revision plan.
func (r *RevisionCursor) Plan() RevisionPlan {
	r.mu.Lock()
	defer r.mu.Unlock()
	return clonePlan(r.plan)
}

// Proof returns the new-version page/rank basis.
func (r *RevisionCursor) Proof() Proof {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.proof
}

// AckedThrough returns the durable retraction acknowledgement boundary.
func (r *RevisionCursor) AckedThrough() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.ackedThrough
}

// AckRetraction acknowledges exactly the next contiguous retraction rank.
func (r *RevisionCursor) AckRetraction(rank int) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.ackLocked(rank)
}

// AckRetractionsThrough cumulatively acknowledges retractions through rank.
func (r *RevisionCursor) AckRetractionsThrough(rank int) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	last := len(r.plan.OldEmitted)
	if rank < r.plan.PrefixLength {
		return fmt.Errorf("graphwalk: ack rank %d precedes stable prefix %d", rank, r.plan.PrefixLength)
	}
	if rank > last {
		return fmt.Errorf("graphwalk: no retraction through rank %d", rank)
	}
	r.ackedThrough = rank
	return nil
}

func (r *RevisionCursor) ackLocked(rank int) error {
	last := len(r.plan.OldEmitted)
	if rank <= r.ackedThrough {
		return nil
	}
	if rank != r.ackedThrough+1 {
		return fmt.Errorf("graphwalk: non-contiguous ack %d after boundary %d", rank, r.ackedThrough)
	}
	if rank > last {
		return fmt.Errorf("graphwalk: no retraction for rank %d", rank)
	}
	r.ackedThrough = rank
	return nil
}

// NextEvent returns the next protocol event. An unacked retraction remains
// the current event until acknowledged; after the ack boundary reaches the
// end of the invalid old prefix, reissues and completion are emitted.
func (r *RevisionCursor) NextEvent(_ context.Context) (RevisionEvent, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	lastOld := len(r.plan.OldEmitted)
	if r.ackedThrough < lastOld {
		rank := r.ackedThrough + 1
		if rank < r.plan.PrefixLength+1 || rank > len(r.plan.OldEmitted) {
			return RevisionEvent{}, fmt.Errorf("graphwalk: invalid retraction boundary %d", r.ackedThrough)
		}
		old := cloneWalk(r.plan.OldEmitted[rank-1])
		return RevisionEvent{Type: EventRetract, Rank: rank, Old: &old}, nil
	}

	if r.reissueIndex < len(r.plan.Reissues) {
		ri := r.plan.Reissues[r.reissueIndex]
		r.reissueIndex++
		w := cloneWalk(ri.Walk)
		return RevisionEvent{Type: EventReissue, Rank: ri.Rank, New: &w, Replaces: ri.Replaces}, nil
	}
	if !r.completeSent {
		r.completeSent = true
		return RevisionEvent{Type: EventComplete, FinalRank: r.plan.FinalRank}, nil
	}
	return RevisionEvent{}, ErrDone
}

// PendingRetractions returns retractions above the acknowledgement boundary.
func (r *RevisionCursor) PendingRetractions() []Retraction {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []Retraction
	for _, rt := range r.plan.Retractions {
		if rt.Rank > r.ackedThrough {
			out = append(out, Retraction{Rank: rt.Rank, Old: cloneWalk(rt.Old)})
		}
	}
	return out
}

// RevisionCheckpoint is the durable state of a RevisionCursor.
type RevisionCheckpoint struct {
	Format       int          `json:"format"`
	Plan         RevisionPlan `json:"plan"`
	Proof        Proof        `json:"proof"`
	AckedThrough int          `json:"acked_through"`
	ReissueIndex int          `json:"reissue_index"`
	CompleteSent bool         `json:"complete_sent"`
}

// MarshalCheckpoint encodes the revision cursor state.
func (r *RevisionCursor) MarshalCheckpoint() ([]byte, error) {
	r.mu.Lock()
	cp := RevisionCheckpoint{
		Format:       checkpointFormat,
		Plan:         clonePlan(r.plan),
		Proof:        r.proof,
		AckedThrough: r.ackedThrough,
		ReissueIndex: r.reissueIndex,
		CompleteSent: r.completeSent,
	}
	r.mu.Unlock()
	data, err := json.MarshalIndent(&cp, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("graphwalk: marshal revision checkpoint: %w", err)
	}
	return data, nil
}

// RestoreRevisionCursor restores a durable revision acknowledgement boundary.
func RestoreRevisionCursor(data []byte) (*RevisionCursor, error) {
	var cp RevisionCheckpoint
	if err := json.Unmarshal(data, &cp); err != nil {
		return nil, fmt.Errorf("graphwalk: unmarshal revision checkpoint: %w", err)
	}
	if cp.Format != checkpointFormat {
		return nil, fmt.Errorf("graphwalk: unsupported revision checkpoint format %d", cp.Format)
	}
	last := len(cp.Plan.OldEmitted)
	if cp.AckedThrough < cp.Plan.PrefixLength || cp.AckedThrough > last {
		return nil, fmt.Errorf("graphwalk: invalid ack boundary %d", cp.AckedThrough)
	}
	if cp.ReissueIndex < 0 || cp.ReissueIndex > len(cp.Plan.Reissues) {
		return nil, fmt.Errorf("graphwalk: invalid reissue index %d", cp.ReissueIndex)
	}
	return &RevisionCursor{
		plan:         cp.Plan,
		proof:        cp.Proof,
		ackedThrough: cp.AckedThrough,
		reissueIndex: cp.ReissueIndex,
		completeSent: cp.CompleteSent,
	}, nil
}

func clonePlan(p RevisionPlan) RevisionPlan {
	out := p
	out.OldEmitted = make([]Walk, len(p.OldEmitted))
	for i, w := range p.OldEmitted {
		out.OldEmitted[i] = cloneWalk(w)
	}
	out.NewResults = make([]Walk, len(p.NewResults))
	for i, w := range p.NewResults {
		out.NewResults[i] = cloneWalk(w)
	}
	out.Retractions = make([]Retraction, len(p.Retractions))
	for i, rt := range p.Retractions {
		out.Retractions[i] = Retraction{Rank: rt.Rank, Old: cloneWalk(rt.Old)}
	}
	out.Reissues = make([]Reissue, len(p.Reissues))
	for i, ri := range p.Reissues {
		out.Reissues[i] = Reissue{Rank: ri.Rank, Replaces: ri.Replaces, Walk: cloneWalk(ri.Walk)}
	}
	out.Changes.Changes = append([]PageChange(nil), p.Changes.Changes...)
	return out
}
