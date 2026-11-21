package relationindex

import (
	"errors"
	"math"
	"sync"
	"testing"
)

func TestStoreUpsertDeleteAndSnapshot(t *testing.T) {
	store, err := LoadStore([]Row{
		{ID: "a", Region: "eu", Score: NewScore(1)},
		{ID: "b", Region: "eu", Score: NewScore(2)},
	})
	if err != nil {
		t.Fatalf("LoadStore: %v", err)
	}
	if err := store.Upsert(Row{ID: "a", Region: "eu", Score: NewScore(9)}); err != nil {
		t.Fatalf("Upsert: %v", err)
	}
	if !store.Delete("b") {
		t.Fatal("Delete(b) reported missing")
	}
	if store.Delete("b") {
		t.Fatal("second Delete(b) reported present")
	}
	if err := store.Upsert(Row{ID: "c", Region: "eu", Score: NewScore(0)}); err != nil {
		t.Fatalf("Upsert: %v", err)
	}

	session, err := store.BeginSession()
	if err != nil {
		t.Fatalf("BeginSession: %v", err)
	}
	if got := ids(mustPage(t)(session.FirstPage(10)).Rows); !equalStrings(got, []string{"a", "c"}) {
		t.Fatalf("snapshot rows = %v", got)
	}
}

func TestLoadStoreRejectsDuplicatesAndInvalidRows(t *testing.T) {
	if _, err := LoadStore([]Row{
		{ID: "x", Region: "r", Score: NewScore(1)},
		{ID: "x", Region: "r", Score: NewScore(2)},
	}); !errors.Is(err, ErrDuplicateID) {
		t.Fatalf("err = %v, want ErrDuplicateID", err)
	}
	if _, err := LoadStore([]Row{{ID: "", Region: "r"}}); !errors.Is(err, ErrInvalidRow) {
		t.Fatalf("err = %v, want ErrInvalidRow", err)
	}
}

func TestStoreRejectsInvalidUpsert(t *testing.T) {
	store := NewStore()
	if err := store.Upsert(Row{ID: "", Region: "r"}); !errors.Is(err, ErrInvalidRow) {
		t.Fatalf("empty id: err = %v", err)
	}
	if err := store.Upsert(Row{ID: "a", Region: "r", Score: NewScore(math.NaN())}); !errors.Is(err, ErrInvalidRow) {
		t.Fatalf("NaN: err = %v", err)
	}
}

// TestConcurrentUpsertsWithSessions exercises the store's locking: sessions
// begun concurrently with writes must each see a consistent, sorted snapshot.
func TestConcurrentUpsertsWithSessions(t *testing.T) {
	store := NewStore()
	for i := 0; i < 50; i++ {
		if err := store.Upsert(Row{
			ID:     zeroPad(i),
			Region: "r",
			Score:  NewScore(float64(i)),
		}); err != nil {
			t.Fatalf("Upsert: %v", err)
		}
	}

	var wg sync.WaitGroup
	errCh := make(chan error, 16)
	for w := 0; w < 8; w++ {
		wg.Add(1)
		go func(base int) {
			defer wg.Done()
			for j := 0; j < 25; j++ {
				id := "w" + zeroPad(base) + "-" + zeroPad(j)
				if err := store.Upsert(Row{ID: id, Region: "r", Score: NewScore(float64(j))}); err != nil {
					errCh <- err
					return
				}
				session, err := store.BeginSession()
				if err != nil {
					errCh <- err
					return
				}
				page, err := session.FirstPage(5)
				if err != nil {
					errCh <- err
					return
				}
				for i := 1; i < len(page.Rows); i++ {
					if compareKeys(keyOfRow(page.Rows[i-1]), keyOfRow(page.Rows[i])) > 0 {
						errCh <- ErrInvalidRow
						return
					}
				}
			}
		}(w)
	}
	wg.Wait()
	close(errCh)
	for err := range errCh {
		t.Fatalf("concurrent session saw inconsistent order: %v", err)
	}
}

func TestCursorRoundTripPreservesExactKey(t *testing.T) {
	keys := []indexKey{
		{region: "", hasScore: false, id: "plain"},
		{region: "地区", hasScore: true, score: -0.5, id: "ключ"},
		{region: "r", hasScore: true, score: 1.5, id: "k"},
		{region: "r", hasScore: false, id: "u"},
		{region: "a\x00b", hasScore: true, score: 0, id: "z|w"},
	}
	for _, key := range keys {
		encoded := encodeCursor(42, key)
		decoded, snapshotID, err := decodeCursor(encoded)
		if err != nil {
			t.Fatalf("decode(%q): %v", encoded, err)
		}
		if snapshotID != 42 {
			t.Fatalf("snapshot id = %d", snapshotID)
		}
		if compareKeys(decoded, key) != 0 {
			t.Fatalf("round trip = %+v, want %+v", decoded, key)
		}
	}
}
