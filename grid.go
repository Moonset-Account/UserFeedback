package warmgrid

import (
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// MaxCoord is the largest absolute coordinate accepted for points and boxes.
// The bound keeps every orientation cross product used by the exact geometry
// predicates within int64: coordinate differences are at most 2*MaxCoord, so a
// single product is at most 4e18 and a difference of two products at most
// 8e18, below math.MaxInt64 (about 9.22e18).
const MaxCoord int64 = 1_000_000_000

// MaxCellsPerObject bounds how many grid cells one geometry's bounding box or
// one query box may cover. It protects the in-memory index from accidental
// quadratic blow-ups on tiny cell sizes.
const MaxCellsPerObject = 1 << 20

var (
	// ErrInvalidCellSize reports a cell size outside [1, MaxCoord].
	ErrInvalidCellSize = errors.New("warmgrid: invalid cell size")
	// ErrInvalidBBox reports an inverted, empty or out-of-range box.
	ErrInvalidBBox = errors.New("warmgrid: invalid bounding box")
	// ErrInvalidGeometry reports an empty or out-of-range polyline.
	ErrInvalidGeometry = errors.New("warmgrid: invalid geometry")
	// ErrTooManyCells reports a geometry or query covering too many cells.
	ErrTooManyCells = errors.New("warmgrid: too many cells")
	// ErrEmptyID reports an empty record id.
	ErrEmptyID = errors.New("warmgrid: empty record id")
	// ErrExists reports an Insert for an id already present in the snapshot.
	ErrExists = errors.New("warmgrid: record already exists")
	// ErrNotFound reports an Update/Delete for an unknown id.
	ErrNotFound = errors.New("warmgrid: record not found")
	// ErrInvalidFilter reports an unknown query filter.
	ErrInvalidFilter = errors.New("warmgrid: invalid filter")
	// ErrDuplicateChange reports two changes for the same id in one Apply.
	ErrDuplicateChange = errors.New("warmgrid: duplicate change for record")
)

// Point is a position on the integer coordinate plane.
type Point struct {
	X int64
	Y int64
}

// Valid reports whether the point lies inside the supported coordinate range.
func (p Point) Valid() bool {
	return p.X >= -MaxCoord && p.X <= MaxCoord &&
		p.Y >= -MaxCoord && p.Y <= MaxCoord
}

// BBox is a closed axis-aligned bounding box; all four edges belong to it.
type BBox struct {
	MinX, MinY, MaxX, MaxY int64
}

// Validate checks ordering and the supported coordinate range.
func (b BBox) Validate() error {
	if b.MinX > b.MaxX || b.MinY > b.MaxY {
		return fmt.Errorf("%w: inverted bounds %+v", ErrInvalidBBox, b)
	}
	if b.MinX < -MaxCoord || b.MaxX > MaxCoord ||
		b.MinY < -MaxCoord || b.MaxY > MaxCoord {
		return fmt.Errorf("%w: coordinates out of range %+v", ErrInvalidBBox, b)
	}
	return nil
}

// Intersects reports whether two closed boxes share at least one point.
func (b BBox) Intersects(other BBox) bool {
	return b.MinX <= other.MaxX && other.MinX <= b.MaxX &&
		b.MinY <= other.MaxY && other.MinY <= b.MaxY
}

// ContainsPoint reports whether p is inside or on the edge of the closed box.
func (b BBox) ContainsPoint(p Point) bool {
	return b.MinX <= p.X && p.X <= b.MaxX &&
		b.MinY <= p.Y && p.Y <= b.MaxY
}

// ContainsBox reports whether other is fully inside or on the edge of b.
func (b BBox) ContainsBox(other BBox) bool {
	return b.MinX <= other.MinX && other.MaxX <= b.MaxX &&
		b.MinY <= other.MinY && other.MaxY <= b.MaxY
}

// CellKey identifies a grid cell. Cell (x, y) covers the half-open rectangle
// [x*size, (x+1)*size) x [y*size, (y+1)*size).
type CellKey struct {
	X int64
	Y int64
}

// Grid is a regular half-open partition of the coordinate plane.
type Grid struct {
	CellSize int64
}

// NewGrid validates and returns a grid.
func NewGrid(cellSize int64) (Grid, error) {
	if cellSize < 1 || cellSize > MaxCoord {
		return Grid{}, fmt.Errorf("%w: got %d, want 1..%d", ErrInvalidCellSize, cellSize, MaxCoord)
	}
	return Grid{CellSize: cellSize}, nil
}

// CellOf returns the unique cell containing p. A point exactly on a cell edge
// belongs to the cell on the positive side because cells are half-open on their
// maximum edges: CellOf(Point{10, 0}) on a size 10 grid is cell (1, 0), not
// cell (0, 0).
func (g Grid) CellOf(p Point) CellKey {
	return CellKey{X: floorDiv(p.X, g.CellSize), Y: floorDiv(p.Y, g.CellSize)}
}

// CellBounds returns the rectangle represented by a cell key. The rectangle is
// half-open [MinX, MaxX) x [MinY, MaxY); BBox is only used as a convenient
// four-number carrier. Cells beyond the coordinate limit are rejected.
func (g Grid) CellBounds(c CellKey) (BBox, error) {
	limit := MaxCoord/g.CellSize + 2
	if c.X < -limit || c.X > limit || c.Y < -limit || c.Y > limit {
		return BBox{}, fmt.Errorf("%w: cell %+v outside supported range", ErrInvalidBBox, c)
	}
	minX := c.X * g.CellSize
	minY := c.Y * g.CellSize
	return BBox{MinX: minX, MinY: minY, MaxX: minX + g.CellSize, MaxY: minY + g.CellSize}, nil
}

// CellsForBox returns every cell touched by the closed box, in row-major order
// (Y ascending, then X ascending).
//
// Because cells are half-open on their maximum edges, a box whose maximum lies
// exactly on a cell edge also covers the cell on the positive side: the edge
// point itself belongs to that half-open cell. For example on a size 10 grid,
// the point box at (10, 10) covers cell (1, 1) only, and the box [0,10]x[0,10]
// covers all four of cells (0,0), (1,0), (0,1), (1,1).
func (g Grid) CellsForBox(b BBox) ([]CellKey, error) {
	if err := b.Validate(); err != nil {
		return nil, err
	}
	minCX := floorDiv(b.MinX, g.CellSize)
	maxCX := floorDiv(b.MaxX, g.CellSize)
	minCY := floorDiv(b.MinY, g.CellSize)
	maxCY := floorDiv(b.MaxY, g.CellSize)

	nx := maxCX - minCX + 1
	ny := maxCY - minCY + 1
	if nx <= 0 || ny <= 0 || nx > MaxCellsPerObject || ny > MaxCellsPerObject ||
		nx*ny > MaxCellsPerObject {
		return nil, fmt.Errorf("%w: %dx%d cells exceeds %d", ErrTooManyCells, nx, ny, MaxCellsPerObject)
	}

	cells := make([]CellKey, 0, int(nx*ny))
	for cy := minCY; cy <= maxCY; cy++ {
		for cx := minCX; cx <= maxCX; cx++ {
			cells = append(cells, CellKey{X: cx, Y: cy})
		}
	}
	return cells, nil
}

// floorDiv is mathematical floor division for a positive divisor.
func floorDiv(a, b int64) int64 {
	q := a / b
	if a%b != 0 && a < 0 {
		q--
	}
	return q
}

// FormatCells renders cell keys as "(x,y);(x,y);..." in the given order, for
// deterministic diagnostics and test assertions.
func FormatCells(cells []CellKey) string {
	parts := make([]string, len(cells))
	for i, c := range cells {
		parts[i] = "(" + strconv.FormatInt(c.X, 10) + "," + strconv.FormatInt(c.Y, 10) + ")"
	}
	return strings.Join(parts, ";")
}
