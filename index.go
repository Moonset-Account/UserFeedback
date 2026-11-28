package warmgrid

import (
	"fmt"
	"sort"
	"sync"
)

// RecordID is a caller-supplied stable identifier for a main record.
type RecordID string

// record is the main record: stored exactly once regardless of how many grid
// cells its geometry covers. It is immutable once published in a snapshot.
type record struct {
	id      RecordID
	version uint64
	geom    Polyline
	bounds  BBox
	cells   []CellKey
}

// Record is the exported, copy-safe view of a main record.
type Record struct {
	ID      RecordID
	Version uint64
	Geom    Polyline
	Bounds  BBox
	Cells   []CellKey
}

// snapshot is an immutable view of the whole index. Published snapshots share
// unchanged cell buckets and records; a write clones only the buckets it
// touches (copy-on-write), which keeps a local redraw local.
type snapshot struct {
	seq     uint64
	records map[RecordID]*record
	cells   map[CellKey]map[RecordID]uint64
}

func newSnapshot(seq uint64) *snapshot {
	return &snapshot{
		seq:     seq,
		records: make(map[RecordID]*record),
		cells:   make(map[CellKey]map[RecordID]uint64),
	}
}

// clone copies only the top-level maps. Cell buckets are still shared and are
// cloned lazily by addTerm/removeTerm before any mutation.
func (s *snapshot) clone() *snapshot {
	records := make(map[RecordID]*record, len(s.records))
	for id, rec := range s.records {
		records[id] = rec
	}
	cells := make(map[CellKey]map[RecordID]uint64, len(s.cells))
	for key, bucket := range s.cells {
		cells[key] = bucket
	}
	return &snapshot{seq: s.seq, records: records, cells: cells}
}

func (s *snapshot) addTerm(key CellKey, id RecordID, version uint64) {
	bucket := s.cells[key]
	cloned := make(map[RecordID]uint64, len(bucket)+1)
	for otherID, otherVersion := range bucket {
		cloned[otherID] = otherVersion
	}
	cloned[id] = version
	s.cells[key] = cloned
}

func (s *snapshot) removeTerm(key CellKey, id RecordID) {
	bucket, ok := s.cells[key]
	if !ok {
		return
	}
	if _, exists := bucket[id]; !exists {
		return
	}
	cloned := make(map[RecordID]uint64, len(bucket))
	for otherID, otherVersion := range bucket {
		if otherID != id {
			cloned[otherID] = otherVersion
		}
	}
	if len(cloned) == 0 {
		delete(s.cells, key)
		return
	}
	s.cells[key] = cloned
}

// putRecord stores rec, first removing the previous version from every cell it
// used to occupy. Only the union of old and new cover cells is touched.
func (s *snapshot) putRecord(rec *record) {
	if old, ok := s.records[rec.id]; ok {
		for _, key := range old.cells {
			s.removeTerm(key, rec.id)
		}
	}
	s.records[rec.id] = rec
	for _, key := range rec.cells {
		s.addTerm(key, rec.id, rec.version)
	}
}

// deleteRecord removes a record and all of its cell terms.
func (s *snapshot) deleteRecord(id RecordID) bool {
	old, ok := s.records[id]
	if !ok {
		return false
	}
	for _, key := range old.cells {
		s.removeTerm(key, id)
	}
	delete(s.records, id)
	return true
}

// Snapshot is an immutable read view bound to the grid that produced it. It
// remains valid and internally consistent after later writes, so an old query
// can keep working on old record versions while a new snapshot observes the new
// cover cells.
//
// A Snapshot is safe for concurrent use.
type Snapshot struct {
	state *snapshot
	grid  Grid
}

// Seq returns the monotonically increasing snapshot sequence number.
func (s *Snapshot) Seq() uint64 { return s.state.seq }

// Len returns the number of live main records.
func (s *Snapshot) Len() int { return len(s.state.records) }

// Record returns a copy of the record with the given id.
func (s *Snapshot) Record(id RecordID) (Record, bool) {
	rec, ok := s.state.records[id]
	if !ok {
		return Record{}, false
	}
	return exportRecord(rec), true
}

// All returns copies of every record in the snapshot, ordered by ID. It exists
// for diagnostics and for linear-scan reference checks in tests.
func (s *Snapshot) All() []Record {
	out := make([]Record, 0, len(s.state.records))
	for _, rec := range s.state.records {
		out = append(out, exportRecord(rec))
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func exportRecord(rec *record) Record {
	cells := make([]CellKey, len(rec.cells))
	copy(cells, rec.cells)
	return Record{
		ID:      rec.id,
		Version: rec.version,
		Geom:    copyPolyline(rec.geom),
		Bounds:  rec.bounds,
		Cells:   cells,
	}
}

// Query runs a snapshot query: it collects candidate (id, version) terms from
// every covered cell, deduplicates them, fetches each main record once, and
// applies the exact boundary-relation filter.
func (s *Snapshot) Query(q Query) ([]Result, error) {
	if err := q.BBox.Validate(); err != nil {
		return nil, err
	}
	if !q.Filter.valid() {
		return nil, fmt.Errorf("%w: %d", ErrInvalidFilter, q.Filter)
	}
	cells, err := s.grid.CellsForBox(q.BBox)
	if err != nil {
		return nil, err
	}

	// Deduplicate by (id, version) across all covered cells. This collapses a
	// multi-cell geometry into one result and stops an outdated term from
	// surfacing a second version of the same record.
	type candidate struct {
		id      RecordID
		version uint64
	}
	seen := make(map[candidate]struct{}, 16)
	candidates := make([]candidate, 0, 16)
	for _, key := range cells {
		for id, version := range s.state.cells[key] {
			c := candidate{id: id, version: version}
			if _, ok := seen[c]; ok {
				continue
			}
			seen[c] = struct{}{}
			candidates = append(candidates, c)
		}
	}
	sort.Slice(candidates, func(i, j int) bool {
		if candidates[i].id != candidates[j].id {
			return candidates[i].id < candidates[j].id
		}
		return candidates[i].version < candidates[j].version
	})

	results := make([]Result, 0, len(candidates))
	for _, cand := range candidates {
		rec, ok := s.state.records[cand.id]
		if !ok || rec.version != cand.version {
			// A term whose record version disagrees is stale and can never
			// contribute a second result for the same record.
			continue
		}
		if !rec.bounds.Intersects(q.BBox) {
			continue
		}
		rel := PolylineRelation(rec.geom, q.BBox)
		if !q.Filter.accepts(rel) {
			continue
		}
		results = append(results, Result{
			ID:       rec.id,
			Version:  rec.version,
			Geom:     copyPolyline(rec.geom),
			Bounds:   rec.bounds,
			Relation: rel,
		})
	}
	return results, nil
}

// Result is one deduplicated record that satisfied a query.
type Result struct {
	ID       RecordID
	Version  uint64
	Geom     Polyline
	Bounds   BBox
	Relation Relation
}

// Filter selects an exact boundary relation.
type Filter uint8

const (
	// FilterAny accepts every candidate whose closed bounding box intersects
	// the query box, without an exact relation filter. Results still carry the
	// exact Relation for the caller.
	FilterAny Filter = iota
	// FilterIntersects accepts geometries touching or crossing the box.
	FilterIntersects
	// FilterInside accepts geometries fully contained in the closed box.
	FilterInside
	// FilterTouches accepts geometries that meet the box only on its boundary.
	FilterTouches
	// FilterCrosses accepts geometries with part strictly inside and part
	// outside the box.
	FilterCrosses
)

func (f Filter) valid() bool { return f <= FilterCrosses }

func (f Filter) accepts(r Relation) bool {
	switch f {
	case FilterAny:
		return true
	case FilterIntersects:
		return r != RelationDisjoint
	case FilterInside:
		return r == RelationInside
	case FilterTouches:
		return r == RelationTouches
	case FilterCrosses:
		return r == RelationCrosses
	}
	return false
}

// Query is a bounding-box selection plus an optional exact relation filter.
type Query struct {
	BBox   BBox
	Filter Filter
}

// ChangeKind is the operation applied by Change.
type ChangeKind uint8

const (
	// ChangeUpsert inserts a new record or locally redraws an existing one.
	ChangeUpsert ChangeKind = iota
	// ChangeDelete removes a record.
	ChangeDelete
)

// Change is one record edit for Apply.
type Change struct {
	Kind ChangeKind
	ID   RecordID
	Geom Polyline
}

// Stats reports committed index sizes.
type Stats struct {
	Records   int
	CellTerms int
	LiveCells int
	Seq       uint64
}

// Index is a concurrent, snapshot-isolated in-memory spatial index.
type Index struct {
	grid Grid

	mu          sync.RWMutex
	current     *snapshot
	nextVersion uint64
	nextSeq     uint64
}

// NewIndex creates an empty index over grid.
func NewIndex(grid Grid) *Index {
	return &Index{grid: grid, nextVersion: 1, nextSeq: 1, current: newSnapshot(0)}
}

// Grid returns the index grid.
func (ix *Index) Grid() Grid { return ix.grid }

// Snapshot returns the current immutable snapshot. Queries against it keep
// observing exactly the state at the time of this call.
func (ix *Index) Snapshot() *Snapshot {
	ix.mu.RLock()
	defer ix.mu.RUnlock()
	return &Snapshot{state: ix.current, grid: ix.grid}
}

// Query runs a query against the current snapshot.
func (ix *Index) Query(q Query) ([]Result, error) {
	return ix.Snapshot().Query(q)
}

// Insert adds a new record and returns its version.
func (ix *Index) Insert(id RecordID, geom Polyline) (uint64, error) {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	if _, exists := ix.current.records[id]; exists {
		return 0, fmt.Errorf("%w: %q", ErrExists, id)
	}
	rec, err := ix.buildRecord(id, geom, ix.nextVersion)
	if err != nil {
		return 0, err
	}
	ix.publishLocked(func(next *snapshot) { next.putRecord(rec) })
	ix.nextVersion++
	return rec.version, nil
}

// Update replaces an existing record and returns its new version. Only the old
// and new cover cells are rewritten; other cell buckets are shared with the
// previous snapshot.
func (ix *Index) Update(id RecordID, geom Polyline) (uint64, error) {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	if _, exists := ix.current.records[id]; !exists {
		return 0, fmt.Errorf("%w: %q", ErrNotFound, id)
	}
	rec, err := ix.buildRecord(id, geom, ix.nextVersion)
	if err != nil {
		return 0, err
	}
	ix.publishLocked(func(next *snapshot) { next.putRecord(rec) })
	ix.nextVersion++
	return rec.version, nil
}

// Delete removes a record.
func (ix *Index) Delete(id RecordID) error {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	if _, exists := ix.current.records[id]; !exists {
		return fmt.Errorf("%w: %q", ErrNotFound, id)
	}
	ix.publishLocked(func(next *snapshot) { next.deleteRecord(id) })
	return nil
}

// Apply atomically applies a batch of local redraws. Either every change takes
// effect in one new snapshot or none does, and it returns the new sequence.
func (ix *Index) Apply(changes ...Change) (uint64, error) {
	if len(changes) == 0 {
		ix.mu.RLock()
		seq := ix.current.seq
		ix.mu.RUnlock()
		return seq, nil
	}
	ix.mu.Lock()
	defer ix.mu.Unlock()

	seen := make(map[RecordID]struct{}, len(changes))
	for _, ch := range changes {
		if ch.ID == "" {
			return 0, fmt.Errorf("%w", ErrEmptyID)
		}
		if _, dup := seen[ch.ID]; dup {
			return 0, fmt.Errorf("%w: %q", ErrDuplicateChange, ch.ID)
		}
		seen[ch.ID] = struct{}{}
	}

	version := ix.nextVersion
	staged := make([]*record, len(changes))
	for i, ch := range changes {
		switch ch.Kind {
		case ChangeUpsert:
			rec, err := ix.buildRecord(ch.ID, ch.Geom, version)
			if err != nil {
				return 0, err
			}
			staged[i] = rec
			version++
		case ChangeDelete:
			if _, exists := ix.current.records[ch.ID]; !exists {
				return 0, fmt.Errorf("%w: %q", ErrNotFound, ch.ID)
			}
		default:
			return 0, fmt.Errorf("warmgrid: unknown change kind %d", ch.Kind)
		}
	}

	seq := ix.publishLocked(func(next *snapshot) {
		for i, ch := range changes {
			if ch.Kind == ChangeDelete {
				next.deleteRecord(ch.ID)
				continue
			}
			next.putRecord(staged[i])
		}
	})
	ix.nextVersion = version
	return seq, nil
}

// publishLocked clones the current snapshot, applies mutate, bumps the sequence
// and swaps the pointer. The caller must hold ix.mu for writing.
func (ix *Index) publishLocked(mutate func(*snapshot)) uint64 {
	next := ix.current.clone()
	ix.nextSeq++
	next.seq = ix.nextSeq
	mutate(next)
	ix.current = next
	return next.seq
}

// buildRecord validates a geometry, computes its cover cells and allocates an
// immutable main record.
func (ix *Index) buildRecord(id RecordID, geom Polyline, version uint64) (*record, error) {
	if id == "" {
		return nil, fmt.Errorf("%w", ErrEmptyID)
	}
	if err := geom.Validate(); err != nil {
		return nil, err
	}
	bounds := geom.Bounds()
	cells, err := ix.grid.CellsForBox(bounds)
	if err != nil {
		return nil, err
	}
	return &record{
		id:      id,
		version: version,
		geom:    copyPolyline(geom),
		bounds:  bounds,
		cells:   cells,
	}, nil
}

// Stats returns a point-in-time size snapshot.
func (ix *Index) Stats() Stats {
	ix.mu.RLock()
	defer ix.mu.RUnlock()
	s := Stats{Records: len(ix.current.records), Seq: ix.current.seq}
	for _, bucket := range ix.current.cells {
		s.CellTerms += len(bucket)
	}
	s.LiveCells = len(ix.current.cells)
	return s
}

// CellTerms returns how many index entries the current snapshot holds in key.
func (ix *Index) CellTerms(key CellKey) int {
	ix.mu.RLock()
	defer ix.mu.RUnlock()
	return len(ix.current.cells[key])
}
