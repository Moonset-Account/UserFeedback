package txstore

import (
	"fmt"
	"sort"
	"sync"
)

// Row is one record in the store. Key is the nullable composite unique key.
type Row struct {
	ID      string
	Key     Key
	Payload string
}

// Mode selects when unique-key conflicts are detected inside a transaction.
type Mode int

const (
	// ModeStatement checks uniqueness after every Insert, Update and Delete.
	// A statement that would violate uniqueness is rejected and leaves the
	// transaction state exactly as it was before the statement.
	ModeStatement Mode = iota
	// ModeDeferred postpones uniqueness checking until Commit, so intermediate
	// states may contain duplicate keys (for example, while swapping two keys).
	ModeDeferred
)

// String returns the mode name used in diagnostics.
func (m Mode) String() string {
	switch m {
	case ModeStatement:
		return "statement"
	case ModeDeferred:
		return "deferred"
	default:
		return fmt.Sprintf("Mode(%d)", int(m))
	}
}

// CommitResult describes a successful commit.
type CommitResult struct {
	// Version is the store version assigned to the commit. The first commit
	// after Open has version 1.
	Version uint64
	// Rows is the number of IDs inserted, updated or deleted by the commit.
	Rows int
}

// committedRow is the immutable committed form of a row. Snapshot views keep
// pointers to these values; a commit always installs fresh values instead of
// mutating existing ones, so a snapshot can never observe a later write.
type committedRow struct {
	id      string
	key     Key
	payload string
}

func (r *committedRow) toRow() Row {
	return Row{ID: r.id, Key: cloneKey(r.key), Payload: r.payload}
}

// Store holds committed rows. All exported methods are safe for concurrent
// use. Transactions read a stable snapshot and commit under the store lock.
type Store struct {
	mu      sync.Mutex
	rows    map[string]*committedRow
	version uint64
}

// Open builds a store from initial rows. IDs must be unique and non-empty.
// Keys of all initial rows must satisfy the composite unique constraint
// (keys containing a NULL column are exempt). Opening validates both and
// returns ErrDuplicateID or ErrUniqueViolation on bad input.
func Open(rows []Row) (*Store, error) {
	s := &Store{rows: make(map[string]*committedRow, len(rows))}
	keyOwner := make(map[string]string, len(rows))
	for i, r := range rows {
		if r.ID == "" {
			return nil, fmt.Errorf("txstore: row %d has an empty ID", i)
		}
		if _, dup := s.rows[r.ID]; dup {
			return nil, fmt.Errorf("%w: id %q", ErrDuplicateID, r.ID)
		}
		k := cloneKey(r.Key)
		s.rows[r.ID] = &committedRow{id: r.ID, key: k, payload: r.Payload}
		if k.HasNull() {
			continue
		}
		canonical := k.String()
		if owner, dup := keyOwner[canonical]; dup {
			return nil, fmt.Errorf("%w: key %s used by ids %q and %q", ErrUniqueViolation, canonical, owner, r.ID)
		}
		keyOwner[canonical] = r.ID
	}
	return s, nil
}

// Version returns the number of successful commits applied so far.
func (s *Store) Version() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.version
}

// Get returns the latest committed row with the given ID.
func (s *Store) Get(id string) (Row, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.rows[id]
	if !ok {
		return Row{}, false
	}
	return r.toRow(), true
}

// CommittedRows returns all committed rows sorted by ID. It is a consistent
// snapshot of the latest committed state.
func (s *Store) CommittedRows() []Row {
	s.mu.Lock()
	defer s.mu.Unlock()
	return rowsSorted(s.rows)
}

// CommittedKeys returns the sorted canonical key set of the committed state.
// Keys with NULL columns are included; they simply do not participate in
// uniqueness checks.
func (s *Store) CommittedKeys() []Key {
	s.mu.Lock()
	defer s.mu.Unlock()
	keys := make([]Key, 0, len(s.rows))
	for _, r := range s.rows {
		keys = append(keys, cloneKey(r.key))
	}
	sortKeys(keys)
	return keys
}

// Begin starts a transaction whose read view is the current committed state.
func (s *Store) Begin() *Txn {
	s.mu.Lock()
	defer s.mu.Unlock()
	snapshot := make(map[string]*committedRow, len(s.rows))
	for id, r := range s.rows {
		snapshot[id] = r
	}
	return &Txn{
		store:       s,
		baseVersion: s.version,
		snapshot:    snapshot,
		working:     make(map[string]workRow),
		mode:        ModeStatement,
	}
}

// workRow is a row as modified by the transaction. base is the committed row
// the transaction started from (nil if the ID did not exist), and is used to
// detect concurrent writes to the same ID at commit time.
type workRow struct {
	base    *committedRow
	key     Key
	payload string
	deleted bool
}

type savepoint struct {
	name    string
	working map[string]workRow
	mode    Mode
}

// Txn is a transaction. It is not safe for concurrent use by multiple
// goroutines; use one transaction per goroutine.
type Txn struct {
	store       *Store
	baseVersion uint64
	snapshot    map[string]*committedRow
	working     map[string]workRow
	mode        Mode
	savepoints  []savepoint
	closed      bool
	result      CommitResult
}

// BaseVersion is the store version the transaction's read view is based on.
func (t *Txn) BaseVersion() uint64 { return t.baseVersion }

// Mode returns the current uniqueness-checking mode.
func (t *Txn) Mode() Mode { return t.mode }

// Get returns the row as seen by this transaction, including its own writes.
func (t *Txn) Get(id string) (Row, bool) {
	if t.closed {
		return Row{}, false
	}
	if w, ok := t.working[id]; ok {
		if w.deleted {
			return Row{}, false
		}
		return Row{ID: id, Key: cloneKey(w.key), Payload: w.payload}, true
	}
	if r, ok := t.snapshot[id]; ok {
		return r.toRow(), true
	}
	return Row{}, false
}

// Rows returns the transaction's read view sorted by ID. It is empty once
// the transaction is closed.
func (t *Txn) Rows() []Row {
	if t.closed {
		return nil
	}
	view := make(map[string]*committedRow, len(t.snapshot)+len(t.working))
	for id, r := range t.snapshot {
		view[id] = r
	}
	for id, w := range t.working {
		if w.deleted {
			delete(view, id)
			continue
		}
		view[id] = &committedRow{id: id, key: w.key, payload: w.payload}
	}
	return rowsSorted(view)
}

// Keys returns the sorted canonical key set of the transaction's read view,
// which is exactly the set of keys uniqueness checking considers.
func (t *Txn) Keys() []Key {
	if t.closed {
		return nil
	}
	keys := make([]Key, 0, len(t.snapshot)+len(t.working))
	for id, r := range t.snapshot {
		w, written := t.working[id]
		if written {
			if !w.deleted {
				keys = append(keys, cloneKey(w.key))
			}
			continue
		}
		keys = append(keys, cloneKey(r.key))
	}
	for id, w := range t.working {
		if _, inSnapshot := t.snapshot[id]; inSnapshot || w.deleted {
			continue
		}
		keys = append(keys, cloneKey(w.key))
	}
	sortKeys(keys)
	return keys
}

// Insert adds a new row. It fails with ErrRowExists if the ID is visible in
// the transaction's read view. Inserting an ID deleted earlier in the same
// transaction is allowed and reuses the original base row for conflict checks.
func (t *Txn) Insert(id string, key Key, payload string) error {
	return t.statement(func(next map[string]workRow) error {
		if _, ok := viewRow(t.snapshot, t.working, id); ok {
			return fmt.Errorf("%w: id %q", ErrRowExists, id)
		}
		base := t.snapshot[id] // non-nil only when the row was deleted in this txn
		if prev, ok := t.working[id]; ok {
			base = prev.base
		}
		next[id] = workRow{base: base, key: cloneKey(key), payload: payload}
		return nil
	})
}

// Update replaces the key and payload of a visible row.
func (t *Txn) Update(id string, key Key, payload string) error {
	return t.statement(func(next map[string]workRow) error {
		if prev, ok := next[id]; ok {
			if prev.deleted {
				if prev.base == nil {
					return fmt.Errorf("%w: id %q", ErrRowNotFound, id)
				}
				prev.deleted = false
			}
			prev.key = cloneKey(key)
			prev.payload = payload
			next[id] = prev
			return nil
		}
		base, ok := t.snapshot[id]
		if !ok {
			return fmt.Errorf("%w: id %q", ErrRowNotFound, id)
		}
		next[id] = workRow{base: base, key: cloneKey(key), payload: payload}
		return nil
	})
}

// Delete removes a visible row. Deleting an already deleted or absent ID
// fails with ErrRowNotFound.
func (t *Txn) Delete(id string) error {
	return t.statement(func(next map[string]workRow) error {
		if prev, ok := next[id]; ok {
			if prev.deleted {
				return fmt.Errorf("%w: id %q", ErrRowNotFound, id)
			}
			prev.deleted = true
			prev.key = nil
			prev.payload = ""
			next[id] = prev
			return nil
		}
		base, ok := t.snapshot[id]
		if !ok {
			return fmt.Errorf("%w: id %q", ErrRowNotFound, id)
		}
		next[id] = workRow{base: base, deleted: true}
		return nil
	})
}

// statement applies one mutation. The mutation runs on a copy of the working
// set; in ModeStatement the resulting read view is checked for uniqueness.
// Any error discards the copy, so a rejected statement changes neither data
// nor mode.
func (t *Txn) statement(apply func(next map[string]workRow) error) error {
	if err := t.ensureOpen(); err != nil {
		return err
	}
	next := cloneWorking(t.working)
	if err := apply(next); err != nil {
		return err
	}
	if t.mode == ModeStatement {
		if err := validateUnique(t.snapshot, next); err != nil {
			return err
		}
	}
	t.working = next
	return nil
}

// SetMode switches the uniqueness-checking mode. Switching from deferred to
// statement first validates the current read view. If that validation fails
// with ErrUniqueViolation, the mode and all data remain unchanged.
func (t *Txn) SetMode(mode Mode) error {
	if err := t.ensureOpen(); err != nil {
		return err
	}
	switch mode {
	case ModeStatement:
		if t.mode == ModeStatement {
			return nil
		}
		if err := validateUnique(t.snapshot, t.working); err != nil {
			return err
		}
		t.mode = ModeStatement
		return nil
	case ModeDeferred:
		t.mode = ModeDeferred
		return nil
	default:
		return fmt.Errorf("%w: %d", ErrInvalidMode, int(mode))
	}
}

// Savepoint records the current working set and mode under name. Names must
// be non-empty and unique among active savepoints.
func (t *Txn) Savepoint(name string) error {
	if err := t.ensureOpen(); err != nil {
		return err
	}
	if name == "" {
		return ErrInvalidSavepointName
	}
	for _, sp := range t.savepoints {
		if sp.name == name {
			return fmt.Errorf("%w: %q", ErrSavepointExists, name)
		}
	}
	t.savepoints = append(t.savepoints, savepoint{
		name:    name,
		working: cloneWorking(t.working),
		mode:    t.mode,
	})
	return nil
}

// RollbackTo restores the working set and uniqueness mode recorded by the
// named savepoint. Savepoints created after it are invalidated. The named
// savepoint itself stays active and may be rolled back to again.
func (t *Txn) RollbackTo(name string) error {
	if err := t.ensureOpen(); err != nil {
		return err
	}
	idx := -1
	for i := range t.savepoints {
		if t.savepoints[i].name == name {
			idx = i
			break
		}
	}
	if idx < 0 {
		return fmt.Errorf("%w: %q", ErrSavepointNotFound, name)
	}
	sp := t.savepoints[idx]
	t.working = cloneWorking(sp.working)
	t.mode = sp.mode
	t.savepoints = t.savepoints[:idx+1]
	return nil
}

// Rollback discards all transaction state. The transaction is closed.
func (t *Txn) Rollback() {
	if t.closed {
		return
	}
	t.closed = true
	t.working = nil
	t.savepoints = nil
}

// Commit validates and applies the transaction. On any error nothing is
// written to the store and the transaction stays open, so the caller can
// inspect its read view or roll it back.
//
// Validation under the store lock:
//  1. Every ID the transaction wrote must still match the committed row the
//     transaction started from. A changed or concurrently created/deleted row
//     yields ErrWriteConflict, and no rows are applied.
//  2. The final key set (latest committed rows for untouched IDs plus this
//     transaction's rows) must satisfy the unique constraint. This catches
//     concurrent inserts of a key the transaction also writes. A violation
//     yields ErrUniqueViolation, and no rows are applied.
func (t *Txn) Commit() (CommitResult, error) {
	if err := t.ensureOpen(); err != nil {
		return CommitResult{}, err
	}
	// Validate under the lock, then apply all changes before releasing it so
	// the store never exposes a partially applied key set.
	t.store.mu.Lock()
	defer t.store.mu.Unlock()

	for _, id := range sortedWorkingIDs(t.working) {
		w := t.working[id]
		latest, exists := t.store.rows[id]
		switch {
		case w.base == nil && exists:
			return CommitResult{}, fmt.Errorf("%w: id %q was created concurrently", ErrWriteConflict, id)
		case w.base != nil && !exists:
			return CommitResult{}, fmt.Errorf("%w: id %q was deleted concurrently", ErrWriteConflict, id)
		case w.base != nil && exists && latest != w.base:
			return CommitResult{}, fmt.Errorf("%w: id %q changed since transaction start", ErrWriteConflict, id)
		}
	}

	final := make(map[string]Key, len(t.store.rows)+len(t.working))
	for id, r := range t.store.rows {
		if _, written := t.working[id]; !written {
			final[id] = r.key
		}
	}
	for id, w := range t.working {
		if !w.deleted {
			final[id] = w.key
		}
	}
	if err := validateUniqueKeys(final); err != nil {
		return CommitResult{}, err
	}

	for id, w := range t.working {
		if w.deleted {
			delete(t.store.rows, id)
			continue
		}
		t.store.rows[id] = &committedRow{id: id, key: cloneKey(w.key), payload: w.payload}
	}
	t.store.version++
	t.result = CommitResult{Version: t.store.version, Rows: len(t.working)}
	t.closed = true
	t.working = nil
	t.savepoints = nil
	return t.result, nil
}

// Result is the commit result; it is zero before a successful Commit.
func (t *Txn) Result() CommitResult { return t.result }

func (t *Txn) ensureOpen() error {
	if t.closed {
		return ErrTxnClosed
	}
	return nil
}

// viewRow returns the row visible in a combined snapshot/working view.
func viewRow(snapshot map[string]*committedRow, working map[string]workRow, id string) (*committedRow, bool) {
	if w, ok := working[id]; ok {
		if w.deleted {
			return nil, false
		}
		return &committedRow{id: id, key: w.key, payload: w.payload}, true
	}
	r, ok := snapshot[id]
	return r, ok
}

func cloneWorking(in map[string]workRow) map[string]workRow {
	out := make(map[string]workRow, len(in))
	for id, w := range in {
		out[id] = workRow{
			base:    w.base,
			key:     cloneKey(w.key),
			payload: w.payload,
			deleted: w.deleted,
		}
	}
	return out
}

func rowsSorted(rows map[string]*committedRow) []Row {
	out := make([]Row, 0, len(rows))
	for _, r := range rows {
		out = append(out, r.toRow())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func sortedWorkingIDs(working map[string]workRow) []string {
	ids := make([]string, 0, len(working))
	for id := range working {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// validateUnique checks the read view formed by snapshot overlaid with working.
func validateUnique(snapshot map[string]*committedRow, working map[string]workRow) error {
	view := make(map[string]Key, len(snapshot)+len(working))
	for id, r := range snapshot {
		view[id] = r.key
	}
	for id, w := range working {
		if w.deleted {
			delete(view, id)
			continue
		}
		view[id] = w.key
	}
	return validateUniqueKeys(view)
}

// validateUniqueKeys reports the lexicographically first duplicate for a
// non-NULL key, walking IDs in sorted order for determinism.
func validateUniqueKeys(keys map[string]Key) error {
	owner := make(map[string]string, len(keys))
	ids := make([]string, 0, len(keys))
	for id := range keys {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		k := keys[id]
		if k.HasNull() {
			continue
		}
		canonical := k.String()
		if first, dup := owner[canonical]; dup {
			return fmt.Errorf("%w: key %s used by ids %q and %q", ErrUniqueViolation, canonical, first, id)
		}
		owner[canonical] = id
	}
	return nil
}

func sortKeys(keys []Key) {
	sort.Slice(keys, func(i, j int) bool { return keys[i].String() < keys[j].String() })
}
