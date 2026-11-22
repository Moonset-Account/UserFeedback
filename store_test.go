package txstore

import (
	"errors"
	"reflect"
	"sync"
	"testing"
)

// keySet renders keys canonically for set comparisons in test assertions.
func keySet(keys []Key) []string {
	out := make([]string, len(keys))
	for i, k := range keys {
		out[i] = k.String()
	}
	return out
}

func mustOpen(t *testing.T, rows ...Row) *Store {
	t.Helper()
	s, err := Open(rows)
	if err != nil {
		t.Fatalf("Open: unexpected error: %v", err)
	}
	return s
}

func row(id string, cols ...Value) Row {
	return Row{ID: id, Key: Key(cols), Payload: "payload-" + id}
}

func mustGet(t *testing.T, tx *Txn, id string) Row {
	t.Helper()
	r, ok := tx.Get(id)
	if !ok {
		t.Fatalf("Txn.Get(%q): row not visible, want visible", id)
	}
	return r
}

func mustCommit(t *testing.T, tx *Txn) CommitResult {
	t.Helper()
	res, err := tx.Commit()
	if err != nil {
		t.Fatalf("Commit: unexpected error: %v", err)
	}
	return res
}

func TestKeyNullableAndAritySemantics(t *testing.T) {
	nullKey := Key{Str("tenant"), Null()}
	if !nullKey.HasNull() {
		t.Fatal("key containing NULL should report HasNull")
	}
	if (Key{Str("tenant"), Str("alice")}).HasNull() {
		t.Fatal("fully non-NULL key should not report HasNull")
	}
	if !Null().Equal(Null()) {
		t.Fatal("NULL should equal NULL for key comparison")
	}
	if Null().Equal(Str("")) {
		t.Fatal("NULL must not equal empty string")
	}
	if !(Key{Null(), Str("x")}).Equal(Key{Null(), Str("x")}) {
		t.Fatal("identical keys should compare equal")
	}
	if (Key{Str("a")}).Equal(Key{Str("a"), Str("")}) {
		t.Fatal("keys of different arity must not compare equal")
	}
	if got, want := nullKey.String(), `("tenant",NULL)`; got != want {
		t.Fatalf("Key.String() = %q, want %q", got, want)
	}
	if got, want := (Key{Str("a"), Str("b")}).String(), `("a","b")`; got != want {
		t.Fatalf("Key.String() = %q, want %q", got, want)
	}
}

func TestOpenValidatesInitialState(t *testing.T) {
	if _, err := Open([]Row{row("a", Str("k")), row("a", Str("j"))}); !errors.Is(err, ErrDuplicateID) {
		t.Fatalf("Open duplicate IDs: err = %v, want ErrDuplicateID", err)
	}
	if _, err := Open([]Row{row("a", Str("k")), row("b", Str("k"))}); !errors.Is(err, ErrUniqueViolation) {
		t.Fatalf("Open duplicate keys: err = %v, want ErrUniqueViolation", err)
	}
	if _, err := Open([]Row{row("", Str("k"))}); err == nil {
		t.Fatal("Open empty ID: want error, got nil")
	}

	s := mustOpen(t,
		row("a", Str("t"), Null()),
		row("b", Str("t"), Null()),
		row("c", Str("t"), Str("x")),
	)
	if got, want := len(s.CommittedRows()), 3; got != want {
		t.Fatalf("CommittedRows length = %d, want %d", got, want)
	}
	if got, want := keySet(s.CommittedKeys()), []string{`("t","x")`, `("t",NULL)`, `("t",NULL)`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("CommittedKeys = %v, want %v", got, want)
	}
}

func TestTransactionReadsItsOwnWrites(t *testing.T) {
	s := mustOpen(t, row("a", Str("t"), Str("a")))
	tx := s.Begin()

	if err := tx.Insert("b", Key{Str("t"), Str("b")}, "new-b"); err != nil {
		t.Fatalf("Insert: %v", err)
	}
	if got := mustGet(t, tx, "b"); got.Payload != "new-b" || !got.Key.Equal(Key{Str("t"), Str("b")}) {
		t.Fatalf("Get(b) = %+v, want inserted row visible", got)
	}
	if err := tx.Update("a", Key{Str("t"), Str("z")}, "updated-a"); err != nil {
		t.Fatalf("Update: %v", err)
	}
	if got := mustGet(t, tx, "a"); got.Payload != "updated-a" || !got.Key.Equal(Key{Str("t"), Str("z")}) {
		t.Fatalf("Get(a) = %+v, want updated row visible", got)
	}
	if err := tx.Delete("b"); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	if _, ok := tx.Get("b"); ok {
		t.Fatal("Get(b) after Delete: row still visible")
	}
	// The committed state must not change before commit.
	if _, ok := s.Get("b"); ok {
		t.Fatal("Store.Get(b) before commit: uncommitted insert leaked")
	}
	if got, want := keySet(tx.Keys()), []string{`("t","z")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Txn.Keys = %v, want %v", got, want)
	}
	if got, want := keySet(s.CommittedKeys()), []string{`("t","a")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("CommittedKeys before commit = %v, want %v", got, want)
	}

	res := mustCommit(t, tx)
	if res.Rows != 2 || res.Version != 1 {
		t.Fatalf("CommitResult = %+v, want Rows=2 Version=1", res)
	}
	if got, want := keySet(s.CommittedKeys()), []string{`("t","z")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("CommittedKeys after commit = %v, want %v", got, want)
	}
	if _, err := tx.Commit(); !errors.Is(err, ErrTxnClosed) {
		t.Fatalf("second Commit: err = %v, want ErrTxnClosed", err)
	}
}

func TestStatementModeRejectsDuplicateAtomically(t *testing.T) {
	s := mustOpen(t,
		row("a", Str("t"), Str("a")),
		row("b", Str("t"), Str("b")),
	)
	tx := s.Begin()
	if tx.Mode() != ModeStatement {
		t.Fatalf("default Mode = %v, want statement", tx.Mode())
	}

	err := tx.Update("a", Key{Str("t"), Str("b")}, "clash")
	if !errors.Is(err, ErrUniqueViolation) {
		t.Fatalf("Update to duplicate key: err = %v, want ErrUniqueViolation", err)
	}
	if got := mustGet(t, tx, "a"); !got.Key.Equal(Key{Str("t"), Str("a")}) || got.Payload == "clash" {
		t.Fatalf("Get(a) after rejected statement = %+v, want original row", got)
	}
	if got, want := keySet(tx.Keys()), []string{`("t","a")`, `("t","b")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Txn.Keys after rejected statement = %v, want %v", got, want)
	}

	if err := tx.Insert("c", Key{Str("t"), Str("a")}, "dup-insert"); !errors.Is(err, ErrUniqueViolation) {
		t.Fatalf("Insert duplicate key: err = %v, want ErrUniqueViolation", err)
	}
	if _, ok := tx.Get("c"); ok {
		t.Fatal("Get(c): rejected insert leaked into working set")
	}

	// NULL columns exempt the row from the unique check.
	if err := tx.Insert("n1", Key{Str("t"), Null()}, "n1"); err != nil {
		t.Fatalf("Insert NULL key n1: %v", err)
	}
	if err := tx.Insert("n2", Key{Str("t"), Null()}, "n2"); err != nil {
		t.Fatalf("Insert second NULL key n2: %v", err)
	}
	mustCommit(t, tx)
}

func TestDeferredModeSwapOfTwoKeys(t *testing.T) {
	s := mustOpen(t,
		row("a", Str("t"), Str("a")),
		row("b", Str("t"), Str("b")),
	)
	tx := s.Begin()
	if err := tx.SetMode(ModeDeferred); err != nil {
		t.Fatalf("SetMode(deferred): %v", err)
	}
	// Intermediate state has duplicate keys; statement mode would reject this.
	if err := tx.Update("a", Key{Str("t"), Str("b")}, "a-swapped"); err != nil {
		t.Fatalf("deferred Update a: %v", err)
	}
	if got, want := keySet(tx.Keys()), []string{`("t","b")`, `("t","b")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Txn.Keys mid-swap = %v, want duplicate keys %v", got, want)
	}
	if err := tx.Update("b", Key{Str("t"), Str("a")}, "b-swapped"); err != nil {
		t.Fatalf("deferred Update b: %v", err)
	}
	if got, want := keySet(tx.Keys()), []string{`("t","a")`, `("t","b")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Txn.Keys after swap = %v, want %v", got, want)
	}
	mustCommit(t, tx)

	if got, want := keySet(s.CommittedKeys()), []string{`("t","a")`, `("t","b")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("CommittedKeys after swap commit = %v, want %v", got, want)
	}
	a, _ := s.Get("a")
	b, _ := s.Get("b")
	if !a.Key.Equal(Key{Str("t"), Str("b")}) || !b.Key.Equal(Key{Str("t"), Str("a")}) {
		t.Fatalf("committed rows = a:%+v b:%+v, want keys swapped", a, b)
	}

	// A deferred duplicate that is never resolved fails at commit and leaves
	// the committed key set untouched.
	tx2 := s.Begin()
	if err := tx2.SetMode(ModeDeferred); err != nil {
		t.Fatalf("SetMode(deferred): %v", err)
	}
	if err := tx2.Update("a", Key{Str("t"), Str("a")}, "still-duplicate"); err != nil {
		t.Fatalf("deferred Update: %v", err)
	}
	if _, err := tx2.Commit(); !errors.Is(err, ErrUniqueViolation) {
		t.Fatalf("Commit duplicate: err = %v, want ErrUniqueViolation", err)
	}
	if got, want := keySet(s.CommittedKeys()), []string{`("t","a")`, `("t","b")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("CommittedKeys after failed commit = %v, want unchanged %v", got, want)
	}
	if _, ok := s.Get("a"); !ok {
		t.Fatal("Store.Get(a) after failed commit: row disappeared")
	}
	tx2.Rollback()
}

func TestSetModeToStatementValidatesAndPreservesStateOnFailure(t *testing.T) {
	s := mustOpen(t,
		row("a", Str("t"), Str("a")),
		row("b", Str("t"), Str("b")),
	)
	tx := s.Begin()
	if err := tx.SetMode(ModeDeferred); err != nil {
		t.Fatalf("SetMode(deferred): %v", err)
	}
	if err := tx.Update("a", Key{Str("t"), Str("b")}, "clash"); err != nil {
		t.Fatalf("deferred Update: %v", err)
	}

	err := tx.SetMode(ModeStatement)
	if !errors.Is(err, ErrUniqueViolation) {
		t.Fatalf("SetMode(statement) with duplicate view: err = %v, want ErrUniqueViolation", err)
	}
	if tx.Mode() != ModeDeferred {
		t.Fatalf("Mode after failed switch = %v, want deferred preserved", tx.Mode())
	}
	if got, want := keySet(tx.Keys()), []string{`("t","b")`, `("t","b")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Txn.Keys after failed switch = %v, want preserved %v", got, want)
	}
	if got := mustGet(t, tx, "a"); got.Payload != "clash" || !got.Key.Equal(Key{Str("t"), Str("b")}) {
		t.Fatalf("Get(a) after failed switch = %+v, want preserved row", got)
	}

	// Resolve the duplicate; the switch then succeeds and later statements
	// are checked immediately again.
	if err := tx.Update("b", Key{Str("t"), Str("a")}, "b"); err != nil {
		t.Fatalf("deferred Update b: %v", err)
	}
	if err := tx.SetMode(ModeStatement); err != nil {
		t.Fatalf("SetMode(statement) after resolving: %v", err)
	}
	if err := tx.Update("a", Key{Str("t"), Str("a")}, "clash-again"); !errors.Is(err, ErrUniqueViolation) {
		t.Fatalf("statement-mode Update: err = %v, want ErrUniqueViolation", err)
	}
	if err := tx.SetMode(Mode(99)); !errors.Is(err, ErrInvalidMode) {
		t.Fatalf("SetMode(99): err = %v, want ErrInvalidMode", err)
	}
	mustCommit(t, tx)
}

func TestSavepointRestoresKeysAndModeAndInvalidatesLaterOnes(t *testing.T) {
	s := mustOpen(t,
		row("a", Str("t"), Str("a")),
		row("b", Str("t"), Str("b")),
	)
	tx := s.Begin()
	if err := tx.Savepoint("s1"); err != nil {
		t.Fatalf("Savepoint s1: %v", err)
	}
	if err := tx.SetMode(ModeDeferred); err != nil {
		t.Fatalf("SetMode(deferred): %v", err)
	}
	if err := tx.Update("a", Key{Str("t"), Str("b")}, "a-to-b"); err != nil {
		t.Fatalf("Update a: %v", err)
	}
	if err := tx.Update("b", Key{Str("t"), Str("a")}, "b-to-a"); err != nil {
		t.Fatalf("Update b: %v", err)
	}
	if err := tx.Insert("c", Key{Str("t"), Str("c")}, "c"); err != nil {
		t.Fatalf("Insert c: %v", err)
	}
	if err := tx.Savepoint("s2"); err != nil {
		t.Fatalf("Savepoint s2: %v", err)
	}
	if err := tx.Delete("a"); err != nil {
		t.Fatalf("Delete a: %v", err)
	}

	if err := tx.RollbackTo("s1"); err != nil {
		t.Fatalf("RollbackTo s1: %v", err)
	}
	if tx.Mode() != ModeStatement {
		t.Fatalf("Mode after rollback = %v, want statement from s1", tx.Mode())
	}
	if got, want := keySet(tx.Keys()), []string{`("t","a")`, `("t","b")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("Txn.Keys after rollback = %v, want s1 keys %v", got, want)
	}
	if _, ok := tx.Get("c"); ok {
		t.Fatal("Get(c) after rollback: insert from after s1 survived")
	}
	if err := tx.RollbackTo("s2"); !errors.Is(err, ErrSavepointNotFound) {
		t.Fatalf("RollbackTo s2 after rollback to s1: err = %v, want ErrSavepointNotFound", err)
	}
	if err := tx.RollbackTo("s1"); err != nil {
		t.Fatalf("RollbackTo s1 twice: %v", err)
	}
	if err := tx.Savepoint(""); !errors.Is(err, ErrInvalidSavepointName) {
		t.Fatalf("Savepoint empty: err = %v, want ErrInvalidSavepointName", err)
	}
	if err := tx.Savepoint("s1"); !errors.Is(err, ErrSavepointExists) {
		t.Fatalf("Savepoint duplicate: err = %v, want ErrSavepointExists", err)
	}
	mustCommit(t, tx)
}

func TestCommitChecksUntouchedRowsAndSameRowConflicts(t *testing.T) {
	// Concurrent update of a key that the committing transaction also claims
	// must be detected in the final committed-state check.
	s := mustOpen(t,
		row("a", Str("t"), Str("a")),
		row("b", Str("t"), Str("b")),
		row("c", Str("t"), Str("c")),
	)
	tx := s.Begin()
	other := s.Begin()
	if err := other.Update("b", Key{Str("t"), Str("x")}, "moved-b"); err != nil {
		t.Fatalf("other Update b: %v", err)
	}
	mustCommit(t, other)

	if err := tx.Update("a", Key{Str("t"), Str("x")}, "claims-x"); err != nil {
		t.Fatalf("tx Update a: %v", err)
	}
	if _, err := tx.Commit(); !errors.Is(err, ErrUniqueViolation) {
		t.Fatalf("Commit claiming concurrently inserted key: err = %v, want ErrUniqueViolation", err)
	}
	if got, want := keySet(s.CommittedKeys()), []string{`("t","a")`, `("t","c")`, `("t","x")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("CommittedKeys after failed commit = %v, want no partial keys %v", got, want)
	}
	if got := mustGet(t, tx, "a"); !got.Key.Equal(Key{Str("t"), Str("x")}) {
		t.Fatalf("Txn.Get(a) after failed commit = %+v, want own write preserved", got)
	}
	tx.Rollback()

	// Same-row concurrent rewrite: second committer must get ErrWriteConflict
	// and must not apply any of its other changes either.
	s2 := mustOpen(t,
		row("a", Str("t"), Str("a")),
		row("c", Str("t"), Str("c")),
	)
	tx1 := s2.Begin()
	tx2 := s2.Begin()
	if err := tx1.Update("a", Key{Str("t"), Str("a1")}, "tx1"); err != nil {
		t.Fatalf("tx1 Update a: %v", err)
	}
	if err := tx1.Update("c", Key{Str("t"), Str("c1")}, "tx1"); err != nil {
		t.Fatalf("tx1 Update c: %v", err)
	}
	mustCommit(t, tx1)

	if err := tx2.Update("a", Key{Str("t"), Str("a2")}, "tx2"); err != nil {
		t.Fatalf("tx2 Update a: %v", err)
	}
	if err := tx2.Update("c", Key{Str("t"), Str("c2")}, "tx2"); err != nil {
		t.Fatalf("tx2 Update c: %v", err)
	}
	if _, err := tx2.Commit(); !errors.Is(err, ErrWriteConflict) {
		t.Fatalf("tx2 Commit: err = %v, want ErrWriteConflict", err)
	}
	if got, want := keySet(s2.CommittedKeys()), []string{`("t","a1")`, `("t","c1")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("CommittedKeys after conflict = %v, want tx1 state only %v", got, want)
	}
	if got, ok := s2.Get("c"); !ok || got.Key.String() != `("t","c1")` || got.Payload != "tx1" {
		t.Fatalf("Store.Get(c) = %+v, want tx1's committed row", got)
	}
	if _, err := tx2.Commit(); !errors.Is(err, ErrWriteConflict) {
		t.Fatalf("tx2 retry Commit: err = %v, want ErrWriteConflict (txn still open)", err)
	}
	tx2.Rollback()
	if _, err := tx2.Commit(); !errors.Is(err, ErrTxnClosed) {
		t.Fatalf("Commit after Rollback: err = %v, want ErrTxnClosed", err)
	}
}

// TestExchangeThenConcurrentSameKeyInsert demonstrates the required
// interleaving: two transactions each perform a complete two-row key swap in
// deferred mode and each inserts the same brand-new key. The transaction that
// commits second passes its own row-conflict check (it touched different
// rows) but is rejected by the final committed-state uniqueness check, so the
// store keeps exactly one row for the new key and no partial keys.
func TestExchangeThenConcurrentSameKeyInsert(t *testing.T) {
	s := mustOpen(t,
		row("a1", Str("t"), Str("a")),
		row("a2", Str("t"), Str("b")),
		row("b1", Str("t"), Str("c")),
		row("b2", Str("t"), Str("d")),
	)
	tx1 := s.Begin()
	tx2 := s.Begin()

	for _, tx := range []*Txn{tx1, tx2} {
		if err := tx.SetMode(ModeDeferred); err != nil {
			t.Fatalf("SetMode(deferred): %v", err)
		}
	}
	// tx1 swaps (a,b) and tx2 swaps (c,d); both insert the same new key.
	if err := tx1.Update("a1", Key{Str("t"), Str("b")}, "a1"); err != nil {
		t.Fatalf("tx1 Update a1: %v", err)
	}
	if err := tx1.Update("a2", Key{Str("t"), Str("a")}, "a2"); err != nil {
		t.Fatalf("tx1 Update a2: %v", err)
	}
	if err := tx1.Insert("new1", Key{Str("t"), Str("new")}, "tx1-new"); err != nil {
		t.Fatalf("tx1 Insert new1: %v", err)
	}
	if err := tx2.Update("b1", Key{Str("t"), Str("d")}, "b1"); err != nil {
		t.Fatalf("tx2 Update b1: %v", err)
	}
	if err := tx2.Update("b2", Key{Str("t"), Str("c")}, "b2"); err != nil {
		t.Fatalf("tx2 Update b2: %v", err)
	}
	if err := tx2.Insert("new2", Key{Str("t"), Str("new")}, "tx2-new"); err != nil {
		t.Fatalf("tx2 Insert new2: %v", err)
	}

	// Read views before either commit: each sees the full snapshot (including
	// the other's rows unchanged), its own swap, and its own copy of the new
	// key; neither sees the other's uncommitted work.
	if got, want := keySet(tx1.Keys()), []string{`("t","a")`, `("t","b")`, `("t","c")`, `("t","d")`, `("t","new")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("tx1.Keys = %v, want %v", got, want)
	}
	if got, want := keySet(tx2.Keys()), []string{`("t","a")`, `("t","b")`, `("t","c")`, `("t","d")`, `("t","new")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("tx2.Keys = %v, want %v", got, want)
	}
	if _, ok := tx1.Get("b1"); !ok {
		t.Fatal("tx1 sees b1; snapshot should include it unchanged")
	}
	if got := mustGet(t, tx1, "b1"); !got.Key.Equal(Key{Str("t"), Str("c")}) {
		t.Fatalf("tx1 Get(b1) = %+v, want snapshot key, not tx2's uncommitted swap", got)
	}

	res1 := mustCommit(t, tx1)
	if res1.Version != 1 || res1.Rows != 3 {
		t.Fatalf("tx1 CommitResult = %+v, want Version=1 Rows=3", res1)
	}

	_, err := tx2.Commit()
	if !errors.Is(err, ErrUniqueViolation) {
		t.Fatalf("tx2 Commit: err = %v, want ErrUniqueViolation for the shared new key", err)
	}
	// tx2 remains open with its read view intact for inspection.
	if got, want := keySet(tx2.Keys()), []string{`("t","a")`, `("t","b")`, `("t","c")`, `("t","d")`, `("t","new")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("tx2.Keys after failed commit = %v, want preserved view %v", got, want)
	}
	if _, ok := s.Get("new2"); ok {
		t.Fatal("Store.Get(new2): failed transaction leaked its insert")
	}
	// Final committed key set: tx1's swap, tx2's rows untouched, one new key.
	if got, want := keySet(s.CommittedKeys()), []string{
		`("t","a")`, `("t","b")`, `("t","c")`, `("t","d")`, `("t","new")`,
	}; !reflect.DeepEqual(got, want) {
		t.Fatalf("final CommittedKeys = %v, want %v", got, want)
	}
	// No partial keys: tx2's swap must not appear.
	if _, ok := s.Get("b1"); !ok {
		t.Fatal("Store.Get(b1) missing after tx2 failure")
	}
	b1, _ := s.Get("b1")
	b2, _ := s.Get("b2")
	if !b1.Key.Equal(Key{Str("t"), Str("c")}) || !b2.Key.Equal(Key{Str("t"), Str("d")}) {
		t.Fatalf("b1=%s b2=%s, want tx2 swap fully absent", b1.Key, b2.Key)
	}
	// The exchange itself is sound: drop only the conflicting insert and the
	// swap commits, proving each transaction's swap is independently viable.
	if err := tx2.Delete("new2"); err != nil {
		t.Fatalf("tx2 Delete new2: %v", err)
	}
	res2 := mustCommit(t, tx2)
	if res2.Version != 2 || res2.Rows != 3 {
		t.Fatalf("tx2 second CommitResult = %+v, want Version=2 Rows=3", res2)
	}
	if got, want := keySet(s.CommittedKeys()), []string{`("t","a")`, `("t","b")`, `("t","c")`, `("t","d")`, `("t","new")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("final CommittedKeys after both swaps = %v, want %v", got, want)
	}
	c1, _ := s.Get("b1")
	c2, _ := s.Get("b2")
	if !c1.Key.Equal(Key{Str("t"), Str("d")}) || !c2.Key.Equal(Key{Str("t"), Str("c")}) {
		t.Fatalf("final b1=%s b2=%s, want tx2 swap applied", c1.Key, c2.Key)
	}
}

func TestConcurrentSameRowWritersOneWins(t *testing.T) {
	for iter := 0; iter < 25; iter++ {
		s := mustOpen(t, row("shared", Str("t"), Str("v0")))
		tx1 := s.Begin()
		tx2 := s.Begin()
		start := make(chan struct{})
		var wg sync.WaitGroup
		errs := make([]error, 2)
		payloads := []string{"one", "two"}
		for i, tx := range []*Txn{tx1, tx2} {
			wg.Add(1)
			go func(i int, tx *Txn) {
				defer wg.Done()
				<-start
				if err := tx.Update("shared", Key{Str("t"), Str("v1")}, payloads[i]); err != nil {
					errs[i] = err
					return
				}
				_, errs[i] = tx.Commit()
			}(i, tx)
		}
		close(start)
		wg.Wait()

		success := 0
		conflicts := 0
		for i, err := range errs {
			switch {
			case err == nil:
				success++
			case errors.Is(err, ErrWriteConflict):
				conflicts++
			default:
				t.Fatalf("iteration %d tx%d: unexpected error %v", iter, i, err)
			}
		}
		if success != 1 || conflicts != 1 {
			t.Fatalf("iteration %d: success=%d conflicts=%d, want 1 and 1", iter, success, conflicts)
		}
		if got, want := keySet(s.CommittedKeys()), []string{`("t","v1")`}; !reflect.DeepEqual(got, want) {
			t.Fatalf("iteration %d: CommittedKeys = %v, want %v", iter, got, want)
		}
		row, ok := s.Get("shared")
		if !ok || (row.Payload != "one" && row.Payload != "two") {
			t.Fatalf("iteration %d: committed row = %+v, ok=%v; want one full winner payload", iter, row, ok)
		}
		if s.Version() != 1 {
			t.Fatalf("iteration %d: Version = %d, want 1", iter, s.Version())
		}
	}
}

func TestStatementAndSavepointBoundaries(t *testing.T) {
	s := mustOpen(t, row("a", Str("t"), Str("a")))
	tx := s.Begin()

	if err := tx.Insert("a", Key{Str("t"), Str("dup")}, "x"); !errors.Is(err, ErrRowExists) {
		t.Fatalf("Insert existing ID: err = %v, want ErrRowExists", err)
	}
	if err := tx.Update("missing", Key{Str("t"), Str("m")}, "x"); !errors.Is(err, ErrRowNotFound) {
		t.Fatalf("Update missing: err = %v, want ErrRowNotFound", err)
	}
	if err := tx.Delete("missing"); !errors.Is(err, ErrRowNotFound) {
		t.Fatalf("Delete missing: err = %v, want ErrRowNotFound", err)
	}
	if err := tx.Delete("a"); err != nil {
		t.Fatalf("Delete a: %v", err)
	}
	if err := tx.Delete("a"); !errors.Is(err, ErrRowNotFound) {
		t.Fatalf("Delete twice: err = %v, want ErrRowNotFound", err)
	}
	// Re-inserting an ID deleted in this transaction is allowed and keeps the
	// original base row for conflict detection.
	if err := tx.Insert("a", Key{Str("t"), Str("a2")}, "a2"); err != nil {
		t.Fatalf("Insert after delete: %v", err)
	}
	if err := tx.RollbackTo("nope"); !errors.Is(err, ErrSavepointNotFound) {
		t.Fatalf("RollbackTo unknown: err = %v, want ErrSavepointNotFound", err)
	}
	mustCommit(t, tx)
	if got := mustGet(t, s.Begin(), "a"); got.Payload != "a2" {
		t.Fatalf("committed a payload = %q, want a2", got.Payload)
	}

	closed := s.Begin()
	closed.Rollback()
	if err := closed.Savepoint("s"); !errors.Is(err, ErrTxnClosed) {
		t.Fatalf("Savepoint on closed txn: err = %v, want ErrTxnClosed", err)
	}
	if err := closed.SetMode(ModeDeferred); !errors.Is(err, ErrTxnClosed) {
		t.Fatalf("SetMode on closed txn: err = %v, want ErrTxnClosed", err)
	}
	if err := closed.Insert("z", Key{Str("t"), Str("z")}, ""); !errors.Is(err, ErrTxnClosed) {
		t.Fatalf("Insert on closed txn: err = %v, want ErrTxnClosed", err)
	}
}

func TestCallerSlicesCannotMutateStoreState(t *testing.T) {
	shared := Key{Str("t"), Str("a")}
	s := mustOpen(t, Row{ID: "a", Key: shared, Payload: "p"})
	// Mutating the slice handed to Open must not change the stored key.
	shared[1] = Str("hacked")
	if got, _ := s.Get("a"); !got.Key.Equal(Key{Str("t"), Str("a")}) {
		t.Fatalf("Store.Get(a) = %s, want original key preserved", got.Key)
	}

	tx := s.Begin()
	inserted := Key{Str("t"), Str("b")}
	if err := tx.Insert("b", inserted, "p"); err != nil {
		t.Fatalf("Insert: %v", err)
	}
	inserted[1] = Str("hacked")
	if got := mustGet(t, tx, "b"); !got.Key.Equal(Key{Str("t"), Str("b")}) {
		t.Fatalf("Txn.Get(b) = %s, want inserted key preserved", got.Key)
	}
	// Mutating a key returned by Get must not corrupt the transaction either.
	got := mustGet(t, tx, "b")
	got.Key[1] = Str("hacked")
	if again := mustGet(t, tx, "b"); !again.Key.Equal(Key{Str("t"), Str("b")}) {
		t.Fatalf("Txn.Get(b) after mutating returned key = %s, want unchanged", again.Key)
	}
}

func TestRollbackDiscardsEverythingAndKeepsCommittedStable(t *testing.T) {
	s := mustOpen(t, row("a", Str("t"), Str("a")))
	before := keySet(s.CommittedKeys())

	tx := s.Begin()
	if err := tx.Insert("b", Key{Str("t"), Str("b")}, "b"); err != nil {
		t.Fatalf("Insert: %v", err)
	}
	if err := tx.Delete("a"); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	if err := tx.Savepoint("sp"); err != nil {
		t.Fatalf("Savepoint: %v", err)
	}
	tx.Rollback()

	if got := keySet(s.CommittedKeys()); !reflect.DeepEqual(got, before) {
		t.Fatalf("CommittedKeys after Rollback = %v, want %v", got, before)
	}
	if s.Version() != 0 {
		t.Fatalf("Version after Rollback = %d, want 0", s.Version())
	}
	if tx.Rows() != nil {
		t.Fatal("Txn.Rows() after Rollback should be empty")
	}
	if err := tx.RollbackTo("sp"); !errors.Is(err, ErrTxnClosed) {
		t.Fatalf("RollbackTo after Rollback: err = %v, want ErrTxnClosed", err)
	}
	tx.Rollback() // idempotent, must not panic
}

// TestDeleteThenReuseKeyInterleaving covers a boundary where one transaction
// frees a key and another concurrently claims it. The claim is judged against
// the latest committed state: rejected while the old row still holds the key,
// accepted once that delete has committed.
func TestDeleteThenReuseKeyInterleaving(t *testing.T) {
	s := mustOpen(t,
		row("a", Str("t"), Str("a")),
		row("b", Str("t"), Str("b")),
	)
	reuser := s.Begin()
	deleter := s.Begin()

	if err := deleter.Delete("a"); err != nil {
		t.Fatalf("deleter Delete a: %v", err)
	}
	if err := reuser.SetMode(ModeDeferred); err != nil {
		t.Fatalf("SetMode(deferred): %v", err)
	}
	if err := reuser.Update("b", Key{Str("t"), Str("a")}, "claims-a"); err != nil {
		t.Fatalf("reuser Update b: %v", err)
	}
	// reuser wrote only b. Row a is untouched and still holds ("t","a") in the
	// latest committed state, so the final-state check rejects the claim even
	// though reuser's own row check passes.
	if _, err := reuser.Commit(); !errors.Is(err, ErrUniqueViolation) {
		t.Fatalf("reuser Commit before delete: err = %v, want ErrUniqueViolation on untouched row a", err)
	}
	if got, want := keySet(s.CommittedKeys()), []string{`("t","a")`, `("t","b")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("CommittedKeys after failed reuse = %v, want %v", got, want)
	}
	if b, _ := s.Get("b"); !b.Key.Equal(Key{Str("t"), Str("b")}) {
		t.Fatalf("Store.Get(b) = %s, want original key, no partial write", b.Key)
	}
	if got, want := keySet(reuser.Keys()), []string{`("t","a")`, `("t","a")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("reuser.Keys after failed commit = %v, want preserved view %v", got, want)
	}

	// The deleter commits, freeing the key. reuser touched a different row and
	// its row check still passes, so its retry now succeeds and the claim lands.
	mustCommit(t, deleter)
	res := mustCommit(t, reuser)
	if res.Version != 2 || res.Rows != 1 {
		t.Fatalf("reuser CommitResult = %+v, want Version=2 Rows=1", res)
	}
	if got, want := keySet(s.CommittedKeys()), []string{`("t","a")`}; !reflect.DeepEqual(got, want) {
		t.Fatalf("CommittedKeys after reuse = %v, want %v", got, want)
	}
	if b, _ := s.Get("b"); !b.Key.Equal(Key{Str("t"), Str("a")}) || b.Payload != "claims-a" {
		t.Fatalf("Store.Get(b) = %+v, want claimed key", b)
	}
}

// TestSnapshotIsolationFromLaterCommits proves a long-lived read view does not
// observe commits made after Begin.
func TestSnapshotIsolationFromLaterCommits(t *testing.T) {
	s := mustOpen(t, row("a", Str("t"), Str("a")))
	reader := s.Begin()

	writer := s.Begin()
	if err := writer.Update("a", Key{Str("t"), Str("z")}, "changed"); err != nil {
		t.Fatalf("writer Update: %v", err)
	}
	if err := writer.Insert("b", Key{Str("t"), Str("b")}, "new"); err != nil {
		t.Fatalf("writer Insert: %v", err)
	}
	mustCommit(t, writer)

	if got := mustGet(t, reader, "a"); !got.Key.Equal(Key{Str("t"), Str("a")}) || got.Payload != "payload-a" {
		t.Fatalf("reader.Get(a) = %+v, want pre-commit snapshot", got)
	}
	if _, ok := reader.Get("b"); ok {
		t.Fatal("reader.Get(b): snapshot must not see later insert")
	}
	if reader.BaseVersion() != 0 {
		t.Fatalf("reader.BaseVersion = %d, want 0", reader.BaseVersion())
	}
	if s.Version() != 1 {
		t.Fatalf("Store.Version = %d, want 1", s.Version())
	}
	reader.Rollback()
}
