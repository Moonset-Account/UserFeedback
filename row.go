// Package exceptall implements a streaming, externally spilled EXCEPT ALL.
//
// The operator compares the nullable Text key and Num. Source is a provenance
// field and is deliberately ignored by equality. For every equal key group,
// max(leftCount-rightCount, 0) left rows remain. The remaining copies are the
// earliest left rows in original left-input order, and the final result is
// returned in that original order.
package exceptall

import "context"

// Row is one input row. Text is nullable; two NULL Text values are equivalent
// for set operations. Num participates in equality. Source is carried through
// to output but never participates in equality or ordering.
type Row struct {
	Text   *string `json:"text"`
	Num    int64   `json:"num"`
	Source int64   `json:"source"`
}

// OutputRow is a result row plus its 1-based original position in the left
// input. LeftLine is useful when verifying which physical duplicate survived.
type OutputRow struct {
	Row
	LeftLine int64 `json:"left_line"`
}

// RowReader is the streaming input interface. Implementations may read from a
// channel, scanner, database cursor, or generator. Next returns ok=false at
// clean EOF.
type RowReader interface {
	Next(ctx context.Context) (row Row, ok bool, err error)
}

// RowReaderFunc adapts a function to RowReader.
type RowReaderFunc func(ctx context.Context) (Row, bool, error)

// Next implements RowReader.
func (f RowReaderFunc) Next(ctx context.Context) (Row, bool, error) {
	return f(ctx)
}

// SliceReader streams an in-memory slice. It is primarily useful in tests and
// for small inputs.
type SliceReader struct {
	rows []Row
	pos  int
}

// NewSliceReader creates a reader over rows.
func NewSliceReader(rows []Row) *SliceReader {
	return &SliceReader{rows: rows}
}

// Next implements RowReader.
func (r *SliceReader) Next(ctx context.Context) (Row, bool, error) {
	if err := ctx.Err(); err != nil {
		return Row{}, false, err
	}
	if r.pos >= len(r.rows) {
		return Row{}, false, nil
	}
	row := r.rows[r.pos]
	r.pos++
	return row, true, nil
}

// GeneratorReader produces rows on demand without retaining the whole input.
type GeneratorReader struct {
	// Generate returns the next row. It returns ok=false at EOF.
	Generate func(ctx context.Context) (Row, bool, error)
}

// Next implements RowReader.
func (r GeneratorReader) Next(ctx context.Context) (Row, bool, error) {
	if r.Generate == nil {
		return Row{}, false, nil
	}
	return r.Generate(ctx)
}

func stringPtr(s string) *string {
	return &s
}
