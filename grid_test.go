package warmgrid

import (
	"errors"
	"testing"
)

func mustGrid(t *testing.T, size int64) Grid {
	t.Helper()
	g, err := NewGrid(size)
	if err != nil {
		t.Fatalf("NewGrid(%d): %v", size, err)
	}
	return g
}

func TestNewGridValidation(t *testing.T) {
	for _, size := range []int64{0, -1, -100, MaxCoord + 1} {
		if _, err := NewGrid(size); !errors.Is(err, ErrInvalidCellSize) {
			t.Fatalf("NewGrid(%d) error = %v, want ErrInvalidCellSize", size, err)
		}
	}
	if _, err := NewGrid(1); err != nil {
		t.Fatalf("NewGrid(1): %v", err)
	}
	if _, err := NewGrid(MaxCoord); err != nil {
		t.Fatalf("NewGrid(MaxCoord): %v", err)
	}
}

// A point exactly on a cell boundary belongs to the cell on the positive side:
// cells are half-open [min, max).
func TestCellOfHalfOpenBoundary(t *testing.T) {
	g := mustGrid(t, 10)
	cases := []struct {
		p    Point
		want CellKey
	}{
		{Point{0, 0}, CellKey{0, 0}},
		{Point{9, 9}, CellKey{0, 0}},
		{Point{10, 10}, CellKey{1, 1}}, // positive edge: not cell (0,0)
		{Point{-1, -1}, CellKey{-1, -1}},
		{Point{-10, -10}, CellKey{-1, -1}},
		{Point{-11, 0}, CellKey{-2, 0}},
		{Point{20, -10}, CellKey{2, -1}},
	}
	for _, tc := range cases {
		if got := g.CellOf(tc.p); got != tc.want {
			t.Errorf("CellOf(%v) = %v, want %v", tc.p, got, tc.want)
		}
	}
}

// CellOf must agree with floor division for negative coordinates.
func TestCellOfMatchesFloor(t *testing.T) {
	g := mustGrid(t, 7)
	for x := int64(-50); x <= 50; x++ {
		for y := int64(-50); y <= 50; y++ {
			got := g.CellOf(Point{x, y})
			// Verify membership via the half-open rectangle.
			minX := got.X * g.CellSize
			minY := got.Y * g.CellSize
			if !(minX <= x && x < minX+g.CellSize) {
				t.Fatalf("CellOf(%d,%d).X=%d does not contain x", x, y, got.X)
			}
			if !(minY <= y && y < minY+g.CellSize) {
				t.Fatalf("CellOf(%d,%d).Y=%d does not contain y", x, y, got.Y)
			}
		}
	}
}

// A box ending exactly on a grid line covers the cell on the positive side,
// because its edge points belong to that half-open cell.
func TestCellsForBoxExactEdge(t *testing.T) {
	g := mustGrid(t, 10)

	// Degenerate point box at the shared corner (10,10).
	cells, err := g.CellsForBox(BBox{MinX: 10, MinY: 10, MaxX: 10, MaxY: 10})
	if err != nil {
		t.Fatal(err)
	}
	want := []CellKey{{1, 1}}
	if got := FormatCells(cells); got != FormatCells(want) {
		t.Fatalf("point box cells = %s, want %s", got, FormatCells(want))
	}

	// Box [0,10]x[0,10] touches four cells: the edges are shared with the
	// positive-side neighbours.
	cells, err = g.CellsForBox(BBox{MinX: 0, MinY: 0, MaxX: 10, MaxY: 10})
	if err != nil {
		t.Fatal(err)
	}
	want = []CellKey{{0, 0}, {1, 0}, {0, 1}, {1, 1}}
	if got := FormatCells(cells); got != FormatCells(want) {
		t.Fatalf("edge box cells = %s, want %s", got, FormatCells(want))
	}

	// Box ending at 9 never reaches the neighbouring cell.
	cells, err = g.CellsForBox(BBox{MinX: 0, MinY: 0, MaxX: 9, MaxY: 9})
	if err != nil {
		t.Fatal(err)
	}
	want = []CellKey{{0, 0}}
	if got := FormatCells(cells); got != FormatCells(want) {
		t.Fatalf("interior box cells = %s, want %s", got, FormatCells(want))
	}
}

// CellsForBox must equal the union of CellOf over every lattice point in the
// box; this is the operational definition of "touched cells".
func TestCellsForBoxEqualsPointUnion(t *testing.T) {
	for _, size := range []int64{1, 3, 10} {
		g := mustGrid(t, size)
		for minX := int64(-12); minX <= 12; minX += 3 {
			for maxX := minX; maxX <= 12; maxX += 3 {
				box := BBox{MinX: minX, MinY: minX, MaxX: maxX, MaxY: maxX}
				cells, err := g.CellsForBox(box)
				if err != nil {
					t.Fatal(err)
				}
				got := make(map[CellKey]bool, len(cells))
				for _, c := range cells {
					got[c] = true
				}
				want := make(map[CellKey]bool)
				for x := minX; x <= maxX; x++ {
					for y := minX; y <= maxX; y++ {
						want[g.CellOf(Point{x, y})] = true
					}
				}
				if len(got) != len(want) {
					t.Fatalf("size %d box %+v: got %d cells want %d", size, box, len(got), len(want))
				}
				for c := range want {
					if !got[c] {
						t.Fatalf("size %d box %+v: missing cell %v", size, box, c)
					}
				}
			}
		}
	}
}

func TestCellsForBoxErrors(t *testing.T) {
	g := mustGrid(t, 10)
	if _, err := g.CellsForBox(BBox{MinX: 1, MinY: 0, MaxX: 0, MaxY: 1}); !errors.Is(err, ErrInvalidBBox) {
		t.Fatalf("inverted box error = %v", err)
	}
	// A tiny cell over the full coordinate range would cover 2e6 x 2e6 cells.
	if _, err := g.CellsForBox(BBox{MinX: -MaxCoord, MinY: -MaxCoord, MaxX: MaxCoord, MaxY: MaxCoord}); !errors.Is(err, ErrTooManyCells) {
		t.Fatalf("huge box error = %v", err)
	}
}

func TestCellBounds(t *testing.T) {
	g := mustGrid(t, 10)
	b, err := g.CellBounds(CellKey{-1, 2})
	if err != nil {
		t.Fatal(err)
	}
	want := BBox{MinX: -10, MinY: 20, MaxX: 0, MaxY: 30}
	if b != want {
		t.Fatalf("CellBounds = %+v, want %+v", b, want)
	}
	if _, err := g.CellBounds(CellKey{MaxCoord, 0}); !errors.Is(err, ErrInvalidBBox) {
		t.Fatalf("far cell error = %v", err)
	}
}
