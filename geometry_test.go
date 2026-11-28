package warmgrid

import (
	"errors"
	"testing"
)

func pl(pts ...Point) Polyline { return Polyline{Points: pts} }

func TestPolylineValidateAndBounds(t *testing.T) {
	if err := (Polyline{}).Validate(); !errors.Is(err, ErrInvalidGeometry) {
		t.Fatalf("empty polyline error = %v", err)
	}
	if err := pl(Point{MaxCoord + 1, 0}).Validate(); !errors.Is(err, ErrInvalidGeometry) {
		t.Fatalf("out-of-range vertex error = %v", err)
	}
	got := pl(Point{-3, 4}, Point{10, -2}, Point{0, 0}).Bounds()
	want := BBox{MinX: -3, MinY: -2, MaxX: 10, MaxY: 4}
	if got != want {
		t.Fatalf("Bounds = %+v, want %+v", got, want)
	}
}

// SegmentsIntersect must be exact for touching endpoints, collinear overlap,
// proper crossings and near-misses on the integer lattice.
func TestSegmentsIntersectExact(t *testing.T) {
	cases := []struct {
		name string
		a, b Segment
		want bool
	}{
		{"proper cross", Segment{A: Point{0, 0}, B: Point{10, 10}}, Segment{A: Point{0, 10}, B: Point{10, 0}}, true},
		{"touching endpoint", Segment{A: Point{0, 0}, B: Point{5, 5}}, Segment{A: Point{5, 5}, B: Point{10, 0}}, true},
		{"collinear overlap", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{5, 0}, B: Point{15, 0}}, true},
		{"collinear gap", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{11, 0}, B: Point{15, 0}}, false},
		{"parallel", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{0, 1}, B: Point{10, 1}}, false},
		{"near miss", Segment{A: Point{0, 0}, B: Point{10, 1}}, Segment{A: Point{0, 2}, B: Point{10, 2}}, false},
		{"t at endpoint", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{10, 0}, B: Point{10, 10}}, true},
		{"degenerate point on segment", Segment{A: Point{0, 0}, B: Point{10, 10}}, Segment{A: Point{5, 5}, B: Point{5, 5}}, true},
		{"degenerate point off segment", Segment{A: Point{0, 0}, B: Point{10, 10}}, Segment{A: Point{5, 6}, B: Point{5, 6}}, false},
	}
	for _, tc := range cases {
		if got := SegmentsIntersect(tc.a, tc.b); got != tc.want {
			t.Errorf("%s: SegmentsIntersect = %v, want %v", tc.name, got, tc.want)
		}
		if got := SegmentsIntersect(tc.b, tc.a); got != tc.want {
			t.Errorf("%s (reversed): SegmentsIntersect = %v, want %v", tc.name, got, tc.want)
		}
	}
}

// A route exactly on the box edge must be classified consistently: closed
// containment counts as inside; boundary-only contact with outside parts counts
// as touches; never as disjoint.
func TestPolylineRelationBoundaryCases(t *testing.T) {
	box := BBox{MinX: 0, MinY: 0, MaxX: 10, MaxY: 10}

	cases := []struct {
		name string
		p    Polyline
		want Relation
	}{
		{"deep inside", pl(Point{2, 2}, Point{8, 8}), RelationInside},
		{"corner point", pl(Point{0, 0}), RelationInside},
		{"vertex on edge inside box", pl(Point{10, 5}, Point{5, 5}), RelationInside},
		{"route along top edge, ends outside", pl(Point{-5, 10}, Point{15, 10}), RelationTouches},
		{"approach ending on edge", pl(Point{-10, 5}, Point{0, 5}), RelationTouches},
		{"single corner touch", pl(Point{-1, -1}, Point{0, 0}), RelationTouches},
		{"crosses interior", pl(Point{-5, 5}, Point{5, 5}), RelationCrosses},
		{"enters and exits", pl(Point{-5, -5}, Point{15, 15}), RelationCrosses},
		{"far away", pl(Point{20, 20}, Point{30, 30}), RelationDisjoint},
		{"parallel outside", pl(Point{-5, 11}, Point{15, 11}), RelationDisjoint},
	}
	for _, tc := range cases {
		if got := PolylineRelation(tc.p, box); got != tc.want {
			t.Errorf("%s: relation = %v, want %v", tc.name, got, tc.want)
		}
	}
}

// A zero-area box is a point query: the open interior is empty, so a hit can
// only ever be "touches" or "inside" (when the point is a vertex).
func TestPolylineRelationPointQuery(t *testing.T) {
	p := pl(Point{0, 0}, Point{10, 0})
	pt := func(x, y int64) BBox { return BBox{MinX: x, MinY: y, MaxX: x, MaxY: y} }

	if got := PolylineRelation(p, pt(5, 0)); got != RelationTouches {
		t.Errorf("point on middle of polyline: %v, want touches", got)
	}
	if got := PolylineRelation(pl(Point{0, 0}), pt(0, 0)); got != RelationInside {
		t.Errorf("single-vertex polyline at query point: %v, want inside", got)
	}
	if got := PolylineRelation(p, pt(5, 1)); got != RelationDisjoint {
		t.Errorf("point off polyline: %v, want disjoint", got)
	}
}

func TestPointDistance(t *testing.T) {
	s := Segment{A: Point{0, 0}, B: Point{10, 0}}
	if d := s.PointDistance(Point{5, 3}); d != 3 {
		t.Errorf("perpendicular distance = %v, want 3", d)
	}
	if d := s.PointDistance(Point{-3, 0}); d != 3 {
		t.Errorf("beyond-start distance = %v, want 3", d)
	}
	if d := s.PointDistance(Point{10, 4}); d != 4 {
		t.Errorf("beyond-end distance = %v, want 4", d)
	}
}

func TestIntersectsPolyline(t *testing.T) {
	a := pl(Point{0, 0}, Point{10, 10})
	b := pl(Point{0, 10}, Point{10, 0})
	c := pl(Point{0, 1}, Point{10, 11})
	if !a.Intersects(b) {
		t.Fatal("crossing polylines should intersect")
	}
	if a.Intersects(c) {
		t.Fatal("parallel polylines should not intersect")
	}
	if !a.IntersectsAny([]Polyline{c, b}) {
		t.Fatal("IntersectsAny should find b")
	}
}
