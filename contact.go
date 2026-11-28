package warmgrid

// SegmentContact classifies the contact between two closed segments. It
// distinguishes a junction (the two segments meet at a point that is an endpoint
// of both) from a genuine conflict (a proper crossing, a T-junction where one
// endpoint lies in the other's interior, or a collinear overlap of positive
// length).
type SegmentContact uint8

const (
	// ContactNone means the segments share no point.
	ContactNone SegmentContact = iota
	// ContactEndpoint means they share exactly one point that is an endpoint of
	// both, i.e. an ordinary junction.
	ContactEndpoint
	// ContactCross means they share interior geometry: a proper crossing, a
	// T-junction, or a collinear overlap.
	ContactCross
)

func (c SegmentContact) String() string {
	switch c {
	case ContactNone:
		return "none"
	case ContactEndpoint:
		return "endpoint"
	case ContactCross:
		return "cross"
	}
	return "unknown"
}

// SegmentContactKind classifies the contact between a and b exactly, using
// integer arithmetic only.
func SegmentContactKind(a, b Segment) SegmentContact {
	if !SegmentsIntersect(a, b) {
		return ContactNone
	}

	d1 := cross(b.A, b.B, a.A)
	d2 := cross(b.A, b.B, a.B)
	d3 := cross(a.A, a.B, b.A)
	d4 := cross(a.A, a.B, b.B)

	if opposite64(d1, d2) && opposite64(d3, d4) {
		return ContactCross
	}

	// Collinear with both segments non-degenerate: a positive-length projected
	// overlap is shared road, not a junction.
	if d1 == 0 && d2 == 0 && d3 == 0 && d4 == 0 && a.A != a.B && b.A != b.B {
		ax := a.B.X - a.A.X
		ay := a.B.Y - a.A.Y
		var aLo, aHi, bLo, bHi int64
		if abs64(ax) >= abs64(ay) {
			aLo, aHi = minMax64(a.A.X, a.B.X)
			bLo, bHi = minMax64(b.A.X, b.B.X)
		} else {
			aLo, aHi = minMax64(a.A.Y, a.B.Y)
			bLo, bHi = minMax64(b.A.Y, b.B.Y)
		}
		if max64(aLo, bLo) < min64(aHi, bHi) {
			return ContactCross
		}
	}

	// The segments meet in a single point. Any endpoint of one that lies on the
	// other is a witness. If every witness is an endpoint of both segments the
	// contact is a plain junction; otherwise it is a crossing.
	var witnesses []Point
	addWitness := func(p Point) {
		for _, existing := range witnesses {
			if existing == p {
				return
			}
		}
		witnesses = append(witnesses, p)
	}
	if collinearPointOnSegment(b.A, b.B, a.A) {
		addWitness(a.A)
	}
	if collinearPointOnSegment(b.A, b.B, a.B) {
		addWitness(a.B)
	}
	if collinearPointOnSegment(a.A, a.B, b.A) {
		addWitness(b.A)
	}
	if collinearPointOnSegment(a.A, a.B, b.B) {
		addWitness(b.B)
	}
	if len(witnesses) == 0 {
		// Should be unreachable for intersecting segments; treat defensively.
		return ContactCross
	}
	for _, p := range witnesses {
		if !isEndpoint(p, a) || !isEndpoint(p, b) {
			return ContactCross
		}
	}
	return ContactEndpoint
}

// Crosses reports whether the two polylines share interior geometry: a proper
// crossing, a T-junction, or a collinear overlap. Routes meeting only at shared
// endpoints (a junction) do not cross.
func (p Polyline) Crosses(other Polyline) bool {
	if len(p.Points) == 0 || len(other.Points) == 0 {
		return false
	}
	if !p.Bounds().Intersects(other.Bounds()) {
		return false
	}
	for _, a := range p.Segments() {
		for _, b := range other.Segments() {
			if SegmentContactKind(a, b) == ContactCross {
				return true
			}
		}
	}
	return false
}

// CrossesAny reports whether p crosses at least one of others.
func (p Polyline) CrossesAny(others []Polyline) bool {
	for _, o := range others {
		if p.Crosses(o) {
			return true
		}
	}
	return false
}

func cross(a, b, c Point) int64 {
	return (b.X-a.X)*(c.Y-a.Y) - (b.Y-a.Y)*(c.X-a.X)
}

// collinearPointOnSegment reports whether p lies on the *line* through a and b
// and, if so, within the closed segment between them.
func collinearPointOnSegment(a, b, p Point) bool {
	if cross(a, b, p) != 0 {
		return false
	}
	return min64(a.X, b.X) <= p.X && p.X <= max64(a.X, b.X) &&
		min64(a.Y, b.Y) <= p.Y && p.Y <= max64(a.Y, b.Y)
}

func opposite64(a, b int64) bool {
	return (a > 0 && b < 0) || (a < 0 && b > 0)
}

func isEndpoint(p Point, s Segment) bool {
	return p == s.A || p == s.B
}

func abs64(v int64) int64 {
	if v < 0 {
		return -v
	}
	return v
}

func minMax64(a, b int64) (int64, int64) {
	if a <= b {
		return a, b
	}
	return b, a
}
