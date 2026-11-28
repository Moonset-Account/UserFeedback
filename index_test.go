package warmgrid

import (
	"errors"
	"sort"
	"sync"
	"testing"
)

func newTestIndex(t *testing.T, cellSize int64) *Index {
	t.Helper()
	return NewIndex(mustGrid(t, cellSize))
}

func ids(results []Result) []RecordID {
	out := make([]RecordID, len(results))
	for i, r := range results {
		out[i] = r.ID
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func assertIDs(t *testing.T, results []Result, want ...RecordID) {
	t.Helper()
	got := ids(results)
	sort.Slice(want, func(i, j int) bool { return want[i] < want[j] })
	if len(got) != len(want) {
		t.Fatalf("ids = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("ids = %v, want %v", got, want)
		}
	}
}

func TestInsertQuerySingleAndMultiCell(t *testing.T) {
	ix := newTestIndex(t, 10)

	// Short route inside one cell.
	if _, err := ix.Insert("short", pl(Point{1, 1}, Point{5, 5})); err != nil {
		t.Fatal(err)
	}
	// Long route crossing four cells (bbox 0..25 spans cells 0..2 in x and y
	// with size 10, but the diagonal only touches 3 of them).
	if _, err := ix.Insert("long", pl(Point{0, 0}, Point{25, 25})); err != nil {
		t.Fatal(err)
	}

	stats := ix.Stats()
	if stats.Records != 2 {
		t.Fatalf("Records = %d, want 2", stats.Records)
	}
	// The long record must be indexed in more than one cell while remaining a
	// single main record.
	rec, ok := ix.Snapshot().Record("long")
	if !ok {
		t.Fatal("long record missing")
	}
	if len(rec.Cells) <= 1 {
		t.Fatalf("long record covers %d cells, want > 1", len(rec.Cells))
	}
	if stats.CellTerms != len(rec.Cells)+len(mustRecord(t, ix, "short").Cells) {
		t.Fatalf("CellTerms = %d inconsistent with per-record cells", stats.CellTerms)
	}

	res, err := ix.Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: 9, MaxY: 9}})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, res, "long", "short")
}

func mustRecord(t *testing.T, ix *Index, id RecordID) Record {
	t.Helper()
	rec, ok := ix.Snapshot().Record(id)
	if !ok {
		t.Fatalf("record %q missing", id)
	}
	return rec
}

// A multi-cell geometry must produce exactly one result, no matter how many of
// its cells the query box touches.
func TestQueryDeduplicatesAcrossCells(t *testing.T) {
	ix := newTestIndex(t, 10)
	// Diagonal across cells (0,0), (1,1), (2,2) and several boundary-touching
	// cells.
	if _, err := ix.Insert("diag", pl(Point{0, 0}, Point{25, 25})); err != nil {
		t.Fatal(err)
	}
	// Query box that covers many of the record's cells at once.
	res, err := ix.Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: 29, MaxY: 29}})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, res, "diag")
	if len(res) != 1 {
		t.Fatalf("got %d results for one multi-cell record", len(res))
	}
	if res[0].Version == 0 {
		t.Fatal("result version must be set")
	}
}

func TestFilterInsideAndTouches(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("in", pl(Point{2, 2}, Point{6, 6})); err != nil {
		t.Fatal(err)
	}
	// A route lying exactly on the box edge is still entirely within the
	// closed box, so it is classified Inside, not Touches.
	if _, err := ix.Insert("edge", pl(Point{10, 0}, Point{10, 9})); err != nil {
		t.Fatal(err)
	}
	// A route that starts on the edge and leaves the box only touches it.
	if _, err := ix.Insert("touch", pl(Point{10, 5}, Point{20, 5})); err != nil {
		t.Fatal(err)
	}
	if _, err := ix.Insert("cross", pl(Point{8, 5}, Point{20, 5})); err != nil {
		t.Fatal(err)
	}

	box := BBox{MinX: 0, MinY: 0, MaxX: 10, MaxY: 10}
	all, err := ix.Query(Query{BBox: box})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, all, "in", "edge", "touch", "cross")

	inside, err := ix.Query(Query{BBox: box, Filter: FilterInside})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, inside, "in", "edge")

	touch, err := ix.Query(Query{BBox: box, Filter: FilterTouches})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, touch, "touch")

	cross, err := ix.Query(Query{BBox: box, Filter: FilterCrosses})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, cross, "cross")

	if _, err := ix.Query(Query{BBox: box, Filter: Filter(99)}); !errors.Is(err, ErrInvalidFilter) {
		t.Fatalf("invalid filter error = %v", err)
	}
}

func TestInsertErrors(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("", pl(Point{0, 0})); !errors.Is(err, ErrEmptyID) {
		t.Fatalf("empty id error = %v", err)
	}
	if _, err := ix.Insert("a", Polyline{}); !errors.Is(err, ErrInvalidGeometry) {
		t.Fatalf("empty geometry error = %v", err)
	}
	if _, err := ix.Insert("a", pl(Point{0, 0})); err != nil {
		t.Fatal(err)
	}
	if _, err := ix.Insert("a", pl(Point{1, 1})); !errors.Is(err, ErrExists) {
		t.Fatalf("duplicate insert error = %v", err)
	}
	if _, err := ix.Update("missing", pl(Point{0, 0})); !errors.Is(err, ErrNotFound) {
		t.Fatalf("update missing error = %v", err)
	}
	if err := ix.Delete("missing"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("delete missing error = %v", err)
	}
	// A failed insert must not consume a version or create a record.
	before := ix.Stats()
	if _, err := ix.Insert("b", pl(Point{MaxCoord + 1, 0})); err == nil {
		t.Fatal("expected out-of-range insert to fail")
	}
	if after := ix.Stats(); after != before {
		t.Fatalf("stats changed after failed insert: %+v -> %+v", before, after)
	}
}

// The core isolation property: after a local redraw, an old snapshot keeps
// seeing exactly one old version (deduplicated across its old cells) and a new
// snapshot immediately sees the new cover cells.
func TestSnapshotSeesOldAndNewVersions(t *testing.T) {
	ix := newTestIndex(t, 10)
	oldGeom := pl(Point{0, 0}, Point{25, 25}) // spans cells around origin
	if _, err := ix.Insert("road", oldGeom); err != nil {
		t.Fatal(err)
	}
	oldSnap := ix.Snapshot()
	oldRec := mustRecord(t, ix, "road")

	// Redraw far away, so the old cells lose their term and new cells gain one.
	newGeom := pl(Point{100, 100}, Point{125, 125})
	newVersion, err := ix.Update("road", newGeom)
	if err != nil {
		t.Fatal(err)
	}
	newSnap := ix.Snapshot()
	if newSnap.Seq() <= oldSnap.Seq() {
		t.Fatalf("sequence did not advance: %d -> %d", oldSnap.Seq(), newSnap.Seq())
	}
	if newVersion == oldRec.Version {
		t.Fatal("version did not advance")
	}

	// Old snapshot: old cells still resolve, exactly once, old version.
	nearOld, err := oldSnap.Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: 9, MaxY: 9}})
	if err != nil {
		t.Fatal(err)
	}
	if len(nearOld) != 1 {
		t.Fatalf("old snapshot near old cells: got %d results", len(nearOld))
	}
	if nearOld[0].Version != oldRec.Version {
		t.Fatalf("old snapshot version = %d, want %d", nearOld[0].Version, oldRec.Version)
	}
	if nearOld[0].Bounds != oldRec.Bounds {
		t.Fatalf("old snapshot geometry changed")
	}
	// Old snapshot: new cells must be invisible.
	nearNew, err := oldSnap.Query(Query{BBox: BBox{MinX: 100, MinY: 100, MaxX: 109, MaxY: 109}})
	if err != nil {
		t.Fatal(err)
	}
	if len(nearNew) != 0 {
		t.Fatalf("old snapshot saw new cells: %v", ids(nearNew))
	}

	// New snapshot: new cells visible, old cells empty.
	nearNew, err = newSnap.Query(Query{BBox: BBox{MinX: 100, MinY: 100, MaxX: 109, MaxY: 109}})
	if err != nil {
		t.Fatal(err)
	}
	if len(nearNew) != 1 || nearNew[0].Version != newVersion {
		t.Fatalf("new snapshot new cells = %+v, want one result version %d", nearNew, newVersion)
	}
	nearOld, err = newSnap.Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: 9, MaxY: 9}})
	if err != nil {
		t.Fatal(err)
	}
	if len(nearOld) != 0 {
		t.Fatalf("new snapshot still saw old cells: %v", ids(nearOld))
	}
}

// An intermediate span must never be returned: a record that keeps its old
// version in some cells while new cell terms exist elsewhere is exactly the
// mixed-version tiling the design prevents.
func TestRedrawKeepsSingleVersionPerSnapshot(t *testing.T) {
	ix := newTestIndex(t, 5)
	if _, err := ix.Insert("r", pl(Point{0, 0}, Point{24, 0})); err != nil {
		t.Fatal(err)
	}
	// A query covering the whole old plus new area after each redraw.
	bigBox := BBox{MinX: -50, MinY: -50, MaxX: 200, MaxY: 200}

	for i := 0; i < 20; i++ {
		x := int64(i * 7)
		if _, err := ix.Update("r", pl(Point{x, 0}, Point{x + 24, 0})); err != nil {
			t.Fatal(err)
		}
		res, err := ix.Query(Query{BBox: bigBox})
		if err != nil {
			t.Fatal(err)
		}
		if len(res) != 1 {
			t.Fatalf("iteration %d: got %d results, want exactly 1", i, len(res))
		}
	}
}

// Apply must be all-or-nothing and must publish a single new snapshot.
func TestApplyAtomic(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("a", pl(Point{0, 0}, Point{5, 5})); err != nil {
		t.Fatal(err)
	}
	before := ix.Snapshot()

	// One invalid change aborts the whole batch.
	_, err := ix.Apply(
		Change{Kind: ChangeUpsert, ID: "a", Geom: pl(Point{50, 50})},
		Change{Kind: ChangeUpsert, ID: "b", Geom: Polyline{}},
	)
	if !errors.Is(err, ErrInvalidGeometry) {
		t.Fatalf("Apply error = %v, want ErrInvalidGeometry", err)
	}
	if ix.Snapshot().Seq() != before.Seq() {
		t.Fatal("failed Apply published a snapshot")
	}
	res, err := ix.Query(Query{BBox: BBox{MinX: -1, MinY: -1, MaxX: 6, MaxY: 6}})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, res, "a")
	if res[0].Version != mustRecord(t, ix, "a").Version {
		t.Fatal("failed Apply changed the geometry")
	}

	// A valid batch publishes once, with all changes visible together.
	seq, err := ix.Apply(
		Change{Kind: ChangeUpsert, ID: "a", Geom: pl(Point{50, 50}, Point{55, 55})},
		Change{Kind: ChangeUpsert, ID: "b", Geom: pl(Point{60, 60}, Point{65, 65})},
		Change{Kind: ChangeDelete, ID: "gone"}, // unknown -> error
	)
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("Apply delete missing error = %v", err)
	}
	if seq != 0 {
		t.Fatalf("failed Apply returned seq %d", seq)
	}

	seq, err = ix.Apply(
		Change{Kind: ChangeUpsert, ID: "a", Geom: pl(Point{50, 50}, Point{55, 55})},
		Change{Kind: ChangeUpsert, ID: "b", Geom: pl(Point{60, 60}, Point{65, 65})},
	)
	if err != nil {
		t.Fatal(err)
	}
	if seq != ix.Snapshot().Seq() {
		t.Fatal("Apply seq does not match new snapshot")
	}
	all, err := ix.Query(Query{BBox: BBox{MinX: 40, MinY: 40, MaxX: 70, MaxY: 70}})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, all, "a", "b")

	if _, err := ix.Apply(Change{Kind: ChangeUpsert, ID: "x", Geom: pl(Point{0, 0})}, Change{Kind: ChangeUpsert, ID: "x", Geom: pl(Point{1, 1})}); !errors.Is(err, ErrDuplicateChange) {
		t.Fatalf("duplicate change error = %v", err)
	}
}

// Deleting removes every cell term, so no stale candidate can remain.
func TestDeleteClearsAllCellTerms(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("r", pl(Point{0, 0}, Point{25, 25})); err != nil {
		t.Fatal(err)
	}
	rec := mustRecord(t, ix, "r")
	if err := ix.Delete("r"); err != nil {
		t.Fatal(err)
	}
	if got := ix.Stats(); got.Records != 0 || got.CellTerms != 0 || got.LiveCells != 0 {
		t.Fatalf("stats after delete = %+v, want empty", got)
	}
	for _, c := range rec.Cells {
		if n := ix.CellTerms(c); n != 0 {
			t.Fatalf("cell %v still has %d terms", c, n)
		}
	}
}

// Query results are copies: mutating them must not corrupt the index.
func TestResultsAreCopies(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("r", pl(Point{0, 0}, Point{5, 5})); err != nil {
		t.Fatal(err)
	}
	res, err := ix.Query(Query{BBox: BBox{MinX: -1, MinY: -1, MaxX: 6, MaxY: 6}})
	if err != nil {
		t.Fatal(err)
	}
	res[0].Geom.Points[0] = Point{999, 999}
	got := mustRecord(t, ix, "r")
	if got.Geom.Points[0] != (Point{0, 0}) {
		t.Fatalf("index geometry was mutated through a result: %+v", got.Geom)
	}
}

// Concurrent writers and snapshot readers: every observed snapshot stays
// internally consistent (one version per record) under -race.
func TestConcurrentSnapshots(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("r", pl(Point{0, 0}, Point{9, 9})); err != nil {
		t.Fatal(err)
	}

	var writers, readers sync.WaitGroup
	stop := make(chan struct{})

	for w := 0; w < 4; w++ {
		writers.Add(1)
		go func(w int) {
			defer writers.Done()
			for i := 0; ; i++ {
				select {
				case <-stop:
					return
				default:
				}
				x := int64((w*1000 + i*3) % 400)
				if _, err := ix.Update("r", pl(Point{x, 0}, Point{x + 9, 9})); err != nil {
					t.Errorf("update: %v", err)
					return
				}
			}
		}(w)
	}
	for r := 0; r < 8; r++ {
		readers.Add(1)
		go func() {
			defer readers.Done()
			for i := 0; i < 200; i++ {
				snap := ix.Snapshot()
				res, err := snap.Query(Query{BBox: BBox{MinX: -10, MinY: -10, MaxX: 500, MaxY: 20}})
				if err != nil {
					t.Errorf("query: %v", err)
					return
				}
				if len(res) > 1 {
					t.Errorf("snapshot returned %d copies of one record", len(res))
					return
				}
				for _, item := range res {
					// The returned geometry must match the record stored in the
					// same snapshot: no mixed read across versions.
					rec, ok := snap.Record(item.ID)
					if !ok {
						t.Errorf("result id %q missing from its own snapshot", item.ID)
						return
					}
					if rec.Version != item.Version || rec.Bounds != item.Bounds {
						t.Errorf("result version/bounds disagree with snapshot: %d/%+v vs %d/%+v",
							item.Version, item.Bounds, rec.Version, rec.Bounds)
						return
					}
				}
			}
		}()
	}

	readers.Wait()
	close(stop)
	writers.Wait()
}
