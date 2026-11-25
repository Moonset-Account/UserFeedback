package exceptall

import "context"

// Record is the physical representation stored in temporary segments.
//
// For ordinary sorted row segments, Line is the side-local or left-original
// sequence number. For decision segments produced internally, Keep is the
// number of earliest left rows that must survive for one business key.
type Record struct {
	Row
	Line int64 `json:"line"`
	Keep int64 `json:"keep,omitempty"`
}

// Output converts an ordinary row record to the public output value.
func (r Record) Output() OutputRow {
	return OutputRow{Row: r.Row, LeftLine: r.Line}
}

// SegmentReader is a sequential reader for a temporary segment.
type SegmentReader interface {
	// Next returns ok=false at EOF.
	Next(ctx context.Context) (record Record, ok bool, err error)
	Close() error
}

// Segment is a write-once, sequentially readable temporary segment.
//
// Implementations must support OpenReader after all required Append calls.
// OpenReader may be called more than once; each call returns an independent
// sequential reader. Release must make a best effort to return or remove all
// allocated resources and must be safe to call more than once.
type Segment interface {
	ID() string
	Append(ctx context.Context, record Record) error
	OpenReader(ctx context.Context) (SegmentReader, error)
	Release() error
}

// SegmentManager allocates temporary segments. Production code will normally
// use NewFileSegmentManager; tests may use NewMemorySegmentManager or a
// tracking wrapper.
type SegmentManager interface {
	CreateSegment(ctx context.Context) (Segment, error)
}

// SegmentStats is implemented by built-in managers and useful in cancellation
// and cleanup tests.
type SegmentStats interface {
	CreatedSegments() int
	ReleasedSegments() int
	ActiveSegments() int
}
