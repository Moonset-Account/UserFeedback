package warmgrid

import (
	"fmt"
	"math"
	"math/big"
)

// Segment is a closed line segment between two points.
type Segment struct {
	A Point
	B Point
}

// Polyline is an ordered vertex chain. A single-vertex polyline is a point.
// Consecutive duplicate vertices are allowed and behave as degenerate segments.
type Polyline struct {
	Points []Point
}

// Validate checks that the polyline has at least one in-range vertex.
func (p Polyline) Validate() error {
	if len(p.Points) == 0 {
		return fmt.Errorf("%w: polyline has no vertices", ErrInvalidGeometry)
	}
	for i, pt := range p.Points {
		if !pt.Valid() {
			return fmt.Errorf("%w: vertex %d out of range: %+v", ErrInvalidGeometry, i, pt)
		}
	}
	return nil
}

// Bounds returns the closed bounding box of the polyline. An empty polyline
// returns the zero box.
func (p Polyline) Bounds() BBox {
	if len(p.Points) == 0 {
		return BBox{}
	}
	b := BBox{
		MinX: p.Points[0].X, MinY: p.Points[0].Y,
		MaxX: p.Points[0].X, MaxY: p.Points[0].Y,
	}
	for _, pt := range p.Points[1:] {
		if pt.X < b.MinX {
			b.MinX = pt.X
		}
		if pt.X > b.MaxX {
			b.MaxX = pt.X
		}
		if pt.Y < b.MinY {
			b.MinY = pt.Y
		}
		if pt.Y > b.MaxY {
			b.MaxY = pt.Y
		}
	}
	return b
}

// Segments returns the polyline segments. A one-point polyline yields a single
// degenerate (zero-length) segment.
func (p Polyline) Segments() []Segment {
	if len(p.Points) == 0 {
		return nil
	}
	if len(p.Points) == 1 {
		return []Segment{{A: p.Points[0], B: p.Points[0]}}
	}
	segs := make([]Segment, 0, len(p.Points)-1)
	for i := 1; i < len(p.Points); i++ {
		segs = append(segs, Segment{A: p.Points[i-1], B: p.Points[i]})
	}
	return segs
}

// Relation is the exact boundary relationship between a geometry and a closed
// query box. A point exactly on the box edge counts as inside the box, matching
// the half-open grid convention in which a cell edge belongs to the cell on its
// positive side.
type Relation uint8

const (
	// RelationDisjoint means no point of the geometry touches the box.
	RelationDisjoint Relation = iota
	// RelationTouches means the geometry meets the box, but every shared point
	// lies on the box boundary (for example a route running along an edge).
	RelationTouches
	// RelationCrosses means part of the geometry lies strictly inside the box
	// and another part lies outside it.
	RelationCrosses
	// RelationInside means every point of the geometry is within or on the
	// closed box.
	RelationInside
)

func (r Relation) String() string {
	switch r {
	case RelationDisjoint:
		return "disjoint"
	case RelationTouches:
		return "touches"
	case RelationCrosses:
		return "crosses"
	case RelationInside:
		return "inside"
	}
	return "unknown"
}

// PolylineRelation classifies p against a closed box exactly.
func PolylineRelation(p Polyline, box BBox) Relation {
	if len(p.Points) == 0 {
		return RelationDisjoint
	}
	allInside := true
	for _, pt := range p.Points {
		if !box.ContainsPoint(pt) {
			allInside = false
			break
		}
	}
	// A box is convex, so containing every vertex contains every segment.
	if allInside {
		return RelationInside
	}

	closedHit := false
	interiorHit := false
	for _, seg := range p.Segments() {
		if !closedHit && segmentIntersectsBBox(seg, box) {
			closedHit = true
		}
		if !interiorHit && segmentHitsOpenBox(seg, box) {
			interiorHit = true
		}
	}
	switch {
	case interiorHit:
		return RelationCrosses
	case closedHit:
		return RelationTouches
	default:
		return RelationDisjoint
	}
}

// segmentIntersectsBBox reports exactly whether a closed segment shares at
// least one point with the closed box.
func segmentIntersectsBBox(s Segment, box BBox) bool {
	if box.ContainsPoint(s.A) || box.ContainsPoint(s.B) {
		return true
	}
	// Both endpoints are outside the closed box, so any shared point would be
	// on the boundary. Touching a corner therefore counts.
	corners := [4]Point{
		{X: box.MinX, Y: box.MinY},
		{X: box.MaxX, Y: box.MinY},
		{X: box.MaxX, Y: box.MaxY},
		{X: box.MinX, Y: box.MaxY},
	}
	for i := 0; i < 4; i++ {
		edge := Segment{A: corners[i], B: corners[(i+1)%4]}
		if SegmentsIntersect(s, edge) {
			return true
		}
	}
	return false
}

// segmentHitsOpenBox reports whether some point of the segment lies strictly
// inside the box (open on all four sides).
//
// The segment is clipped against the closed box with exact rational arithmetic
// (Liang-Barsky with big.Rat). If the resulting parameter interval has positive
// length, its midpoint is either strictly inside the box, or it lies on a
// boundary line that the entire clipped piece follows (in which case the piece
// only touches the closed box and never enters the interior). The single
// remaining ambiguity, a midpoint exactly on a corner, cannot occur for a
// positive-length interval.
func segmentHitsOpenBox(s Segment, box BBox) bool {
	// Cheap exact rejection: a segment whose bounding box misses the closed
	// box cannot touch the interior.
	segBox := BBox{
		MinX: min64(s.A.X, s.B.X), MinY: min64(s.A.Y, s.B.Y),
		MaxX: max64(s.A.X, s.B.X), MaxY: max64(s.A.Y, s.B.Y),
	}
	if !segBox.Intersects(box) {
		return false
	}

	zero := new(big.Rat)
	one := big.NewRat(1, 1)

	ax := new(big.Rat).SetInt64(s.A.X)
	ay := new(big.Rat).SetInt64(s.A.Y)
	dx := new(big.Rat).SetInt64(s.B.X - s.A.X)
	dy := new(big.Rat).SetInt64(s.B.Y - s.A.Y)
	minX := new(big.Rat).SetInt64(box.MinX)
	maxX := new(big.Rat).SetInt64(box.MaxX)
	minY := new(big.Rat).SetInt64(box.MinY)
	maxY := new(big.Rat).SetInt64(box.MaxY)

	t0 := new(big.Rat).Set(zero)
	t1 := new(big.Rat).Set(one)

	// clip narrows [t0, t1] by the half-plane p*t <= q. It returns false when
	// the segment misses the corresponding boundary slab entirely.
	clip := func(p, q *big.Rat) bool {
		if p.Sign() == 0 {
			return q.Sign() >= 0
		}
		r := new(big.Rat).Quo(q, p)
		if p.Sign() < 0 {
			if r.Cmp(t1) > 0 {
				return false
			}
			if r.Cmp(t0) > 0 {
				t0.Set(r)
			}
		} else {
			if r.Cmp(t0) < 0 {
				return false
			}
			if r.Cmp(t1) < 0 {
				t1.Set(r)
			}
		}
		return true
	}

	// x(t) >= MinX  <=>  -dx*t <= ax-MinX
	if !clip(new(big.Rat).Neg(dx), new(big.Rat).Sub(ax, minX)) {
		return false
	}
	// x(t) <= MaxX  <=>   dx*t <= MaxX-ax
	if !clip(dx, new(big.Rat).Sub(maxX, ax)) {
		return false
	}
	// y(t) >= MinY  <=>  -dy*t <= ay-MinY
	if !clip(new(big.Rat).Neg(dy), new(big.Rat).Sub(ay, minY)) {
		return false
	}
	// y(t) <= MaxY  <=>   dy*t <= MaxY-ay
	if !clip(dy, new(big.Rat).Sub(maxY, ay)) {
		return false
	}
	if t0.Cmp(t1) >= 0 {
		return false
	}

	mid := new(big.Rat).Add(t0, t1)
	mid.Quo(mid, big.NewRat(2, 1))
	x := new(big.Rat).Add(ax, new(big.Rat).Mul(mid, dx))
	y := new(big.Rat).Add(ay, new(big.Rat).Mul(mid, dy))
	return minX.Cmp(x) < 0 && x.Cmp(maxX) < 0 &&
		minY.Cmp(y) < 0 && y.Cmp(maxY) < 0
}

// SegmentsIntersect reports whether two closed segments share at least one
// point, using exact integer orientation tests. Touching endpoints count.
func SegmentsIntersect(s1, s2 Segment) bool {
	d1 := orientation(s2.A, s2.B, s1.A)
	d2 := orientation(s2.A, s2.B, s1.B)
	d3 := orientation(s1.A, s1.B, s2.A)
	d4 := orientation(s1.A, s1.B, s2.B)

	if opposite(d1, d2) && opposite(d3, d4) {
		return true
	}
	if d1 == 0 && pointOnSegment(s2.A, s2.B, s1.A) {
		return true
	}
	if d2 == 0 && pointOnSegment(s2.A, s2.B, s1.B) {
		return true
	}
	if d3 == 0 && pointOnSegment(s1.A, s1.B, s2.A) {
		return true
	}
	if d4 == 0 && pointOnSegment(s1.A, s1.B, s2.B) {
		return true
	}
	return false
}

// Intersects reports whether two polylines share at least one point.
func (p Polyline) Intersects(other Polyline) bool {
	if len(p.Points) == 0 || len(other.Points) == 0 {
		return false
	}
	if !p.Bounds().Intersects(other.Bounds()) {
		return false
	}
	for _, a := range p.Segments() {
		for _, b := range other.Segments() {
			if SegmentsIntersect(a, b) {
				return true
			}
		}
	}
	return false
}

// IntersectsAny reports whether p intersects at least one of others, which is
// the disjoint-route acceptance test used by the demo and tests.
func (p Polyline) IntersectsAny(others []Polyline) bool {
	for _, o := range others {
		if p.Intersects(o) {
			return true
		}
	}
	return false
}

func orientation(a, b, c Point) int64 {
	v := (b.X-a.X)*(c.Y-a.Y) - (b.Y-a.Y)*(c.X-a.X)
	switch {
	case v > 0:
		return 1
	case v < 0:
		return -1
	default:
		return 0
	}
}

func opposite(a, b int64) bool {
	return (a > 0 && b < 0) || (a < 0 && b > 0)
}

// pointOnSegment assumes a, b, p are collinear and checks the bounding box.
func pointOnSegment(a, b, p Point) bool {
	return min64(a.X, b.X) <= p.X && p.X <= max64(a.X, b.X) &&
		min64(a.Y, b.Y) <= p.Y && p.Y <= max64(a.Y, b.Y)
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}

// PointDistance returns the Euclidean distance from p to the closed segment s.
func (s Segment) PointDistance(p Point) float64 {
	if s.A == s.B {
		return math.Hypot(float64(p.X-s.A.X), float64(p.Y-s.A.Y))
	}
	ax := float64(s.B.X - s.A.X)
	ay := float64(s.B.Y - s.A.Y)
	px := float64(p.X - s.A.X)
	py := float64(p.Y - s.A.Y)

	len2 := ax*ax + ay*ay
	t := (ax*px + ay*py) / len2
	if t <= 0 {
		return math.Hypot(px, py)
	}
	if t >= 1 {
		return math.Hypot(float64(p.X-s.B.X), float64(p.Y-s.B.Y))
	}
	// The perpendicular distance |cross|/|B-A| is numerically stable for a
	// point whose projection falls inside the segment.
	cross := ax*py - ay*px
	return math.Abs(cross) / math.Sqrt(len2)
}

// DistanceToPolyline returns the Euclidean distance from target to the nearest
// point of p, or +Inf for an empty polyline.
func (p Polyline) DistanceToPolyline(target Point) float64 {
	best := math.Inf(1)
	for _, seg := range p.Segments() {
		if d := seg.PointDistance(target); d < best {
			best = d
		}
	}
	return best
}

func copyPolyline(p Polyline) Polyline {
	pts := make([]Point, len(p.Points))
	copy(pts, p.Points)
	return Polyline{Points: pts}
}
