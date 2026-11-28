package warmgrid

import (
	"fmt"
	"math/rand"
	"sort"
	"testing"
)

// bruteForceQuery is the reference implementation: a full linear scan using the
// same exact relation predicate, with no grid involvement at all.
func bruteForceQuery(snap *Snapshot, q Query) []Result {
	var out []Result
	for _, rec := range snap.All() {
		if !rec.Bounds.Intersects(q.BBox) {
			continue
		}
		rel := PolylineRelation(rec.Geom, q.BBox)
		if !q.Filter.accepts(rel) {
			continue
		}
		out = append(out, Result{
			ID:       rec.ID,
			Version:  rec.Version,
			Geom:     rec.Geom,
			Bounds:   rec.Bounds,
			Relation: rel,
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func resultFingerprint(results []Result) string {
	s := ""
	for _, r := range results {
		s += fmt.Sprintf("%s@%d:%s:%+v;", r.ID, r.Version, r.Relation, r.Bounds)
	}
	return s
}

// Differential test: for random small-coordinate road networks, the grid index
// must return exactly the linear scan's answer for every filter. Negative
// coordinates and cell-boundary-aligned coordinates are included on purpose.
func TestQueryMatchesLinearScan(t *testing.T) {
	const cell = 4
	g := mustGrid(t, cell)
	ix := NewIndex(g)
	rng := rand.New(rand.NewSource(20261128))

	randomGeom := func() Polyline {
		n := 1 + rng.Intn(4)
		pts := make([]Point, n)
		for i := range pts {
			// Coordinate range -8..8 with size 4 hits every cell edge case.
			pts[i] = Point{X: int64(rng.Intn(17) - 8), Y: int64(rng.Intn(17) - 8)}
		}
		return Polyline{Points: pts}
	}

	for i := 0; i < 120; i++ {
		id := RecordID(fmt.Sprintf("r%03d", i))
		if _, err := ix.Insert(id, randomGeom()); err != nil {
			t.Fatalf("insert %s: %v", id, err)
		}
	}
	// Redraw a third of them so old/new cell sets differ.
	for i := 0; i < 120; i += 3 {
		id := RecordID(fmt.Sprintf("r%03d", i))
		if _, err := ix.Update(id, randomGeom()); err != nil {
			t.Fatalf("update %s: %v", id, err)
		}
	}

	snap := ix.Snapshot()
	filters := []Filter{FilterAny, FilterIntersects, FilterInside, FilterTouches, FilterCrosses}
	for trial := 0; trial < 200; trial++ {
		x1 := int64(rng.Intn(17) - 8)
		x2 := int64(rng.Intn(17) - 8)
		y1 := int64(rng.Intn(17) - 8)
		y2 := int64(rng.Intn(17) - 8)
		if x1 > x2 {
			x1, x2 = x2, x1
		}
		if y1 > y2 {
			y1, y2 = y2, y1
		}
		box := BBox{MinX: x1, MinY: y1, MaxX: x2, MaxY: y2}
		for _, f := range filters {
			q := Query{BBox: box, Filter: f}
			got, err := snap.Query(q)
			if err != nil {
				t.Fatalf("query %+v: %v", q, err)
			}
			want := bruteForceQuery(snap, q)
			if g, w := resultFingerprint(got), resultFingerprint(want); g != w {
				t.Fatalf("trial %d box %+v filter %v:\n got  %s\n want %s", trial, box, f, g, w)
			}
		}
	}
}

// A route ending exactly on a cell line must still be found by a query whose
// bounds only reach that line: this is the half-open convention applied to both
// indexing and query.
func TestHalfOpenEdgeFindableFromBothSides(t *testing.T) {
	ix := newTestIndex(t, 10)
	// The endpoint (10,10) lies exactly on the lattice corner shared by four
	// cells; it belongs to cell (1,1).
	if _, err := ix.Insert("corner", pl(Point{10, 10})); err != nil {
		t.Fatal(err)
	}
	rec := mustRecord(t, ix, "corner")
	if got := FormatCells(rec.Cells); got != "(1,1)" {
		t.Fatalf("corner record cells = %s, want (1,1)", got)
	}

	queries := []BBox{
		{MinX: 10, MinY: 10, MaxX: 10, MaxY: 10}, // exact lattice point
		{MinX: 0, MinY: 0, MaxX: 10, MaxY: 10},   // reaches the line
		{MinX: 10, MinY: 10, MaxX: 20, MaxY: 20}, // starts on the line
		{MinX: 9, MinY: 9, MaxX: 10, MaxY: 10},
	}
	for _, box := range queries {
		res, err := ix.Query(Query{BBox: box})
		if err != nil {
			t.Fatal(err)
		}
		assertIDs(t, res, "corner")
	}

	// A box strictly inside the neighbouring cell must not see it.
	res, err := ix.Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: 9, MaxY: 9}})
	if err != nil {
		t.Fatal(err)
	}
	if len(res) != 0 {
		t.Fatalf("interior query saw corner record: %v", ids(res))
	}
}

// A route crossing a cell boundary must be indexed in both cells and must be
// found from either side without duplication.
func TestRouteCrossingCellLine(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("road", pl(Point{5, 5}, Point{15, 5})); err != nil {
		t.Fatal(err)
	}
	rec := mustRecord(t, ix, "road")
	if got := FormatCells(rec.Cells); got != "(0,0);(1,0)" {
		t.Fatalf("crossing route cells = %s, want (0,0);(1,0)", got)
	}
	for _, box := range []BBox{
		{MinX: 0, MinY: 0, MaxX: 9, MaxY: 9},
		{MinX: 10, MinY: 0, MaxX: 19, MaxY: 9},
		{MinX: 0, MinY: 0, MaxX: 19, MaxY: 9},
	} {
		res, err := ix.Query(Query{BBox: box})
		if err != nil {
			t.Fatal(err)
		}
		assertIDs(t, res, "road")
		if len(res) != 1 {
			t.Fatalf("box %+v returned %d results", box, len(res))
		}
	}
}

// A route lying exactly along a cell line belongs to the cell on the positive
// side, and a query whose bounds only reach that line must find it. This is the
// same convention applied to indexing and to queries.
func TestRouteAlongCellLine(t *testing.T) {
	ix := newTestIndex(t, 10)
	if _, err := ix.Insert("along", pl(Point{10, 0}, Point{10, 9})); err != nil {
		t.Fatal(err)
	}
	rec := mustRecord(t, ix, "along")
	if got := FormatCells(rec.Cells); got != "(1,0)" {
		t.Fatalf("line-aligned route cells = %s, want (1,0)", got)
	}
	// Query box in the negative-side cell whose max edge is exactly x=10.
	res, err := ix.Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: 10, MaxY: 9}})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, res, "along")
	if len(res) != 1 {
		t.Fatalf("got %d results", len(res))
	}
	// Because the route lies on the closed box edge, it counts as inside that
	// box.
	if res[0].Relation != RelationInside {
		t.Fatalf("relation = %v, want inside (closed box includes its edge)", res[0].Relation)
	}
	// A box that stops short of the line must not see it.
	short, err := ix.Query(Query{BBox: BBox{MinX: 0, MinY: 0, MaxX: 9, MaxY: 9}})
	if err != nil {
		t.Fatal(err)
	}
	if len(short) != 0 {
		t.Fatalf("short query saw line-aligned route: %v", ids(short))
	}
}

// Negative coordinates use floor division; a record straddling the origin must
// be indexed in both negative and positive cells.
func TestNegativeCoordinates(t *testing.T) {
	ix := newTestIndex(t, 10)
	// From (-5,5) to (5,5): straddles x=0 but not y=0.
	if _, err := ix.Insert("straddle", pl(Point{-5, 5}, Point{5, 5})); err != nil {
		t.Fatal(err)
	}
	rec := mustRecord(t, ix, "straddle")
	if got := FormatCells(rec.Cells); got != "(-1,0);(0,0)" {
		t.Fatalf("straddling route cells = %s, want (-1,0);(0,0)", got)
	}
	res, err := ix.Query(Query{BBox: BBox{MinX: -9, MinY: 0, MaxX: -1, MaxY: 9}})
	if err != nil {
		t.Fatal(err)
	}
	assertIDs(t, res, "straddle")
}

// The headline scenario: several candidate routes proposed inside the very same
// bounding box; the index must support repeatedly selecting a route that does
// not cross any already-accepted route, using exact geometry rather than boxes.
func TestChooseDisjointRoutesInSameBBox(t *testing.T) {
	ix := newTestIndex(t, 10)
	shared := BBox{MinX: 0, MinY: 0, MaxX: 40, MaxY: 40}
	candidates := []struct {
		id   RecordID
		geom Polyline
	}{
		{"north", pl(Point{0, 40}, Point{40, 40})},
		{"south", pl(Point{0, 0}, Point{40, 0})},
		{"west", pl(Point{0, 0}, Point{0, 40})},
		{"east", pl(Point{40, 0}, Point{40, 40})},
		{"diagA", pl(Point{0, 0}, Point{40, 40})},
		{"diagB", pl(Point{0, 40}, Point{40, 0})},
		{"midH", pl(Point{0, 20}, Point{40, 20})},
		{"shortInside", pl(Point{10, 30}, Point{20, 30})},
	}
	for _, c := range candidates {
		if _, err := ix.Insert(c.id, c.geom); err != nil {
			t.Fatal(err)
		}
		// Every candidate sits inside the same shared bounding box.
		rec := mustRecord(t, ix, c.id)
		if !shared.ContainsBox(rec.Bounds) {
			t.Fatalf("%s bbox %+v not inside shared bbox", c.id, rec.Bounds)
		}
	}

	// Select all candidates in the shared box, then greedily keep a maximal
	// subset whose routes never share interior geometry. Corner-to-corner
	// contact is a junction, not a crossing, so the four border routes are all
	// compatible even though they meet at the corners.
	res, err := ix.Query(Query{BBox: shared, Filter: FilterInside})
	if err != nil {
		t.Fatal(err)
	}
	if len(res) != len(candidates) {
		t.Fatalf("got %d candidates, want %d", len(res), len(candidates))
	}
	byID := make(map[RecordID]Polyline, len(res))
	for _, r := range res {
		byID[r.ID] = r.Geom
	}

	order := []RecordID{"north", "south", "west", "east", "diagA", "diagB", "midH", "shortInside"}
	var accepted []Polyline
	var acceptedIDs []RecordID
	for _, id := range order {
		g := byID[id]
		if !g.CrossesAny(accepted) {
			accepted = append(accepted, g)
			acceptedIDs = append(acceptedIDs, id)
		}
	}
	wantAccepted := []RecordID{"north", "south", "west", "east", "diagA", "shortInside"}
	if len(acceptedIDs) != len(wantAccepted) {
		t.Fatalf("accepted = %v, want %v", acceptedIDs, wantAccepted)
	}
	for i := range wantAccepted {
		if acceptedIDs[i] != wantAccepted[i] {
			t.Fatalf("accepted = %v, want %v", acceptedIDs, wantAccepted)
		}
	}

	// The two diagonals properly cross at (20,20): diagB is rejected once
	// diagA is accepted.
	if !byID["diagA"].Crosses(byID["diagB"]) {
		t.Fatal("diagA and diagB must cross at the centre")
	}
	// midH ends in the interior of west/east: a T-junction counts as a crossing,
	// so it is rejected.
	if !byID["midH"].Crosses(byID["west"]) {
		t.Fatal("midH must cross west at a T-junction")
	}
	// Plain Intersects still reports the corner junction, which the crossing
	// predicate deliberately excludes.
	if !pl(Point{0, 40}, Point{40, 40}).Intersects(pl(Point{0, 0}, Point{0, 40})) {
		t.Fatal("corner-sharing routes must still intersect")
	}
	if pl(Point{0, 40}, Point{40, 40}).Crosses(pl(Point{0, 0}, Point{0, 40})) {
		t.Fatal("corner junction must not count as a crossing")
	}

	// The same selection must be reproducible from a later snapshot after an
	// unrelated redraw: newly inserted geometry is added, accepted set intact.
	beforeVersion := mustRecord(t, ix, "north").Version
	if _, err := ix.Update("shortInside", pl(Point{30, 30}, Point{35, 35})); err != nil {
		t.Fatal(err)
	}
	after, err := ix.Query(Query{BBox: shared, Filter: FilterInside})
	if err != nil {
		t.Fatal(err)
	}
	if len(after) != len(candidates) {
		t.Fatalf("after redraw got %d candidates, want %d", len(after), len(candidates))
	}
	if v := mustRecord(t, ix, "north").Version; v != beforeVersion {
		t.Fatalf("unrelated redraw changed north's version: %d -> %d", beforeVersion, v)
	}
}
