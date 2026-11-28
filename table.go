package heap

import (
	"errors"
	"sync"
)

// PageID identifies a page within a table.
type PageID int

// Location is the stable address of a record: page plus slot number.
// Compaction moves bytes inside a page but never changes slot numbers, so
// a Location stays valid until the record is deleted.
type Location struct {
	Page PageID
	Slot int
}

var (
	// ErrRecordTooLarge is returned when a record cannot fit on an empty page.
	ErrRecordTooLarge = errors.New("heap: record too large for an empty page")
	// ErrPageFull is returned by Place when the picked page cannot hold the
	// record even after one compaction; pick another page and retry.
	ErrPageFull = errors.New("heap: picked page cannot hold the record")
	// ErrNotFound is returned for unknown pages or out-of-range slots.
	ErrNotFound = errors.New("heap: location not found")
	// ErrNotLive is returned when a slot does not hold a live record.
	ErrNotLive = errors.New("heap: slot does not hold a live record")
	// ErrNoPagePicked is returned by Place before any PickPage.
	ErrNoPagePicked = errors.New("heap: PickPage must be called before Place")
	// ErrSessionDone is returned when a finished session is reused.
	ErrSessionDone = errors.New("heap: insert session already finished")
)

// Table is a heap table: a set of slotted pages plus a free-space directory.
type Table struct {
	mu       sync.Mutex
	pageSize int
	pages    map[PageID]*Page
	nextID   PageID
	dir      *FreeSpaceDir
}

// NewTable creates an empty table with the given page size and directory
// tier size, both in bytes.
func NewTable(pageSize, tierSize int) *Table {
	if pageSize < pageHeaderSize+linePointerSize {
		panic("heap: page size too small")
	}
	if tierSize <= 0 {
		panic("heap: tier size must be positive")
	}
	return &Table{
		pageSize: pageSize,
		pages:    make(map[PageID]*Page),
		dir:      newFreeSpaceDir(tierSize),
	}
}

// Directory exposes the table's free-space directory (read-only views such
// as Search and TierOf are meant for inspection and tests).
func (t *Table) Directory() *FreeSpaceDir { return t.dir }

// PageCount reports how many pages the table currently has.
func (t *Table) PageCount() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return len(t.pages)
}

// PageInfo returns the real space accounting of a page.
func (t *Table) PageInfo(id PageID) (PageInfo, error) {
	p, ok := t.page(id)
	if !ok {
		return PageInfo{}, ErrNotFound
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.info(), nil
}

func (t *Table) page(id PageID) (*Page, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	p, ok := t.pages[id]
	return p, ok
}

// addPage allocates a fresh page and publishes its free space tier.
func (t *Table) addPage() PageID {
	t.mu.Lock()
	id := t.nextID
	t.nextID++
	t.pages[id] = newPage(id, t.pageSize)
	t.mu.Unlock()
	t.dir.Update(id, t.pageSize-pageHeaderSize)
	return id
}

// Insert places rec and returns its location. It is Finish over an
// anonymous session: pick a page from the directory, place or switch, and
// repeat until the record lands.
func (t *Table) Insert(rec []byte) (Location, error) {
	s, err := t.BeginInsert(rec)
	if err != nil {
		return Location{}, err
	}
	return s.Finish()
}

// Get returns a copy of the live record at loc.
func (t *Table) Get(loc Location) ([]byte, error) {
	p, ok := t.page(loc.Page)
	if !ok {
		return nil, ErrNotFound
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	rec, ok := p.get(loc.Slot)
	if !ok {
		return nil, ErrNotLive
	}
	return rec, nil
}

// Delete removes the record at loc. Its slot becomes reusable and its bytes
// become a fragment reclaimable by a later compaction. The directory hint
// is refreshed so future inserts can find the freed space.
func (t *Table) Delete(loc Location) error {
	p, ok := t.page(loc.Page)
	if !ok {
		return ErrNotFound
	}
	p.mu.Lock()
	if !p.delete(loc.Slot) {
		p.mu.Unlock()
		return ErrNotLive
	}
	free := p.totalFree()
	p.mu.Unlock()
	t.dir.Update(loc.Page, free)
	return nil
}

// InsertSession is a pausable insert. The steps are:
//
//	PickPage — consult the directory and remember a candidate page;
//	Place    — lock the page, revalidate its real state, and place the
//	           record (compacting once if needed), or fail with
//	           ErrPageFull after correcting the directory hint;
//	Finish   — loop PickPage/Place until the record lands.
//
// A test can pause between PickPage and Place and interleave another
// transaction, then observe how Place reacts to the changed page. A session
// belongs to a single writer and is not safe for concurrent use.
type InsertSession struct {
	t      *Table
	rec    []byte
	cur    PageID
	hasCur bool
	tried  map[PageID]bool // pages Place already failed on; never retried
	done   bool
}

// BeginInsert starts a pausable insert of rec.
func (t *Table) BeginInsert(rec []byte) (*InsertSession, error) {
	if len(rec) > t.pageSize-pageHeaderSize-linePointerSize {
		return nil, ErrRecordTooLarge
	}
	return &InsertSession{t: t, rec: rec, tried: make(map[PageID]bool)}, nil
}

// PickPage selects the next candidate page: the first directory entry whose
// tier fits the record (worst case, including a new line pointer), skipping
// pages this session already failed on. If no page qualifies, a fresh page
// is allocated. Returns the picked page.
func (s *InsertSession) PickPage() PageID {
	if s.done {
		return s.cur
	}
	need := len(s.rec) + linePointerSize
	id, ok := s.t.dir.search(need, s.tried)
	if !ok {
		id = s.t.addPage()
	}
	s.cur = id
	s.hasCur = true
	return id
}

// Place attempts to place the record on the picked page. On success it
// returns the record's location. If the page's real free space is not
// enough — even after one compaction — it corrects the directory hint,
// excludes the page for this session, and returns ErrPageFull; call
// PickPage again for the next candidate.
func (s *InsertSession) Place() (Location, error) {
	if s.done {
		return Location{}, ErrSessionDone
	}
	if !s.hasCur {
		return Location{}, ErrNoPagePicked
	}
	p, _ := s.t.page(s.cur)
	p.mu.Lock()
	slot, ok := p.place(s.rec)
	free := p.totalFree()
	p.mu.Unlock()
	// Publish the page's real free space either way, so other writers do
	// not keep acting on a stale hint.
	s.t.dir.Update(s.cur, free)
	if !ok {
		s.tried[s.cur] = true
		s.hasCur = false
		return Location{}, ErrPageFull
	}
	s.done = true
	return Location{Page: s.cur, Slot: slot}, nil
}

// Finish drives the session to completion from its current state.
func (s *InsertSession) Finish() (Location, error) {
	for {
		if !s.hasCur {
			s.PickPage()
		}
		loc, err := s.Place()
		if err == nil {
			return loc, nil
		}
		if !errors.Is(err, ErrPageFull) {
			return Location{}, err
		}
	}
}
