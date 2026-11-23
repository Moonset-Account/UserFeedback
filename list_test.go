package objstore

import (
	"errors"
	"fmt"
	"reflect"
	"sync"
	"testing"
)

// --- helpers ---------------------------------------------------------------

func obj(names ...string) []Entry {
	out := make([]Entry, len(names))
	for i, n := range names {
		out[i] = Entry{Kind: EntryObject, Name: n}
	}
	return out
}

func cp(names ...string) []Entry {
	out := make([]Entry, len(names))
	for i, n := range names {
		out[i] = Entry{Kind: EntryCommonPrefix, Name: n}
	}
	return out
}

func cat(parts ...[]Entry) []Entry {
	var out []Entry
	for _, p := range parts {
		out = append(out, p...)
	}
	return out
}

func newStoreWith(keys ...string) *Store {
	s := NewStore()
	for _, k := range keys {
		s.Put(k, []byte("v-"+k))
	}
	return s
}

// listAll pages through the whole listing and returns the concatenated
// entries, failing the test on any error.
func listAll(t *testing.T, s *Store, req ListRequest) []Entry {
	t.Helper()
	page, err := s.List(req)
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	out := append([]Entry{}, page.Entries...)
	for page.Token != "" {
		page, err = s.ListNext(page.Token)
		if err != nil {
			t.Fatalf("ListNext: %v", err)
		}
		out = append(out, page.Entries...)
	}
	return out
}

// oneShot scans the whole listing in a single huge page.
func oneShot(t *testing.T, s *Store, prefix, delim string) []Entry {
	t.Helper()
	page, err := s.List(ListRequest{Prefix: prefix, Delimiter: delim, PageSize: 1 << 30})
	if err != nil {
		t.Fatalf("one-shot List: %v", err)
	}
	if page.Token != "" {
		t.Fatalf("one-shot List returned a continuation token")
	}
	return page.Entries
}

func requireEntries(t *testing.T, got, want []Entry) {
	t.Helper()
	if len(got) == 0 && len(want) == 0 {
		return
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("entries mismatch\n got: %v\nwant: %v", got, want)
	}
}

// --- normal scenarios ------------------------------------------------------

// TestFoldOrdering checks the exact folded entry stream for a mixed data set,
// including a key exactly equal to a collapsible prefix.
func TestFoldOrdering(t *testing.T) {
	s := newStoreWith("a/1", "a/2", "a/x/1", "b", "b/1", "c/", "c/2", "d")
	want := cat(
		cp("a/"),  // folds a/1, a/2, a/x/1
		obj("b"),  // plain object
		cp("b/"),  // folds b/1
		obj("c/"), // key exactly equal to the collapsible prefix: object first
		cp("c/"),  // ... then the prefix itself, folding c/2
		obj("d"),
	)
	requireEntries(t, oneShot(t, s, "", "/"), want)
	// Every page size must reassemble to the same stream.
	for ps := 1; ps <= len(want)+1; ps++ {
		got := listAll(t, s, ListRequest{Delimiter: "/", PageSize: ps})
		requireEntries(t, got, want)
	}
}

// TestPrefixScoping restricts the listing to a prefix.
func TestPrefixScoping(t *testing.T) {
	s := newStoreWith("a/1", "a/2", "a/x/1", "a/x/2", "b/1")
	want := cat(obj("a/1", "a/2"), cp("a/x/"))
	for ps := 1; ps <= 4; ps++ {
		got := listAll(t, s, ListRequest{Prefix: "a/", Delimiter: "/", PageSize: ps})
		requireEntries(t, got, want)
	}
}

// TestPagedEqualsOneShot sweeps page sizes over a nested data set and
// compares the concatenation of pages against a one-shot scan.
func TestPagedEqualsOneShot(t *testing.T) {
	var keys []string
	for _, dir := range []string{"a", "b", "c"} {
		for i := 0; i < 4; i++ {
			keys = append(keys, fmt.Sprintf("%s/f%d", dir, i))
			keys = append(keys, fmt.Sprintf("%s/sub/g%d", dir, i))
		}
	}
	keys = append(keys, "top1", "top2")
	s := newStoreWith(keys...)
	want := oneShot(t, s, "", "/")
	if len(want) != 5 { // cp a/, cp b/, cp c/, obj top1, obj top2
		t.Fatalf("unexpected one-shot entry count: %d (%v)", len(want), want)
	}
	for ps := 1; ps <= 7; ps++ {
		got := listAll(t, s, ListRequest{Delimiter: "/", PageSize: ps})
		requireEntries(t, got, want)
	}
}

// TestPageBoundaryExactlyAtCommonPrefix ends a page exactly on a folded
// common prefix; the next page must not re-emit its subtree.
func TestPageBoundaryExactlyAtCommonPrefix(t *testing.T) {
	s := newStoreWith("a/1", "a/2", "b/1", "c")
	p1, err := s.List(ListRequest{Delimiter: "/", PageSize: 2})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	requireEntries(t, p1.Entries, cp("a/", "b/"))
	if p1.Token == "" {
		t.Fatalf("expected a continuation token after page 1")
	}
	p2, err := s.ListNext(p1.Token)
	if err != nil {
		t.Fatalf("ListNext: %v", err)
	}
	requireEntries(t, p2.Entries, obj("c"))
	if p2.Token != "" {
		t.Fatalf("expected listing to be complete, got token %q", p2.Token)
	}
}

// TestEmptyDelimiter disables folding entirely.
func TestEmptyDelimiter(t *testing.T) {
	s := newStoreWith("a/1", "a/2", "b")
	want := obj("a/1", "a/2", "b")
	for ps := 1; ps <= 4; ps++ {
		requireEntries(t, listAll(t, s, ListRequest{PageSize: ps}), want)
	}
}

// TestMultiByteDelimiter folds on a multi-byte delimiter.
func TestMultiByteDelimiter(t *testing.T) {
	s := newStoreWith("a::1", "a::2", "b")
	want := cat(cp("a::"), obj("b"))
	for ps := 1; ps <= 3; ps++ {
		requireEntries(t, listAll(t, s, ListRequest{Delimiter: "::", PageSize: ps}), want)
	}
}

// --- boundary scenarios ----------------------------------------------------

// TestKeyEqualsCollapsiblePrefix splits the object/prefix pair across pages
// in every possible way.
func TestKeyEqualsCollapsiblePrefix(t *testing.T) {
	s := newStoreWith("a/", "a/b", "a/c", "b")
	want := cat(obj("a/"), cp("a/"), obj("b"))
	for ps := 1; ps <= 4; ps++ {
		requireEntries(t, listAll(t, s, ListRequest{Delimiter: "/", PageSize: ps}), want)
	}
	// Page size 1 must emit object, then prefix, then b as separate pages.
	p1, _ := s.List(ListRequest{Delimiter: "/", PageSize: 1})
	requireEntries(t, p1.Entries, obj("a/"))
	p2, err := s.ListNext(p1.Token)
	if err != nil {
		t.Fatalf("ListNext: %v", err)
	}
	requireEntries(t, p2.Entries, cp("a/"))
	p3, err := s.ListNext(p2.Token)
	if err != nil {
		t.Fatalf("ListNext: %v", err)
	}
	requireEntries(t, p3.Entries, obj("b"))
	if p3.Token != "" {
		t.Fatalf("expected listing to be complete")
	}
}

// TestPrefixEndByteBoundary exercises resume points computed by incrementing
// trailing 0xff bytes, and a common prefix whose subtree reaches the end of
// the key space.
func TestPrefixEndByteBoundary(t *testing.T) {
	s := newStoreWith("a\xff1", "a\xff2", "b")
	want := cat(cp("a\xff"), obj("b"))
	for ps := 1; ps <= 3; ps++ {
		requireEntries(t, listAll(t, s, ListRequest{Delimiter: "\xff", PageSize: ps}), want)
	}

	// A common prefix ending in 0xff whose subtree is the tail of the key
	// space: the listing must terminate cleanly right after it, and the
	// non-UTF-8 bytes in keys and delimiter must survive the token
	// round-trip untouched.
	s2 := newStoreWith("top", "\xff\xffa", "\xff\xffb")
	p1, err := s2.List(ListRequest{Delimiter: "\xff", PageSize: 1})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	requireEntries(t, p1.Entries, obj("top"))
	if p1.Token == "" {
		t.Fatalf("expected a continuation token after page 1")
	}
	p2, err := s2.ListNext(p1.Token)
	if err != nil {
		t.Fatalf("ListNext: %v", err)
	}
	requireEntries(t, p2.Entries, cp("\xff"))
	if p2.Token != "" {
		t.Fatalf("expected listing to be complete at the end of the key space, got token %q", p2.Token)
	}
}

// TestEmptyStoreAndNoMatch lists an empty store and a prefix with no keys.
func TestEmptyStoreAndNoMatch(t *testing.T) {
	s := NewStore()
	page, err := s.List(ListRequest{Delimiter: "/", PageSize: 10})
	if err != nil {
		t.Fatalf("List on empty store: %v", err)
	}
	if len(page.Entries) != 0 || page.Token != "" {
		t.Fatalf("expected empty page, got %v token %q", page.Entries, page.Token)
	}
	s.Put("a/1", nil)
	requireEntries(t, listAll(t, s, ListRequest{Prefix: "zzz/", Delimiter: "/", PageSize: 10}), nil)
}

// TestInvalidInputs rejects a non-positive page size and a garbage token.
func TestInvalidInputs(t *testing.T) {
	s := newStoreWith("a")
	if _, err := s.List(ListRequest{PageSize: 0}); err == nil {
		t.Fatalf("expected error for PageSize 0")
	}
	if _, err := s.ListNext("not-a-token"); !errors.Is(err, ErrInvalidToken) {
		t.Fatalf("expected ErrInvalidToken, got %v", err)
	}
}

// --- interleaved scenarios -------------------------------------------------

// TestShardSplitBetweenPages splits a shard between two pages; continuation
// must keep reading the pinned snapshot under the new layout.
func TestShardSplitBetweenPages(t *testing.T) {
	s := newStoreWith("a/1", "a/2", "b/1", "b/2", "c")
	p1, err := s.List(ListRequest{Delimiter: "/", PageSize: 2})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	requireEntries(t, p1.Entries, cp("a/", "b/"))
	s.Split("b")
	s.Split("a/2")
	p2, err := s.ListNext(p1.Token)
	if err != nil {
		t.Fatalf("ListNext after split: %v", err)
	}
	requireEntries(t, p2.Entries, obj("c"))
	if p2.Token != "" {
		t.Fatalf("expected listing to be complete")
	}
}

// TestShardMergeBetweenPages merges all shards between two pages.
func TestShardMergeBetweenPages(t *testing.T) {
	s := newStoreWith("a/1", "a/2", "b/1", "b/2", "c")
	s.Split("b")
	s.Split("a/2")
	want := oneShot(t, s, "", "/")
	p1, err := s.List(ListRequest{Delimiter: "/", PageSize: 1})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	s.MergeAll()
	p2, err := s.ListNext(p1.Token)
	if err != nil {
		t.Fatalf("ListNext after merge: %v", err)
	}
	p3, err := s.ListNext(p2.Token)
	if err != nil {
		t.Fatalf("ListNext after merge: %v", err)
	}
	requireEntries(t, cat(p1.Entries, p2.Entries, p3.Entries), want)
}

// TestMigrationOverlap reads through a migratory split whose old and new
// shards serve overlapping ranges; entries must not be duplicated.
func TestMigrationOverlap(t *testing.T) {
	s := newStoreWith("a/1", "a/2", "a/3", "b/1", "c")
	want := cat(cp("a/", "b/"), obj("c"))

	s.BeginSplit("a/2")
	// The old shard and both new half-shards now serve overlapping ranges.
	requireEntries(t, listAll(t, s, ListRequest{Delimiter: "/", PageSize: 1}), want)
	// Writes during migration land on every overlapping shard and appear once.
	s.Put("a/4", []byte("v-a/4")) // folds into the already-folded cp a/
	requireEntries(t, listAll(t, s, ListRequest{Delimiter: "/", PageSize: 2}), want)
	s.CompleteSplit("a/2")
	requireEntries(t, listAll(t, s, ListRequest{Delimiter: "/", PageSize: 1}), want)

	// A listing started before the migration continues across it.
	p1, err := s.List(ListRequest{Delimiter: "/", PageSize: 2})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	requireEntries(t, p1.Entries, cp("a/", "b/"))
	s.BeginSplit("b")
	s.Put("a/9", nil) // under an already-emitted prefix, mid-migration
	s.CompleteSplit("b")
	p2, err := s.ListNext(p1.Token)
	if err != nil {
		t.Fatalf("ListNext across migration: %v", err)
	}
	requireEntries(t, p2.Entries, obj("c"))
}

// TestInsertUnderEmittedPrefixBetweenPages adds objects under an
// already-emitted common prefix between pages; the pinned snapshot must not
// see them, and the concatenation must equal a one-shot scan of that
// snapshot.
func TestInsertUnderEmittedPrefixBetweenPages(t *testing.T) {
	s := newStoreWith("a/1", "a/2", "b")
	before := oneShot(t, s, "", "/")
	requireEntries(t, before, cat(cp("a/"), obj("b")))

	p1, err := s.List(ListRequest{Delimiter: "/", PageSize: 1})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	requireEntries(t, p1.Entries, cp("a/"))

	// Mutations after the snapshot: inside the emitted subtree, right after
	// it, and far away.
	s.Put("a/0", nil)
	s.Put("a/zzz", nil)
	s.Put("ab", nil)

	p2, err := s.ListNext(p1.Token)
	if err != nil {
		t.Fatalf("ListNext: %v", err)
	}
	requireEntries(t, p2.Entries, obj("b"))
	if p2.Token != "" {
		t.Fatalf("expected listing to be complete")
	}
	requireEntries(t, cat(p1.Entries, p2.Entries), before)

	// A fresh one-shot scan sees the new objects, proving the paged listing
	// really was pinned to the old snapshot.
	requireEntries(t, oneShot(t, s, "", "/"), cat(cp("a/"), obj("ab", "b")))
}

// TestDeleteBetweenPages removes an object after the first page; the pinned
// snapshot must still return it on later pages.
func TestDeleteBetweenPages(t *testing.T) {
	s := newStoreWith("a", "b", "c")
	p1, err := s.List(ListRequest{PageSize: 1})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	requireEntries(t, p1.Entries, obj("a"))
	s.Delete("b")
	s.Put("a2", nil) // new key inside the remaining range: must stay invisible
	p2, err := s.ListNext(p1.Token)
	if err != nil {
		t.Fatalf("ListNext: %v", err)
	}
	requireEntries(t, p2.Entries, obj("b"))
	p3, err := s.ListNext(p2.Token)
	if err != nil {
		t.Fatalf("ListNext: %v", err)
	}
	requireEntries(t, p3.Entries, obj("c"))
	if p3.Token != "" {
		t.Fatalf("expected listing to be complete")
	}
	requireEntries(t, oneShot(t, s, "", ""), obj("a", "a2", "c"))
}

// TestSnapshotExpiry compacts away the pinned snapshot; continuation must
// fail explicitly with ErrSnapshotExpired instead of silently continuing
// from a newer version.
func TestSnapshotExpiry(t *testing.T) {
	s := newStoreWith("a", "b", "c")
	p1, err := s.List(ListRequest{PageSize: 1})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	requireEntries(t, p1.Entries, obj("a"))

	s.Put("d", nil) // advance the store version past the pinned snapshot
	s.Compact()     // drop history older than the current version

	page, err := s.ListNext(p1.Token)
	if !errors.Is(err, ErrSnapshotExpired) {
		t.Fatalf("expected ErrSnapshotExpired, got page %v err %v", page.Entries, err)
	}
	if len(page.Entries) != 0 {
		t.Fatalf("expired continuation must not return entries, got %v", page.Entries)
	}

	// A fresh listing on the current version still works.
	requireEntries(t, listAll(t, s, ListRequest{PageSize: 2}), obj("a", "b", "c", "d"))
}

// TestSnapshotSurvivesCompactionWhenCurrent pins a snapshot at the version
// compaction retains; continuation must keep working.
func TestSnapshotSurvivesCompactionWhenCurrent(t *testing.T) {
	s := newStoreWith("a", "b", "c")
	p1, err := s.List(ListRequest{PageSize: 1})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	s.Compact() // retains exactly the pinned version
	p2, err := s.ListNext(p1.Token)
	if err != nil {
		t.Fatalf("ListNext after compact at pinned version: %v", err)
	}
	requireEntries(t, p2.Entries, obj("b"))
}

// TestSplitMergeInsertInterleaved combines every interleaving the store
// supports between pages of a single listing.
func TestSplitMergeInsertInterleaved(t *testing.T) {
	s := newStoreWith("p/a", "p/b", "q/a", "r")
	want := oneShot(t, s, "", "/")
	requireEntries(t, want, cat(cp("p/", "q/"), obj("r")))

	p1, err := s.List(ListRequest{Delimiter: "/", PageSize: 1})
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	requireEntries(t, p1.Entries, cp("p/"))

	s.Split("q")
	s.Put("p/new", nil) // under the already-emitted prefix: invisible
	s.MergeAll()

	p2, err := s.ListNext(p1.Token)
	if err != nil {
		t.Fatalf("ListNext: %v", err)
	}
	requireEntries(t, p2.Entries, cp("q/"))

	s.BeginSplit("r")
	s.Delete("r") // deleted after the snapshot: still visible to it
	s.CompleteSplit("r")

	p3, err := s.ListNext(p2.Token)
	if err != nil {
		t.Fatalf("ListNext: %v", err)
	}
	requireEntries(t, p3.Entries, obj("r"))
	if p3.Token != "" {
		t.Fatalf("expected listing to be complete")
	}
	requireEntries(t, cat(p1.Entries, p2.Entries, p3.Entries), want)
}

// TestConcurrentWritesDuringPaging hammers the store with concurrent writes
// and a shard split while a listing pages through its pinned snapshot.
func TestConcurrentWritesDuringPaging(t *testing.T) {
	s := NewStore()
	var want []Entry
	for i := 0; i < 50; i++ {
		k := fmt.Sprintf("k%02d", i)
		s.Put(k, []byte(k))
		want = append(want, Entry{Kind: EntryObject, Name: k})
	}

	page, err := s.List(ListRequest{PageSize: 4})
	if err != nil {
		t.Fatalf("List: %v", err)
	}

	var wg sync.WaitGroup
	for g := 0; g < 4; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 25; i++ {
				s.Put(fmt.Sprintf("w%02d-%02d", g, i), nil)
			}
		}(g)
	}

	got := append([]Entry{}, page.Entries...)
	pages := 1
	for page.Token != "" {
		if pages == 3 {
			s.Split("k25") // reshuffle the layout mid-listing
		}
		page, err = s.ListNext(page.Token)
		if err != nil {
			t.Fatalf("ListNext: %v", err)
		}
		got = append(got, page.Entries...)
		pages++
	}
	wg.Wait()
	requireEntries(t, got, want)
}
