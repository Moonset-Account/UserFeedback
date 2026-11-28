package warmgrid

import (
	"errors"
	"testing"
)

// FilterAny is a pure bounding-box filter: it still returns a record whose
// bounding box overlaps the query box even when the exact geometry is disjoint.
// FilterIntersects applies the exact relation and drops that false positive.
func TestFilterAnyVsExactIntersects(t *testing.T) {
	ix := newTestIndex(t, 10)
	// A steep diagonal: its bbox spans the query box, but the line itself stays
	// well away from it.
	if _, err := ix.Insert("corner", pl(Point{0, 0}, Point{100, 1})); err != nil {
		t.Fatal(err)
	}
	// Query box in the bbox's upper-left corner: bbox overlaps, geometry does
	// not.
	box := BBox{MinX: 0, MinY: 1, MaxX: 2, MaxY: 1}
	anyRes, err := ix.Query(Query{BBox: box, Filter: FilterAny})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, anyRes, "corner")
	if anyRes[0].Relation != RelationDisjoint {
		t.Fatalf("relation = %v, want disjoint for the bbox-only hit", anyRes[0].Relation)
	}
	exact, err := ix.Query(Query{BBox: box, Filter: FilterIntersects})
	if err != nil {
		t.Fatal(err)
	}
	if len(exact) != 0 {
		t.Fatalf("exact filter returned a disjoint record: %v", ids(exact))
	}

	// A point that really is on the line is kept by the exact filter.
	onLine := BBox{MinX: 50, MinY: 0, MaxX: 50, MaxY: 1}
	exact, err = ix.Query(Query{BBox: onLine, Filter: FilterIntersects})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, exact, "corner")
}

// A geometry covering too many cells is rejected before any state changes.
func TestInsertTooManyCells(t *testing.T) {
	g := mustGrid(t, 1)
	ix := NewIndex(g)
	before := ix.Stats()
	_, err := ix.Insert("huge", pl(Point{-MaxCoord, 0}, Point{MaxCoord, 0}))
	if !errors.Is(err, ErrTooManyCells) {
		t.Fatalf("error = %v, want ErrTooManyCells", err)
	}
	if after := ix.Stats(); after != before {
		t.Fatalf("stats changed after rejected insert: %+v -> %+v", before, after)
	}
}

// Query validation happens before any grid work and leaves no state behind.
func TestQueryValidation(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: -1, MaxY: 5}}); !errors.Is(err, ErrInvalidBBox) {
		t.Fatalf("inverted query box error = %v", err)
	}
	if _, err := ix.Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: MaxCoord + 1, MaxY: 5}}); !errors.Is(err, ErrInvalidBBox) {
		t.Fatalf("out-of-range query box error = %v", err)
	}
	if _, err := ix.Snapshot().Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: 1, MaxY: 1}, Filter: Filter(200)}); !errors.Is(err, ErrInvalidFilter) {
		t.Fatalf("invalid filter error = %v", err)
	}
}

// Versions are strictly increasing and each accepted write gets a new snapshot
// sequence.
func TestVersionAndSequenceMonotonic(t *testing.T) {
	ix := newTestIndex(t, 10)
	v1, err := ix.Insert("a", pl(Point{0, 0}))
	if err != nil {
		t.Fatal(err)
	}
	seq1 := ix.Snapshot().Seq()
	v2, err := ix.Insert("b", pl(Point{1, 1}))
	if err != nil {
		t.Fatal(err)
	}
	seq2 := ix.Snapshot().Seq()
	v3, err := ix.Update("a", pl(Point{2, 2}))
	if err != nil {
		t.Fatal(err)
	}
	seq3 := ix.Snapshot().Seq()

	if !(v1 < v2 && v2 < v3) {
		t.Fatalf("versions not increasing: %d %d %d", v1, v2, v3)
	}
	if !(seq1 < seq2 && seq2 < seq3) {
		t.Fatalf("sequences not increasing: %d %d %d", seq1, seq2, seq3)
	}
	if got := mustRecord(t, ix, "a").Version; got != v3 {
		t.Fatalf("record a version = %d, want %d", got, v3)
	}
}

// Apply can delete and upsert in one atomic batch, and the resulting snapshot
// shows the full combination at once.
func TestApplyMixedBatch(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("keep", pl(Point{0, 0}, Point{5, 5})); err != nil {
		t.Fatal(err)
	}
	if _, err := ix.Insert("drop", pl(Point{20, 20}, Point{25, 25})); err != nil {
		t.Fatal(err)
	}
	old := ix.Snapshot()

	seq, err := ix.Apply(
		Change{Kind: ChangeUpsert, ID: "new", Geom: pl(Point{30, 30}, Point{35, 35})},
		Change{Kind: ChangeDelete, ID: "drop"},
		Change{Kind: ChangeUpsert, ID: "keep", Geom: pl(Point{40, 40}, Point{45, 45})},
	)
	if err != nil {
		t.Fatal(err)
	}
	if seq != ix.Snapshot().Seq() {
		t.Fatal("Apply returned a seq that does not match the published snapshot")
	}

	// Old snapshot is untouched.
	if old.Len() != 2 {
		t.Fatalf("old snapshot len = %d, want 2", old.Len())
	}
	oldDrop, err := old.Query(Query{BBox: BBox{MinX: 19, MinY: 19, MaxX: 26, MaxY: 26}})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, oldDrop, "drop")
	oldKeep, err := old.Query(Query{BBox: BBox{MinX: -1, MinY: -1, MaxX: 6, MaxY: 6}})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, oldKeep, "keep")

	// New snapshot reflects all three changes.
	snap := ix.Snapshot()
	if snap.Len() != 2 {
		t.Fatalf("new snapshot len = %d, want 2", snap.Len())
	}
	if _, ok := snap.Record("drop"); ok {
		t.Fatal("drop still present in the new snapshot")
	}
	got, err := snap.Query(Query{BBox: BBox{MinX: 29, MinY: 29, MaxX: 46, MaxY: 46}})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, got, "keep", "new")
}
