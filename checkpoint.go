package graphwalk

import (
	"container/heap"
	"context"
	"encoding/json"
	"fmt"
)

// Checkpoint format version.
const checkpointFormat = 1

// CheckpointQuery is the serializable query definition.
type CheckpointQuery struct {
	Source     Vertex `json:"source"`
	Target     Vertex `json:"target"`
	Expression string `json:"expression"`
	MaxHops    int    `json:"max_hops"`
}

// QueuedItem is one pending actual path or unread-page guard.
type QueuedItem struct {
	Kind      itemKind  `json:"kind"`
	Seq       uint64    `json:"seq"`
	End       Vertex    `json:"end"`
	Walk      Walk      `json:"walk"`
	NFAStates []int     `json:"nfa_states"`
	Page      *PageInfo `json:"page,omitempty"`
}

// Checkpoint is the durable fixed-version cursor state.
type Checkpoint struct {
	Format         int                        `json:"format"`
	Query          CheckpointQuery            `json:"query"`
	K              int                        `json:"k"`
	CatalogVersion CatalogVersion             `json:"catalog_version"`
	Pages          []PageInfo                 `json:"pages"`
	Emitted        []Walk                     `json:"emitted"`
	Certificates   []RankCertificate          `json:"certificates,omitempty"`
	Queue          []QueuedItem               `json:"queue"`
	Seq            uint64                     `json:"seq"`
	Fetched        map[PageID]FetchedPageData `json:"fetched,omitempty"`
	LimitReached   bool                       `json:"limit_reached"`
}

// FetchedPageData stores a fetched immutable page in the checkpoint so a
// recovered cursor does not need to reopen it.
type FetchedPageData struct {
	Info            PageInfo `json:"info"`
	Edges           []Edge   `json:"edges"`
	ObservedMinCost int64    `json:"observed_min_cost"`
}

// RestoreOptions supplies callbacks that cannot be JSON-serialized.
type RestoreOptions struct {
	Reader       ReadPageFunc
	Availability func(PageRef) bool
}

// ExportCheckpoint returns a deep serializable snapshot.
func (c *Cursor) ExportCheckpoint() Checkpoint {
	c.mu.Lock()
	defer c.mu.Unlock()

	cp := Checkpoint{
		Format: checkpointFormat,
		Query: CheckpointQuery{
			Source:     c.query.Source,
			Target:     c.query.Target,
			Expression: c.query.Expr.String(),
			MaxHops:    c.query.MaxHops,
		},
		K:              c.k,
		CatalogVersion: c.catalogVersion,
		Pages:          append([]PageInfo(nil), c.pages...),
		Emitted:        make([]Walk, len(c.emitted)),
		Certificates:   make([]RankCertificate, len(c.certs)),
		Queue:          make([]QueuedItem, 0, c.pq.Len()),
		Seq:            c.seq,
		Fetched:        map[PageID]FetchedPageData{},
		LimitReached:   c.limitReached,
	}
	for i, w := range c.emitted {
		cp.Emitted[i] = cloneWalk(w)
	}
	for i, cert := range c.certs {
		cp.Certificates[i] = cert
		cp.Certificates[i].Result = cloneWalk(cert.Result)
	}
	for id, fp := range c.fetched {
		cp.Fetched[id] = FetchedPageData{
			Info:            fp.Info,
			Edges:           append([]Edge(nil), fp.Edges...),
			ObservedMinCost: fp.ObservedMinCost,
		}
	}
	for _, item := range c.pq {
		qi := QueuedItem{
			Kind:      item.kind,
			Seq:       item.seq,
			End:       item.path.end,
			Walk:      cloneWalk(item.path.walk),
			NFAStates: append([]int(nil), item.path.nfaStates...),
		}
		if item.page != nil {
			page := *item.page
			qi.Page = &page
		}
		cp.Queue = append(cp.Queue, qi)
	}
	return cp
}

// MarshalCheckpoint encodes the checkpoint as JSON.
func (c *Cursor) MarshalCheckpoint() ([]byte, error) {
	cp := c.ExportCheckpoint()
	data, err := json.MarshalIndent(&cp, "", "  ")
	if err != nil {
		return nil, fmt.Errorf("graphwalk: marshal checkpoint: %w", err)
	}
	return data, nil
}

// RestoreCursor restores a JSON checkpoint and continues only its fixed
// catalog/page versions. No page callback is made during restoration.
func RestoreCursor(data []byte, opts RestoreOptions) (*Cursor, error) {
	var cp Checkpoint
	if err := json.Unmarshal(data, &cp); err != nil {
		return nil, fmt.Errorf("graphwalk: unmarshal checkpoint: %w", err)
	}
	return RestoreCheckpoint(cp, opts)
}

// RestoreCheckpoint restores from an already decoded checkpoint.
func RestoreCheckpoint(cp Checkpoint, opts RestoreOptions) (*Cursor, error) {
	if cp.Format != checkpointFormat {
		return nil, fmt.Errorf("graphwalk: unsupported checkpoint format %d", cp.Format)
	}
	expr, err := ParseExpr(cp.Query.Expression)
	if err != nil {
		return nil, err
	}
	q := Query{
		Source:     cp.Query.Source,
		Target:     cp.Query.Target,
		Expression: cp.Query.Expression,
		Expr:       expr,
		MaxHops:    cp.Query.MaxHops,
	}
	catalog := NewCatalog(string(cp.CatalogVersion), cp.Pages...)
	c, err := NewCursor(q, cp.K, catalog, opts.Reader)
	if err != nil {
		return nil, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	c.pq = queueHeap{}
	c.seq = cp.Seq
	c.emitted = make([]Walk, 0, len(cp.Emitted))
	for _, w := range cp.Emitted {
		c.emitted = append(c.emitted, cloneWalk(w))
	}
	c.certs = append([]RankCertificate(nil), cp.Certificates...)
	c.limitReached = cp.LimitReached || len(c.emitted) >= c.k
	c.fetched = map[PageID]*fetchedPage{}
	for id, fp := range cp.Fetched {
		data := fp
		data.Edges = append([]Edge(nil), fp.Edges...)
		c.fetched[id] = &fetchedPage{Info: data.Info, Edges: data.Edges, ObservedMinCost: data.ObservedMinCost}
	}
	c.seen = map[string]struct{}{}
	for _, w := range c.emitted {
		c.seen[sequenceSignature(w.IDs)] = struct{}{}
	}

	var maxSeq uint64
	for _, qi := range cp.Queue {
		if qi.Seq > maxSeq {
			maxSeq = qi.Seq
		}
		w := cloneWalk(qi.Walk)
		states := append([]int(nil), qi.NFAStates...)
		p := &pathState{walk: w, end: qi.End, nfaStates: states}
		c.seen[sequenceSignature(w.IDs)] = struct{}{}
		item := &queueItem{seq: qi.Seq, path: p}
		switch qi.Kind {
		case kindPath, "":
			item.kind = kindPath
			item.key = w.Key()
		case kindGuard:
			if qi.Page == nil {
				return nil, fmt.Errorf("graphwalk: guard checkpoint lacks page for path %v", w.IDs)
			}
			page := *qi.Page
			item.kind = kindGuard
			item.page = &page
			item.key = guardKey(p, page)
		default:
			return nil, fmt.Errorf("graphwalk: unknown checkpoint queue kind %q", qi.Kind)
		}
		c.pq = append(c.pq, item)
	}
	for i := range c.pq {
		c.pq[i].index = i
	}
	heap.Init(&c.pq)
	if cp.Seq > maxSeq {
		maxSeq = cp.Seq
	}
	c.seq = maxSeq
	c.availability = opts.Availability
	return c, nil
}

// ValidateVersions checks every fixed page ref using the optional availability
// callback without reading page details.
func (c *Cursor) ValidateVersions() error {
	c.mu.Lock()
	avail := c.availability
	refs := make([]PageRef, 0, len(c.pages))
	version := c.catalogVersion
	for _, p := range c.pages {
		refs = append(refs, p.Ref())
	}
	c.mu.Unlock()
	if avail == nil {
		return nil
	}
	var bad []PageRef
	for _, ref := range refs {
		if !avail(ref) {
			bad = append(bad, ref)
		}
	}
	if len(bad) > 0 {
		return &StaleVersionError{CatalogVersion: version, UnavailablePages: bad}
	}
	return nil
}

// CheckContext is a small helper for callers that want a cancellation point.
func CheckContext(ctx context.Context) error { return ctx.Err() }
