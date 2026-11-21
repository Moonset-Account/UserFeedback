// Package relationindex provides a snapshot-isolated, ordered index over
// relation rows together with bidirectional keyset pagination.
//
// The display order is:
//
//  1. region ascending (byte-wise string order),
//  2. score descending,
//  3. rows with no score after every scored row of the same region,
//  4. primary key ascending (byte-wise string order) as the final tie-break.
//
// The primary key makes the order total, so rows that share a region and a
// score can safely straddle page boundaries without duplication or loss.
package relationindex

import (
	"errors"
	"fmt"
	"math"
	"sort"
	"sync/atomic"
)

var (
	// ErrInvalidPageSize is returned when a page size is not positive.
	ErrInvalidPageSize = errors.New("relationindex: page size must be positive")
	// ErrInvalidRow is returned for a row with an empty key, or with a
	// non-finite score.
	ErrInvalidRow = errors.New("relationindex: invalid row")
	// ErrDuplicateID is returned when a snapshot input contains the same
	// primary key twice.
	ErrDuplicateID = errors.New("relationindex: duplicate primary key")
	// ErrInvalidCursor is returned when a cursor is malformed or corrupt.
	ErrInvalidCursor = errors.New("relationindex: malformed cursor")
	// ErrCursorSnapshotMismatch is returned when a cursor produced by one
	// snapshot is presented to another snapshot.
	ErrCursorSnapshotMismatch = errors.New("relationindex: cursor belongs to a different snapshot")
)

// Row is one relation row. A nil Score means "no score"; such rows sort
// after all scored rows of the same region.
type Row struct {
	ID     string
	Region string
	Score  *float64
}

// NewScore returns a pointer to v, which is convenient for literal rows.
func NewScore(v float64) *float64 {
	return &v
}

func validateRow(r Row) error {
	if r.ID == "" {
		return fmt.Errorf("%w: empty primary key", ErrInvalidRow)
	}
	if r.Score != nil {
		if math.IsNaN(*r.Score) || math.IsInf(*r.Score, 0) {
			return fmt.Errorf("%w: score for %q is not finite", ErrInvalidRow, r.ID)
		}
	}
	return nil
}

func cloneRow(r Row) Row {
	out := Row{ID: r.ID, Region: r.Region}
	if r.Score != nil {
		out.Score = NewScore(*r.Score)
	}
	return out
}

// indexKey is the immutable sort key stored in the snapshot index.
type indexKey struct {
	region   string
	score    float64
	hasScore bool
	id       string
}

func keyOfRow(r Row) indexKey {
	k := indexKey{region: r.Region, id: r.ID}
	if r.Score != nil {
		k.score = *r.Score
		k.hasScore = true
	}
	return k
}

func (k indexKey) row() Row {
	out := Row{ID: k.id, Region: k.region}
	if k.hasScore {
		out.Score = NewScore(k.score)
	}
	return out
}

// compareKeys reports whether a sorts before (-1), equal to (0), or after
// (+1) b in display order.
func compareKeys(a, b indexKey) int {
	if a.region != b.region {
		if a.region < b.region {
			return -1
		}
		return 1
	}
	if a.hasScore != b.hasScore {
		if a.hasScore {
			return -1 // scored rows before unscored rows in the same region
		}
		return 1
	}
	if a.hasScore {
		if a.score > b.score {
			return -1
		}
		if a.score < b.score {
			return 1
		}
	}
	if a.id < b.id {
		return -1
	}
	if a.id > b.id {
		return 1
	}
	return 0
}

// Stats reports how much of the snapshot index a page request touched.
//
// Comparisons counts index entries compared while binary-searching for the
// page boundary; Visited counts index entries materialised into the page.
// The sum is the number of index entries compared or accessed.
type Stats struct {
	Comparisons int
	Visited     int
}

// IndexEntries returns Comparisons + Visited.
func (s Stats) IndexEntries() int {
	return s.Comparisons + s.Visited
}

// Page is one page of rows in display order plus the cursors that continue
// navigation from either end.
//
// PrevCursor points immediately before the first row of the page and is meant
// for FetchBefore; NextCursor points immediately after the last row and is
// meant for FetchAfter. Both are opaque and scoped to the snapshot that
// produced them. HasPrev/HasNext say whether rows exist beyond each end.
// An empty page (only possible when the requested boundary is at an edge) has
// no rows and no cursors.
type Page struct {
	Rows       []Row
	PrevCursor string
	NextCursor string
	HasPrev    bool
	HasNext    bool
	Stats      Stats
}

var snapshotIDCounter uint64

// Snapshot is an immutable ordered index built once from a set of rows.
// Mutating the Store that produced it never changes an existing Snapshot.
type Snapshot struct {
	id      uint64
	entries []indexKey
}

// NewSnapshot validates rows, copies them, sorts them into display order and
// freezes the result. It rejects empty IDs, duplicate IDs and non-finite
// scores.
func NewSnapshot(rows []Row) (*Snapshot, error) {
	seen := make(map[string]struct{}, len(rows))
	entries := make([]indexKey, 0, len(rows))
	for _, r := range rows {
		if err := validateRow(r); err != nil {
			return nil, err
		}
		if _, ok := seen[r.ID]; ok {
			return nil, fmt.Errorf("%w: %q", ErrDuplicateID, r.ID)
		}
		seen[r.ID] = struct{}{}
		entries = append(entries, keyOfRow(r))
	}
	sort.Slice(entries, func(i, j int) bool {
		return compareKeys(entries[i], entries[j]) < 0
	})
	return &Snapshot{
		id:      atomic.AddUint64(&snapshotIDCounter, 1),
		entries: entries,
	}, nil
}

// SnapshotID returns the identity used to scope cursors to this snapshot.
func (s *Snapshot) SnapshotID() uint64 {
	return s.id
}

// Len returns the number of indexed rows.
func (s *Snapshot) Len() int {
	return len(s.entries)
}

// FirstPage returns the first page in display order. An empty cursor means
// "from the start" for FetchAfter.
func (s *Snapshot) FirstPage(pageSize int) (Page, error) {
	return s.fetch("", pageSize, false)
}

// LastPage returns the final page in display order. An empty cursor means
// "from the end" for FetchBefore.
func (s *Snapshot) LastPage(pageSize int) (Page, error) {
	return s.fetch("", pageSize, true)
}

// FetchAfter returns up to pageSize rows that sort strictly after cursor, in
// display order. An empty cursor starts at the beginning of the snapshot.
func (s *Snapshot) FetchAfter(cursor string, pageSize int) (Page, error) {
	return s.fetch(cursor, pageSize, false)
}

// FetchBefore returns up to pageSize rows that sort strictly before cursor,
// still in display order (not reversed). An empty cursor starts at the end of
// the snapshot.
func (s *Snapshot) FetchBefore(cursor string, pageSize int) (Page, error) {
	return s.fetch(cursor, pageSize, true)
}

func (s *Snapshot) fetch(cursor string, pageSize int, backwards bool) (Page, error) {
	if pageSize <= 0 {
		return Page{}, ErrInvalidPageSize
	}

	stats := Stats{}
	boundary := 0
	if backwards {
		boundary = len(s.entries)
	}

	if cursor != "" {
		key, snapshotID, err := decodeCursor(cursor)
		if err != nil {
			return Page{}, err
		}
		if snapshotID != s.id {
			return Page{}, ErrCursorSnapshotMismatch
		}
		if backwards {
			// First entry not before the boundary; everything before it is
			// the preceding page.
			boundary = s.lowerBound(key, &stats)
		} else {
			// First entry strictly after the boundary.
			boundary = s.upperBound(key, &stats)
		}
	}

	start, end := 0, 0
	if backwards {
		end = boundary
		start = end - pageSize
		if start < 0 {
			start = 0
		}
	} else {
		start = boundary
		end = start + pageSize
		if end > len(s.entries) {
			end = len(s.entries)
		}
	}

	stats.Visited = end - start
	page := Page{
		Rows:    make([]Row, 0, stats.Visited),
		Stats:   stats,
		HasPrev: stats.Visited > 0 && start > 0,
		HasNext: stats.Visited > 0 && end < len(s.entries),
	}
	for i := start; i < end; i++ {
		page.Rows = append(page.Rows, s.entries[i].row())
	}
	if stats.Visited > 0 {
		page.PrevCursor = encodeCursor(s.id, s.entries[start])
		page.NextCursor = encodeCursor(s.id, s.entries[end-1])
	}
	return page, nil
}

// lowerBound returns the first index whose key is not before key.
// It counts every index comparison in stats.
func (s *Snapshot) lowerBound(key indexKey, stats *Stats) int {
	lo, hi := 0, len(s.entries)
	for lo < hi {
		mid := int(uint(lo+hi) >> 1)
		stats.Comparisons++
		if compareKeys(s.entries[mid], key) < 0 {
			lo = mid + 1
		} else {
			hi = mid
		}
	}
	return lo
}

// upperBound returns the first index whose key is strictly after key.
// It counts every index comparison in stats.
func (s *Snapshot) upperBound(key indexKey, stats *Stats) int {
	lo, hi := 0, len(s.entries)
	for lo < hi {
		mid := int(uint(lo+hi) >> 1)
		stats.Comparisons++
		if compareKeys(s.entries[mid], key) <= 0 {
			lo = mid + 1
		} else {
			hi = mid
		}
	}
	return lo
}
