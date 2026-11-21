package relationindex

import (
	"errors"
	"fmt"
	"math/bits"
	"testing"
)

func walkForward(t *testing.T, s interface {
	FirstPage(int) (Page, error)
	FetchAfter(string, int) (Page, error)
}, pageSize int) []Row {
	t.Helper()
	var out []Row
	page, err := s.FirstPage(pageSize)
	if err != nil {
		t.Fatalf("FirstPage: %v", err)
	}
	for {
		out = append(out, page.Rows...)
		if !page.HasNext {
			break
		}
		if page.NextCursor == "" {
			t.Fatal("HasNext is true but NextCursor is empty")
		}
		page = mustPage(t)(s.FetchAfter(page.NextCursor, pageSize))
		if len(page.Rows) == 0 {
			t.Fatal("HasNext was true but the next page is empty")
		}
	}
	return out
}

func walkBackward(t *testing.T, s interface {
	LastPage(int) (Page, error)
	FetchBefore(string, int) (Page, error)
}, pageSize int) []Row {
	t.Helper()
	var chunks [][]Row
	page := mustPage(t)(s.LastPage(pageSize))
	for {
		// Each chunk is already in display order; prepend it.
		chunk := append([]Row(nil), page.Rows...)
		chunks = append(chunks, chunk)
		if !page.HasPrev {
			break
		}
		if page.PrevCursor == "" {
			t.Fatal("HasPrev is true but PrevCursor is empty")
		}
		page = mustPage(t)(s.FetchBefore(page.PrevCursor, pageSize))
		if len(page.Rows) == 0 {
			t.Fatal("HasPrev was true but the previous page is empty")
		}
	}
	var out []Row
	for i := len(chunks) - 1; i >= 0; i-- {
		out = append(out, chunks[i]...)
	}
	return out
}

// TestForwardAndBackwardWalkAgree covers the main invite: pages can be
// traversed in both directions, forward pages are in display order, and
// backward pages are returned in display order too (not reversed).
func TestForwardAndBackwardWalkAgree(t *testing.T) {
	rows := []Row{
		{ID: "r1", Region: "ap", Score: NewScore(9)},
		{ID: "r2", Region: "ap", Score: NewScore(9)},
		{ID: "r0", Region: "ap", Score: NewScore(9)},
		{ID: "r3", Region: "ap", Score: NewScore(1)},
		{ID: "r4", Region: "ap"},
		{ID: "s2", Region: "eu", Score: NewScore(5)},
		{ID: "s1", Region: "eu", Score: NewScore(5)},
		{ID: "s0", Region: "eu"},
		{ID: "t0", Region: "us", Score: NewScore(-4)},
		{ID: "t1", Region: "us", Score: NewScore(-4)},
		{ID: "t2", Region: "us", Score: NewScore(-8)},
	}
	snap := mustSnapshot(t, rows)
	want := orderRows(rows)

	for pageSize := 1; pageSize <= len(rows)+2; pageSize++ {
		fwd := walkForward(t, snap, pageSize)
		if !equalRows(fwd, want) {
			t.Fatalf("pageSize %d forward walk = %v, want %v", pageSize, ids(fwd), ids(want))
		}
		back := walkBackward(t, snap, pageSize)
		if !equalRows(back, want) {
			t.Fatalf("pageSize %d backward walk = %v, want %v", pageSize, ids(back), ids(want))
		}
	}
}

// TestTiedRowsStraddlePageBoundaries exercises the documented risk: rows with
// the same region and score can be split across a page boundary. The primary
// key tie-break must keep the walk duplicate-free and gap-free in both
// directions.
func TestTiedRowsStraddlePageBoundaries(t *testing.T) {
	const total = 100
	rows := make([]Row, 0, total)
	for i := 0; i < total; i++ {
		rows = append(rows, Row{
			ID:     fmt.Sprintf("k%03d", i),
			Region: "same",
			Score:  NewScore(5),
		})
	}
	snap := mustSnapshot(t, rows)

	for _, pageSize := range []int{1, 3, 7, 13, 99} {
		seen := make(map[string]bool, total)
		page := mustPage(t)(snap.FirstPage(pageSize))
		for {
			for _, r := range page.Rows {
				if seen[r.ID] {
					t.Fatalf("pageSize %d: duplicate row %q across page boundary", pageSize, r.ID)
				}
				seen[r.ID] = true
			}
			if !page.HasNext {
				break
			}
			next := mustPage(t)(snap.FetchAfter(page.NextCursor, pageSize))
			// Adjacent pages with identical region and score must still be
			// ordered by primary key, with no overlap.
			if len(page.Rows) > 0 && len(next.Rows) > 0 {
				if !(page.Rows[len(page.Rows)-1].ID < next.Rows[0].ID) {
					t.Fatalf("pageSize %d: boundary %q -> %q not key-ordered",
						pageSize, page.Rows[len(page.Rows)-1].ID, next.Rows[0].ID)
				}
			}
			page = next
		}
		if len(seen) != total {
			t.Fatalf("pageSize %d: saw %d rows, want %d", pageSize, len(seen), total)
		}
		back := walkBackward(t, snap, pageSize)
		if !equalRows(back, orderRows(rows)) {
			t.Fatalf("pageSize %d: backward walk lost or reordered tied rows", pageSize)
		}
	}
}

func TestPageEdgesAndCursors(t *testing.T) {
	rows := []Row{
		{ID: "a", Region: "r", Score: NewScore(3)},
		{ID: "b", Region: "r", Score: NewScore(2)},
		{ID: "c", Region: "r", Score: NewScore(1)},
		{ID: "d", Region: "r", Score: NewScore(0)},
		{ID: "e", Region: "r", Score: NewScore(-1)},
	}
	snap := mustSnapshot(t, rows)

	first := mustPage(t)(snap.FirstPage(2))
	if first.HasPrev || !first.HasNext {
		t.Fatalf("first page flags HasPrev=%v HasNext=%v", first.HasPrev, first.HasNext)
	}
	if first.PrevCursor == "" || first.NextCursor == "" {
		t.Fatal("first page must expose both cursors")
	}

	last := mustPage(t)(snap.LastPage(2))
	if !last.HasPrev || last.HasNext {
		t.Fatalf("last page flags HasPrev=%v HasNext=%v", last.HasPrev, last.HasNext)
	}
	if !equalStrings(ids(last.Rows), []string{"d", "e"}) {
		t.Fatalf("last page rows = %v", ids(last.Rows))
	}

	// Empty cursors mean "start" for FetchAfter and "end" for FetchBefore.
	if got := mustPage(t)(snap.FetchAfter("", 2)); !equalStrings(ids(got.Rows), []string{"a", "b"}) {
		t.Fatalf("FetchAfter(\"\") = %v", ids(got.Rows))
	}
	if got := mustPage(t)(snap.FetchBefore("", 2)); !equalStrings(ids(got.Rows), []string{"d", "e"}) {
		t.Fatalf("FetchBefore(\"\") = %v", ids(got.Rows))
	}

	// A cursor at the far edge must round-trip to an adjacent page.
	back := mustPage(t)(snap.FetchBefore(last.PrevCursor, 2))
	if !equalStrings(ids(back.Rows), []string{"b", "c"}) {
		t.Fatalf("FetchBefore(last.PrevCursor) = %v", ids(back.Rows))
	}
	fwd := mustPage(t)(snap.FetchAfter(first.NextCursor, 2))
	if !equalStrings(ids(fwd.Rows), []string{"c", "d"}) {
		t.Fatalf("FetchAfter(first.NextCursor) = %v", ids(fwd.Rows))
	}
}

func TestUnscoredRowsAtRegionEndAcrossPages(t *testing.T) {
	rows := []Row{
		{ID: "n1", Region: "r"},
		{ID: "n0", Region: "r"},
		{ID: "s0", Region: "r", Score: NewScore(0)},
		{ID: "s1", Region: "r", Score: NewScore(0)},
		{ID: "next", Region: "s"},
	}
	snap := mustSnapshot(t, rows)
	want := []string{"s0", "s1", "n0", "n1", "next"}
	for pageSize := 1; pageSize <= 6; pageSize++ {
		if got := ids(walkForward(t, snap, pageSize)); !equalStrings(got, want) {
			t.Fatalf("pageSize %d forward = %v, want %v", pageSize, got, want)
		}
		if got := ids(walkBackward(t, snap, pageSize)); !equalStrings(got, want) {
			t.Fatalf("pageSize %d backward = %v, want %v", pageSize, got, want)
		}
	}
}

func TestCursorFromOtherSnapshotRejected(t *testing.T) {
	first := mustSnapshot(t, []Row{{ID: "a", Region: "r", Score: NewScore(1)}})
	second := mustSnapshot(t, []Row{{ID: "a", Region: "r", Score: NewScore(1)}})
	page := mustPage(t)(first.FirstPage(1))

	if _, err := second.FetchAfter(page.NextCursor, 1); !errors.Is(err, ErrCursorSnapshotMismatch) {
		t.Fatalf("err = %v, want ErrCursorSnapshotMismatch", err)
	}
	if _, err := second.FetchBefore(page.PrevCursor, 1); !errors.Is(err, ErrCursorSnapshotMismatch) {
		t.Fatalf("err = %v, want ErrCursorSnapshotMismatch", err)
	}
}

func TestCorruptCursorRejected(t *testing.T) {
	snap := mustSnapshot(t, []Row{{ID: "a", Region: "r", Score: NewScore(1)}})
	for _, c := range []string{"!!!not-base64", "AAAA", "Ag"} {
		if _, err := snap.FetchAfter(c, 1); !errors.Is(err, ErrInvalidCursor) {
			t.Fatalf("cursor %q: err = %v, want ErrInvalidCursor", c, err)
		}
		if _, err := snap.FetchBefore(c, 1); !errors.Is(err, ErrInvalidCursor) {
			t.Fatalf("cursor %q: err = %v, want ErrInvalidCursor", c, err)
		}
	}
}

func TestSnapshotIsolationFromLaterWrites(t *testing.T) {
	store := NewStore()
	for _, r := range []Row{
		{ID: "a", Region: "r", Score: NewScore(1)},
		{ID: "b", Region: "r", Score: NewScore(2)},
		{ID: "c", Region: "r", Score: NewScore(3)},
	} {
		if err := store.Upsert(r); err != nil {
			t.Fatalf("Upsert: %v", err)
		}
	}

	old, err := store.BeginSession()
	if err != nil {
		t.Fatalf("BeginSession: %v", err)
	}
	oldPage := mustPage(t)(old.FirstPage(2))
	oldTail := mustPage(t)(old.FetchAfter(oldPage.NextCursor, 2))

	// Rescore b, delete c and add d after the first session began.
	if err := store.Upsert(Row{ID: "b", Region: "r", Score: NewScore(100)}); err != nil {
		t.Fatalf("Upsert: %v", err)
	}
	if !store.Delete("c") {
		t.Fatal("Delete(c) reported missing row")
	}
	if err := store.Upsert(Row{ID: "d", Region: "r", Score: NewScore(50)}); err != nil {
		t.Fatalf("Upsert: %v", err)
	}

	// The old session is pinned to its snapshot.
	if got := ids(mustPage(t)(old.FirstPage(10)).Rows); !equalStrings(got, []string{"c", "b", "a"}) {
		t.Fatalf("old session sees %v", got)
	}
	if got := ids(oldTail.Rows); !equalStrings(got, []string{"a"}) {
		t.Fatalf("old session tail = %v", got)
	}
	// Its cursors still resolve against the old snapshot.
	if got := ids(mustPage(t)(old.FetchBefore(oldPage.NextCursor, 10)).Rows); !equalStrings(got, []string{"c"}) {
		t.Fatalf("old session FetchBefore = %v", got)
	}
	if got := ids(mustPage(t)(old.FetchAfter(oldPage.NextCursor, 10)).Rows); !equalStrings(got, []string{"a"}) {
		t.Fatalf("old session FetchAfter = %v", got)
	}

	// A new session sees the mutations only.
	fresh, err := store.BeginSession()
	if err != nil {
		t.Fatalf("BeginSession: %v", err)
	}
	if got := ids(mustPage(t)(fresh.FirstPage(10)).Rows); !equalStrings(got, []string{"b", "d", "a"}) {
		t.Fatalf("fresh session sees %v", got)
	}
}

func TestSnapshotCopiesInput(t *testing.T) {
	rows := []Row{{ID: "a", Region: "r", Score: NewScore(1)}}
	snap := mustSnapshot(t, rows)
	rows[0].Score = NewScore(999)
	rows[0].Region = "zzz"
	if got := ids(mustPage(t)(snap.FirstPage(1)).Rows); !equalStrings(got, []string{"a"}) {
		t.Fatalf("rows = %v", got)
	}
	got := mustPage(t)(snap.FirstPage(1)).Rows[0]
	if got.Region != "r" || *got.Score != 1 {
		t.Fatalf("snapshot row mutated: %+v", got)
	}
}

// largeRows builds n rows over a few regions with ties, negatives and
// unscored entries. IDs are zero-padded so byte order matches numeric order.
func largeRows(n int) []Row {
	regions := []string{"ap", "cn", "eu", "sa", "us"}
	rows := make([]Row, 0, n)
	for i := 0; i < n; i++ {
		r := Row{
			ID:     fmt.Sprintf("id-%07d", i),
			Region: regions[i%len(regions)],
		}
		if i%11 != 0 {
			r.Score = NewScore(float64((i*37)%1000) - 500)
		}
		rows = append(rows, r)
	}
	return rows
}

// TestLargeSnapshotDirectLocate verifies that a cursor restores a page by
// locating its boundary directly instead of scanning from an edge. In 100k
// rows, jumping back to the page before the last page must touch a number of
// index entries that grows with search depth (log n) and page length only.
func TestLargeSnapshotDirectLocate(t *testing.T) {
	const n = 100000
	rows := largeRows(n)
	snap := mustSnapshot(t, rows)
	want := orderRows(rows)

	const pageSize = 25
	last := mustPage(t)(snap.LastPage(pageSize))
	if len(last.Rows) != pageSize {
		t.Fatalf("last page has %d rows", len(last.Rows))
	}
	if last.HasNext {
		t.Fatal("last page reports HasNext")
	}
	if !last.HasPrev {
		t.Fatal("last page reports no predecessor in 100k rows")
	}
	if !equalRows(last.Rows, want[n-pageSize:]) {
		t.Fatal("last page is not the display-order tail")
	}

	prev := mustPage(t)(snap.FetchBefore(last.PrevCursor, pageSize))
	start := n - 2*pageSize
	if !equalRows(prev.Rows, want[start:start+pageSize]) {
		t.Fatalf("page before last = %v, want %v", ids(prev.Rows), ids(want[start:start+pageSize]))
	}

	maxComparisons := bits.Len(uint(n)) + 1 // binary search depth bound
	if prev.Stats.Visited != pageSize {
		t.Fatalf("Visited = %d, want %d", prev.Stats.Visited, pageSize)
	}
	if prev.Stats.Comparisons <= 0 || prev.Stats.Comparisons > maxComparisons {
		t.Fatalf("Comparisons = %d, want in (0,%d]", prev.Stats.Comparisons, maxComparisons)
	}
	if prev.Stats.IndexEntries() != prev.Stats.Comparisons+prev.Stats.Visited {
		t.Fatalf("IndexEntries = %d, want %d", prev.Stats.IndexEntries(),
			prev.Stats.Comparisons+prev.Stats.Visited)
	}
	// Direct locate must not degrade to a scan: total work stays far below n.
	if prev.Stats.IndexEntries() > 4*maxComparisons+4*pageSize {
		t.Fatalf("locating the page before last touched %d entries, want O(log n + pageSize)",
			prev.Stats.IndexEntries())
	}

	// Access volume grows with page length: quadrupling the page size
	// quadruples visited entries while keeping the search bound.
	lastWide := mustPage(t)(snap.LastPage(4 * pageSize))
	prevWide := mustPage(t)(snap.FetchBefore(lastWide.PrevCursor, 4*pageSize))
	if prevWide.Stats.Visited != 4*pageSize {
		t.Fatalf("Visited = %d, want %d", prevWide.Stats.Visited, 4*pageSize)
	}
	if prevWide.Stats.Visited <= prev.Stats.Visited {
		t.Fatalf("visited entries did not grow with page length: %d vs %d",
			prevWide.Stats.Visited, prev.Stats.Visited)
	}
	if prevWide.Stats.Comparisons > maxComparisons {
		t.Fatalf("Comparisons = %d, want <= %d", prevWide.Stats.Comparisons, maxComparisons)
	}

	// Access volume also grows with search depth: a larger snapshot has a
	// deeper binary search, so it compares at least as many entries, but the
	// growth is logarithmic rather than linear in n.
	smallRows := largeRows(1000)
	small := mustSnapshot(t, smallRows)
	smallLast := mustPage(t)(small.LastPage(pageSize))
	smallPrev := mustPage(t)(small.FetchBefore(smallLast.PrevCursor, pageSize))
	smallBound := bits.Len(uint(len(smallRows))) + 1
	if smallPrev.Stats.Comparisons <= 0 || smallPrev.Stats.Comparisons > smallBound {
		t.Fatalf("small Comparisons = %d, want in (0,%d]", smallPrev.Stats.Comparisons, smallBound)
	}
	if prev.Stats.Comparisons <= smallPrev.Stats.Comparisons {
		t.Fatalf("search depth did not grow with n: 1k -> %d, 100k -> %d",
			smallPrev.Stats.Comparisons, prev.Stats.Comparisons)
	}
	if prev.Stats.Comparisons > 2*smallPrev.Stats.Comparisons {
		t.Fatalf("100x rows grew comparisons from %d to %d; expected logarithmic growth",
			smallPrev.Stats.Comparisons, prev.Stats.Comparisons)
	}
}

// TestDirectLocateFromArbitraryDepth checks that any page can be restored
// from its cursors with the same bounded cost, regardless of how far into the
// snapshot it sits.
func TestDirectLocateFromArbitraryDepth(t *testing.T) {
	const n = 50000
	const pageSize = 10
	rows := largeRows(n)
	snap := mustSnapshot(t, rows)
	want := orderRows(rows)
	maxComparisons := bits.Len(uint(n)) + 1

	// Find the cursor that precedes a page deep in the middle by walking
	// forward in page-sized steps, then restore that page directly from the
	// cursor and check its cost.
	target := n / 2         // index of the first row of the page we want
	stopAfter := target - 1 // index of the cursor row we need

	page := mustPage(t)(snap.FirstPage(pageSize))
	lastIndex := len(page.Rows) - 1
	for lastIndex < stopAfter {
		page = mustPage(t)(snap.FetchAfter(page.NextCursor, pageSize))
		lastIndex += len(page.Rows)
	}

	start := lastIndex + 1
	probe := mustPage(t)(snap.FetchAfter(page.NextCursor, pageSize))
	if !equalRows(probe.Rows, want[start:start+pageSize]) {
		t.Fatalf("probe rows = %v, want %v", ids(probe.Rows), ids(want[start:start+pageSize]))
	}
	if probe.Stats.Comparisons > maxComparisons || probe.Stats.Visited != pageSize {
		t.Fatalf("probe stats = %+v, want comparisons <= %d visited %d",
			probe.Stats, maxComparisons, pageSize)
	}
}
