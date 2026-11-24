package graphwalk

import (
	"container/heap"
	"context"
	"errors"
	"fmt"
	"sync"
)

const MaxAllowedHops = 32

// Query describes one bounded label-walk query.
type Query struct {
	Source     Vertex
	Target     Vertex
	Expression string
	Expr       Expr
	MaxHops    int
}

// Cursor is a lazy fixed-catalog-version top-k walk cursor.
type Cursor struct {
	mu sync.Mutex

	query Query
	k     int
	nfa   *nfa

	catalogVersion CatalogVersion
	pages          []PageInfo
	pageIndex      map[PageID]PageInfo
	reader         ReadPageFunc
	availability   func(PageRef) bool

	fetched map[PageID]*fetchedPage
	seen    map[string]struct{}

	pq      queueHeap
	seq     uint64
	emitted []Walk
	certs   []RankCertificate

	limitReached bool
}

type fetchedPage struct {
	Info            PageInfo `json:"info"`
	Edges           []Edge   `json:"edges"`
	ObservedMinCost int64    `json:"observed_min_cost"`
}

type pathState struct {
	walk      Walk
	end       Vertex
	nfaStates []int
}

type itemKind string

const (
	kindPath  itemKind = "path"
	kindGuard itemKind = "page-guard"
)

type queueItem struct {
	kind  itemKind
	seq   uint64
	key   Key
	path  *pathState
	page  *PageInfo
	index int
}

type queueHeap []*queueItem

func (h queueHeap) Len() int { return len(h) }
func (h queueHeap) Less(i, j int) bool {
	cmp := CompareKey(h[i].key, h[j].key)
	if cmp != 0 {
		return cmp < 0
	}
	if h[i].kind != h[j].kind {
		return h[i].kind == kindGuard
	}
	return h[i].seq < h[j].seq
}
func (h queueHeap) Swap(i, j int) {
	h[i], h[j] = h[j], h[i]
	h[i].index = i
	h[j].index = j
}
func (h *queueHeap) Push(x any) {
	item := x.(*queueItem)
	item.index = len(*h)
	*h = append(*h, item)
}
func (h *queueHeap) Pop() any {
	old := *h
	n := len(old)
	item := old[n-1]
	old[n-1] = nil
	*h = old[:n-1]
	item.index = -1
	return item
}

func (h queueHeap) peek() *queueItem {
	if len(h) == 0 {
		return nil
	}
	return h[0]
}

// NewCursor creates a lazy cursor. The catalog snapshot is fixed for the
// lifetime of this cursor; checkpoints resume those exact page versions.
func NewCursor(query Query, k int, catalog Catalog, reader ReadPageFunc) (*Cursor, error) {
	if k < 0 {
		return nil, fmt.Errorf("graphwalk: k must be non-negative")
	}
	if catalog == nil {
		return nil, fmt.Errorf("graphwalk: catalog is required")
	}
	if reader == nil {
		return nil, fmt.Errorf("graphwalk: page read callback is required")
	}
	q := query
	if q.MaxHops == 0 {
		q.MaxHops = MaxAllowedHops
	}
	if q.MaxHops < 0 || q.MaxHops > MaxAllowedHops {
		return nil, fmt.Errorf("graphwalk: MaxHops must be in [0,%d]", MaxAllowedHops)
	}
	expr := q.Expr
	if expr == nil {
		var err error
		expr, err = ParseExpr(q.Expression)
		if err != nil {
			return nil, err
		}
	}
	q.Expr = expr
	n := buildNFA(expr)
	pages := append([]PageInfo(nil), catalog.Pages()...)
	sortPages(pages)
	index := map[PageID]PageInfo{}
	for _, p := range pages {
		if p.ID == "" {
			return nil, fmt.Errorf("graphwalk: page id is required")
		}
		if p.Version == "" {
			return nil, fmt.Errorf("graphwalk: page %s version is required", p.ID)
		}
		if p.MinCost < 0 {
			return nil, fmt.Errorf("graphwalk: page %s has negative MinCost", p.ID)
		}
		if old, ok := index[p.ID]; ok && old.Version != p.Version {
			return nil, fmt.Errorf("graphwalk: catalog contains conflicting versions for page %s", p.ID)
		}
		index[p.ID] = p
	}

	c := &Cursor{
		query:          q,
		k:              k,
		nfa:            n,
		catalogVersion: catalog.Version(),
		pages:          pages,
		pageIndex:      index,
		reader:         reader,
		fetched:        map[PageID]*fetchedPage{},
		seen:           map[string]struct{}{},
	}
	startStates := n.startSet()
	start := &pathState{
		walk: Walk{
			Source: q.Source,
			Target: q.Target,
			Edges:  []Edge{},
			IDs:    []EdgeID{},
			Labels: []string{},
			Cost:   0,
			Hops:   0,
		},
		end:       q.Source,
		nfaStates: append([]int(nil), startStates...),
	}
	c.pushPathLocked(start)
	return c, nil
}

// K returns the cursor's top-k limit.
func (c *Cursor) K() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.k
}

// Rank returns the number of already emitted results.
func (c *Cursor) Rank() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.emitted)
}

// Results returns a copy of the emitted prefix.
func (c *Cursor) Results() []Walk {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]Walk, len(c.emitted))
	for i, w := range c.emitted {
		out[i] = cloneWalk(w)
	}
	return out
}

// Next returns the next walk in the required total order. It returns
// ErrDone after k results or when the bounded finite search is exhausted.
func (c *Cursor) Next(ctx context.Context) (Walk, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	if err := ctx.Err(); err != nil {
		return Walk{}, err
	}
	if c.k == 0 || c.limitReached || len(c.emitted) >= c.k {
		c.limitReached = true
		return Walk{}, ErrDone
	}

	for {
		if err := ctx.Err(); err != nil {
			return Walk{}, err
		}
		item := c.pq.peek()
		if item == nil {
			return Walk{}, ErrDone
		}
		heap.Pop(&c.pq)

		if item.kind == kindGuard {
			if err := c.openGuardLocked(ctx, item); err != nil {
				heap.Push(&c.pq, item)
				return Walk{}, err
			}
			continue
		}

		p := item.path
		accepting := c.nfa.acceptsSet(p.nfaStates) && p.end == c.query.Target
		c.expandPathLocked(p)

		if accepting {
			w := cloneWalk(p.walk)
			c.emitted = append(c.emitted, w)
			c.certs = append(c.certs, c.makeCertificateLocked(w))
			if len(c.emitted) >= c.k {
				c.limitReached = true
			}
			return cloneWalk(w), nil
		}
	}
}

func (c *Cursor) pushPathLocked(p *pathState) bool {
	sig := sequenceSignature(p.walk.IDs)
	if _, ok := c.seen[sig]; ok {
		return false
	}
	c.seen[sig] = struct{}{}
	c.seq++
	item := &queueItem{kind: kindPath, seq: c.seq, key: p.walk.Key(), path: p}
	heap.Push(&c.pq, item)
	return true
}

func sequenceSignature(ids []EdgeID) string {
	b := make([]byte, 0, len(ids)*8)
	for _, id := range ids {
		var x [8]byte
		v := uint64(id)
		for i := 7; i >= 0; i-- {
			x[i] = byte(v)
			v >>= 8
		}
		b = append(b, x[:]...)
	}
	return string(b)
}

func guardKey(p *pathState, info PageInfo) Key {
	ids := make([]EdgeID, 0, p.walk.Hops+1)
	ids = append(ids, p.walk.IDs...)
	ids = append(ids, 0)
	return Key{Cost: saturatingAdd(p.walk.Cost, info.MinCost), Hops: p.walk.Hops + 1, IDs: ids}
}

func (c *Cursor) addGuardLocked(p *pathState, info PageInfo) {
	c.seq++
	key := guardKey(p, info)
	pageCopy := info
	item := &queueItem{kind: kindGuard, seq: c.seq, key: key, path: p, page: &pageCopy}
	heap.Push(&c.pq, item)
}

func saturatingAdd(a, b int64) int64 {
	if b < 0 {
		b = 0
	}
	if a > maxInt64-b {
		return maxInt64
	}
	return a + b
}

const maxInt64 = int64(^uint64(0) >> 1)

func (c *Cursor) expandPathLocked(p *pathState) {
	if p.walk.Hops >= c.query.MaxHops {
		return
	}
	possible := c.nfa.possibleLabels(p.nfaStates)
	if len(possible) == 0 {
		return
	}
	for _, info := range c.pages {
		if info.From != p.end || !info.CoversAnyLabel(possible) {
			continue
		}
		if fp, ok := c.fetched[info.ID]; ok && fp.Info.Version == info.Version {
			c.scanFetchedPageLocked(p, fp)
		} else {
			c.addGuardLocked(p, info)
		}
	}
}

func (c *Cursor) openGuardLocked(ctx context.Context, item *queueItem) error {
	ref := item.page.Ref()
	fp, ok := c.fetched[ref.ID]
	if !ok || fp.Info.Version != ref.Version {
		if c.reader == nil {
			return &StaleVersionError{CatalogVersion: c.catalogVersion, UnavailablePages: []PageRef{ref}, Cause: errors.New("no page reader")}
		}
		edges, err := c.reader(ctx, ref)
		if err != nil {
			if errors.Is(err, ErrPageVersionUnavailable) {
				return &StaleVersionError{CatalogVersion: c.catalogVersion, UnavailablePages: []PageRef{ref}, Cause: err}
			}
			return fmt.Errorf("graphwalk: read page %s: %w", ref.ID, err)
		}
		var minCost int64 = -1
		for _, e := range edges {
			if e.Cost < 0 {
				return fmt.Errorf("graphwalk: page %s edge %d has negative cost", ref.ID, e.ID)
			}
			if e.ID == 0 {
				return fmt.Errorf("graphwalk: page %s contains reserved zero edge id", ref.ID)
			}
			if minCost < 0 || e.Cost < minCost {
				minCost = e.Cost
			}
		}
		if minCost < 0 {
			minCost = item.page.MinCost
		}
		fp = &fetchedPage{Info: *item.page, Edges: append([]Edge(nil), edges...), ObservedMinCost: minCost}
		c.fetched[ref.ID] = fp
	}
	c.scanFetchedPageLocked(item.path, fp)
	return nil
}

func (c *Cursor) scanFetchedPageLocked(parent *pathState, page *fetchedPage) {
	if parent.walk.Hops >= c.query.MaxHops {
		return
	}
	for _, edge := range page.Edges {
		if edge.From != parent.end {
			continue
		}
		nextStates := c.nfa.move(parent.nfaStates, edge.Label)
		if len(nextStates) == 0 {
			continue
		}
		edges := make([]Edge, 0, parent.walk.Hops+1)
		edges = append(edges, parent.walk.Edges...)
		edges = append(edges, edge)

		ids := make([]EdgeID, 0, parent.walk.Hops+1)
		ids = append(ids, parent.walk.IDs...)
		ids = append(ids, edge.ID)

		labels := make([]string, 0, parent.walk.Hops+1)
		labels = append(labels, parent.walk.Labels...)
		labels = append(labels, edge.Label)

		child := &pathState{
			walk: Walk{
				Source: c.query.Source,
				Target: c.query.Target,
				Edges:  edges,
				IDs:    ids,
				Labels: labels,
				Cost:   saturatingAdd(parent.walk.Cost, edge.Cost),
				Hops:   parent.walk.Hops + 1,
			},
			end:       edge.To,
			nfaStates: nextStates,
		}
		c.pushPathLocked(child)
	}
}

func cloneWalk(w Walk) Walk {
	w.Edges = append([]Edge(nil), w.Edges...)
	w.IDs = append([]EdgeID(nil), w.IDs...)
	w.Labels = append([]string(nil), w.Labels...)
	return w
}
