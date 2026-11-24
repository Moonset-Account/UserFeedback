package graphwalk

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
)

// Vertex identifies a graph vertex.
type Vertex string

// EdgeID is the globally stable ordering identifier of an edge. Zero is
// reserved as a lower-bound sentinel and must not be used for a real edge.
type EdgeID uint64

// PageID identifies a page across catalog revisions.
type PageID string

// PageVersion is an immutable page version token.
type PageVersion string

// CatalogVersion identifies a complete directory revision.
type CatalogVersion string

// Edge is one detailed directed edge returned by a page read callback.
type Edge struct {
	ID    EdgeID `json:"id"`
	From  Vertex `json:"from"`
	To    Vertex `json:"to"`
	Label string `json:"label"`
	Cost  int64  `json:"cost"`
}

// PageRef names one immutable page version.
type PageRef struct {
	ID      PageID      `json:"id"`
	Version PageVersion `json:"version"`
}

// PageInfo is the directory-only metadata. It deliberately contains no edge
// details. MinLabel/MaxLabel are an inclusive lexicographic range; empty
// endpoints mean unbounded on that side.
type PageInfo struct {
	ID       PageID      `json:"id"`
	Version  PageVersion `json:"version"`
	From     Vertex      `json:"from"`
	MinLabel string      `json:"min_label"`
	MaxLabel string      `json:"max_label"`
	MinCost  int64       `json:"min_cost"`
}

// Ref returns the immutable page reference.
func (p PageInfo) Ref() PageRef { return PageRef{ID: p.ID, Version: p.Version} }

// CoversLabel reports whether label can be inside the page's inclusive range.
func (p PageInfo) CoversLabel(label string) bool {
	if p.MinLabel != "" && label < p.MinLabel {
		return false
	}
	if p.MaxLabel != "" && label > p.MaxLabel {
		return false
	}
	return true
}

// CoversAnyLabel reports whether any possible label intersects the range.
func (p PageInfo) CoversAnyLabel(labels map[string]bool) bool {
	if len(labels) == 0 {
		return false
	}
	for label := range labels {
		if p.CoversLabel(label) {
			return true
		}
	}
	return false
}

// Catalog is the fixed directory view used by one cursor revision.
type Catalog interface {
	Version() CatalogVersion
	Pages() []PageInfo
}

// ReadPageFunc is the lazy detail callback. Implementations should return
// ErrPageVersionUnavailable (possibly wrapped) for an expired page version.
type ReadPageFunc func(ctx context.Context, ref PageRef) ([]Edge, error)

// ErrDone is returned by Next when the requested k or finite result set ends.
var ErrDone = errors.New("graphwalk: no more walks")

// ErrPageVersionUnavailable is the sentinel for an expired immutable page.
var ErrPageVersionUnavailable = errors.New("graphwalk: page version unavailable")

// StaleVersionError identifies fixed-version page references that cannot be
// read after a directory/page revision has expired them.
type StaleVersionError struct {
	CatalogVersion   CatalogVersion `json:"catalog_version"`
	UnavailablePages []PageRef      `json:"unavailable_pages"`
	Cause            error          `json:"-"`
}

func (e *StaleVersionError) Error() string {
	refs := make([]string, 0, len(e.UnavailablePages))
	for _, ref := range e.UnavailablePages {
		refs = append(refs, fmt.Sprintf("%s@%s", ref.ID, ref.Version))
	}
	return fmt.Sprintf("graphwalk: catalog %s uses unavailable page version(s): %v", e.CatalogVersion, refs)
}

func (e *StaleVersionError) Unwrap() error {
	if e.Cause != nil {
		return e.Cause
	}
	return ErrPageVersionUnavailable
}

// Is reports whether target is ErrPageVersionUnavailable.
func (e *StaleVersionError) Is(target error) bool {
	return target == ErrPageVersionUnavailable
}

// StaticCatalog is an in-memory fixed directory.
type StaticCatalog struct {
	CatalogID CatalogVersion `json:"catalog_version"`
	PageList  []PageInfo     `json:"pages"`
}

// NewCatalog creates a deterministic fixed catalog.
func NewCatalog(version string, pages ...PageInfo) *StaticCatalog {
	cp := make([]PageInfo, len(pages))
	copy(cp, pages)
	sort.SliceStable(cp, func(i, j int) bool {
		if cp[i].ID != cp[j].ID {
			return cp[i].ID < cp[j].ID
		}
		return cp[i].Version < cp[j].Version
	})
	return &StaticCatalog{CatalogID: CatalogVersion(version), PageList: cp}
}

func (c *StaticCatalog) Version() CatalogVersion { return c.CatalogID }
func (c *StaticCatalog) Pages() []PageInfo {
	out := make([]PageInfo, len(c.PageList))
	copy(out, c.PageList)
	return out
}

func sortPages(pages []PageInfo) {
	sort.SliceStable(pages, func(i, j int) bool {
		if pages[i].From != pages[j].From {
			return pages[i].From < pages[j].From
		}
		if pages[i].ID != pages[j].ID {
			return pages[i].ID < pages[j].ID
		}
		return pages[i].Version < pages[j].Version
	})
}

// Key is the required total ordering key.
type Key struct {
	Cost int64    `json:"cost"`
	Hops int      `json:"hops"`
	IDs  []EdgeID `json:"ids"`
}

// CompareKey orders by cost, edge count, then lexicographic edge IDs.
func CompareKey(a, b Key) int {
	if a.Cost != b.Cost {
		if a.Cost < b.Cost {
			return -1
		}
		return 1
	}
	if a.Hops != b.Hops {
		if a.Hops < b.Hops {
			return -1
		}
		return 1
	}
	n := len(a.IDs)
	if len(b.IDs) < n {
		n = len(b.IDs)
	}
	for i := 0; i < n; i++ {
		if a.IDs[i] != b.IDs[i] {
			if a.IDs[i] < b.IDs[i] {
				return -1
			}
			return 1
		}
	}
	switch {
	case len(a.IDs) < len(b.IDs):
		return -1
	case len(a.IDs) > len(b.IDs):
		return 1
	default:
		return 0
	}
}

// Walk is one source-to-target label-matching edge sequence.
type Walk struct {
	Source Vertex   `json:"source"`
	Target Vertex   `json:"target"`
	Edges  []Edge   `json:"edges"`
	IDs    []EdgeID `json:"ids"`
	Labels []string `json:"labels"`
	Cost   int64    `json:"cost"`
	Hops   int      `json:"hops"`
}

// Key returns the ordering key for the walk.
func (w Walk) Key() Key {
	ids := make([]EdgeID, len(w.IDs))
	copy(ids, w.IDs)
	return Key{Cost: w.Cost, Hops: w.Hops, IDs: ids}
}

func walksEqual(a, b Walk) bool {
	if len(a.IDs) != len(b.IDs) {
		return false
	}
	for i := range a.IDs {
		if a.IDs[i] != b.IDs[i] {
			return false
		}
	}
	return true
}

// SortWalks sorts walks by the required key and removes duplicate edge-ID
// sequences. The first occurrence is retained.
func SortWalks(walks []Walk) []Walk {
	sort.SliceStable(walks, func(i, j int) bool {
		return CompareKey(walks[i].Key(), walks[j].Key()) < 0
	})
	out := make([]Walk, 0, len(walks))
	seen := map[string]bool{}
	for _, w := range walks {
		key := fmt.Sprint(w.IDs)
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, w)
	}
	return out
}

// MemoryReader is a thread-safe synthetic page callback used by tests and
// local embeddings. It records opens so laziness can be asserted.
type MemoryReader struct {
	mu     sync.Mutex
	pages  map[PageRef][]Edge
	opens  []PageRef
	counts map[PageRef]int
}

// NewMemoryReader creates an empty reader.
func NewMemoryReader() *MemoryReader {
	return &MemoryReader{pages: map[PageRef][]Edge{}, counts: map[PageRef]int{}}
}

// Put installs one immutable page version.
func (r *MemoryReader) Put(info PageInfo, edges []Edge) {
	r.mu.Lock()
	defer r.mu.Unlock()
	cp := make([]Edge, len(edges))
	copy(cp, edges)
	r.pages[info.Ref()] = cp
}

// PutRef installs edges under an explicit ref.
func (r *MemoryReader) PutRef(ref PageRef, edges []Edge) {
	r.mu.Lock()
	defer r.mu.Unlock()
	cp := make([]Edge, len(edges))
	copy(cp, edges)
	r.pages[ref] = cp
}

// ReadPage implements ReadPageFunc.
func (r *MemoryReader) ReadPage(_ context.Context, ref PageRef) ([]Edge, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.opens = append(r.opens, ref)
	r.counts[ref]++
	edges, ok := r.pages[ref]
	if !ok {
		return nil, fmt.Errorf("%w: %s@%s", ErrPageVersionUnavailable, ref.ID, ref.Version)
	}
	out := make([]Edge, len(edges))
	copy(out, edges)
	return out, nil
}

// OpenCount returns callback opens for one exact page version.
func (r *MemoryReader) OpenCount(ref PageRef) int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.counts[ref]
}

// TotalOpens returns all callback opens.
func (r *MemoryReader) TotalOpens() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.opens)
}

// Opens returns a copy of all opened refs in call order.
func (r *MemoryReader) Opens() []PageRef {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]PageRef, len(r.opens))
	copy(out, r.opens)
	return out
}

// Available reports whether an exact immutable ref is installed.
func (r *MemoryReader) Available(ref PageRef) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	_, ok := r.pages[ref]
	return ok
}

// AvailabilityFunc adapts MemoryReader.Available.
func (r *MemoryReader) AvailabilityFunc() func(PageRef) bool {
	return r.Available
}
