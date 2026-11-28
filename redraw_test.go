package warmgrid

import (
	"fmt"
	"reflect"
	"testing"
)

// cellPtr identifies a cell bucket's backing map so a test can prove that an
// untouched bucket is shared between two snapshots rather than copied.
func cellPtr(m map[CellKey]map[RecordID]uint64, key CellKey) string {
	bucket, ok := m[key]
	if !ok {
		return "<missing>"
	}
	return fmt.Sprintf("%x", reflect.ValueOf(bucket).Pointer())
}

// A local redraw must only rewrite cell buckets on the union of the old and new
// cover sets. Buckets outside that union are shared with the previous snapshot,
// which is the concrete meaning of "local" in this index.
func TestRedrawRewritesOnlyTouchedCells(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("mover", pl(Point{0, 0}, Point{5, 5})); err != nil {
		t.Fatal(err)
	}
	if _, err := ix.Insert("neighbour", pl(Point{5, 5}, Point{15, 5})); err != nil {
		t.Fatal(err)
	}
	// Far away so its bucket must be untouched.
	if _, err := ix.Insert("far", pl(Point{100, 100}, Point{105, 105})); err != nil {
		t.Fatal(err)
	}

	before := ix.Snapshot()
	if _, err := ix.Update("mover", pl(Point{300, 300}, Point{305, 305})); err != nil {
		t.Fatal(err)
	}
	after := ix.Snapshot()

	untouched := CellKey{X: 10, Y: 10} // far record's cell
	if cellPtr(before.state.cells, untouched) != cellPtr(after.state.cells, untouched) {
		t.Fatal("untouched cell bucket was copied, redraw was not local")
	}
	oldCell := CellKey{X: 0, Y: 0}
	if cellPtr(before.state.cells, oldCell) == cellPtr(after.state.cells, oldCell) {
		t.Fatal("vacated cell bucket should have been rewritten")
	}
	newCell := CellKey{X: 30, Y: 30}
	if cellPtr(before.state.cells, newCell) == cellPtr(after.state.cells, newCell) {
		t.Fatal("new cell bucket should have been created for the new snapshot")
	}
	// The old snapshot's bucket must still hold the old term; if the writer had
	// mutated a shared map in place, the old query would lose it.
	oldBucket := before.state.cells[oldCell]
	if _, ok := oldBucket["mover"]; !ok {
		t.Fatal("old snapshot lost its mover term after redraw")
	}
	if _, ok := after.state.cells[oldCell]["mover"]; ok {
		t.Fatal("new snapshot still has mover in its old cell")
	}
}

// White-box check for the defensive version guard: even if a stale term and a
// current term for the same record are both present in the covered cells (for
// example as the residue of an interrupted or externally tiled update), the
// query returns exactly one result, from the current version.
func TestStaleTermDeduplicatedByVersion(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("r", pl(Point{0, 0}, Point{5, 5})); err != nil {
		t.Fatal(err)
	}
	rec := mustRecord(t, ix, "r")

	// Inject a stale term for the same id into a *different* cell that the
	// query will cover.
	stale := ix.Snapshot()
	stale.state = stale.state.clone()
	staleCell := CellKey{X: 5, Y: 5}
	stale.state.addTerm(staleCell, "r", rec.Version-1)

	res, err := stale.Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: 59, MaxY: 59}})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, res, "r")
	if len(res) != 1 {
		t.Fatalf("got %d results for one record with a stale term", len(res))
	}
	if res[0].Version != rec.Version {
		t.Fatalf("result version = %d, want current %d", res[0].Version, rec.Version)
	}
}
