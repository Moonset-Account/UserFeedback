package relationindex

import (
	"fmt"
	"sync"
)

// Store holds the current relation rows. It is the mutable side of the
// design: sessions take an immutable Snapshot when they begin, and later
// Upserts or Deletes only become visible to sessions that begin afterwards.
type Store struct {
	mu   sync.RWMutex
	rows map[string]Row
}

// NewStore returns an empty Store.
func NewStore() *Store {
	return &Store{rows: make(map[string]Row)}
}

// LoadStore builds a Store from rows, rejecting empty IDs, duplicate IDs and
// non-finite scores.
func LoadStore(rows []Row) (*Store, error) {
	s := NewStore()
	for _, r := range rows {
		if err := validateRow(r); err != nil {
			return nil, err
		}
		if _, ok := s.rows[r.ID]; ok {
			return nil, fmt.Errorf("%w: %q", ErrDuplicateID, r.ID)
		}
		s.rows[r.ID] = cloneRow(r)
	}
	return s, nil
}

// Upsert inserts or replaces the row with the given primary key. It returns
// ErrInvalidRow for an empty key or non-finite score.
func (s *Store) Upsert(r Row) error {
	if err := validateRow(r); err != nil {
		return err
	}
	s.mu.Lock()
	s.rows[r.ID] = cloneRow(r)
	s.mu.Unlock()
	return nil
}

// Delete removes a row. It reports whether the row existed.
func (s *Store) Delete(id string) bool {
	s.mu.Lock()
	_, ok := s.rows[id]
	delete(s.rows, id)
	s.mu.Unlock()
	return ok
}

// Snapshot freezes the current rows into an immutable ordered index. The
// returned Snapshot never observes later writes to the Store.
func (s *Store) Snapshot() (*Snapshot, error) {
	s.mu.RLock()
	rows := make([]Row, 0, len(s.rows))
	for _, r := range s.rows {
		rows = append(rows, r)
	}
	s.mu.RUnlock()
	return NewSnapshot(rows)
}

// Session is a read view over one immutable Snapshot. All cursors it returns
// are scoped to that snapshot; writes to the Store after the session began do
// not change what the session sees.
type Session struct {
	snapshot *Snapshot
}

// BeginSession captures the current Store contents into a new session.
// Records added or rescored after this point are only visible to sessions
// begun later.
func (s *Store) BeginSession() (*Session, error) {
	snap, err := s.Snapshot()
	if err != nil {
		return nil, err
	}
	return &Session{snapshot: snap}, nil
}

// Snapshot returns the session's immutable index.
func (s *Session) Snapshot() *Snapshot {
	return s.snapshot
}

// FirstPage returns the first page of the session.
func (s *Session) FirstPage(pageSize int) (Page, error) {
	return s.snapshot.FirstPage(pageSize)
}

// LastPage returns the last page of the session.
func (s *Session) LastPage(pageSize int) (Page, error) {
	return s.snapshot.LastPage(pageSize)
}

// FetchAfter continues forward from a cursor produced by this session.
func (s *Session) FetchAfter(cursor string, pageSize int) (Page, error) {
	return s.snapshot.FetchAfter(cursor, pageSize)
}

// FetchBefore continues backward from a cursor produced by this session,
// returning rows in display order.
func (s *Session) FetchBefore(cursor string, pageSize int) (Page, error) {
	return s.snapshot.FetchBefore(cursor, pageSize)
}
