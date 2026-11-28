package warmgrid

import "testing"

func TestSegmentContactKind(t *testing.T) {
	cases := []struct {
		name string
		a, b Segment
		want SegmentContact
	}{
		{"disjoint", Segment{A: Point{0, 0}, B: Point{1, 1}}, Segment{A: Point{5, 5}, B: Point{6, 6}}, ContactNone},
		{"shared corner", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{10, 0}, B: Point{10, 10}}, ContactEndpoint},
		{"shared tail", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{10, 0}, B: Point{0, 0}}, ContactCross},
		{"proper cross", Segment{A: Point{0, 0}, B: Point{10, 10}}, Segment{A: Point{0, 10}, B: Point{10, 0}}, ContactCross},
		{"T junction", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{5, 5}, B: Point{5, 0}}, ContactCross},
		{"collinear overlap", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{5, 0}, B: Point{15, 0}}, ContactCross},
		{"collinear touching tip", Segment{A: Point{0, 0}, B: Point{5, 0}}, Segment{A: Point{5, 0}, B: Point{10, 0}}, ContactEndpoint},
		{"same segment", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{0, 0}, B: Point{10, 0}}, ContactCross},
		{"point at endpoint", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{0, 0}, B: Point{0, 0}}, ContactEndpoint},
		{"point in middle", Segment{A: Point{0, 0}, B: Point{10, 0}}, Segment{A: Point{5, 0}, B: Point{5, 0}}, ContactCross},
	}
	for _, tc := range cases {
		if got := SegmentContactKind(tc.a, tc.b); got != tc.want {
			t.Errorf("%s: contact = %v, want %v", tc.name, got, tc.want)
		}
		if got := SegmentContactKind(tc.b, tc.a); got != tc.want {
			t.Errorf("%s (reversed): contact = %v, want %v", tc.name, got, tc.want)
		}
	}
}
