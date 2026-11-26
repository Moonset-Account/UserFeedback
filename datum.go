package sharedscan

import (
	"fmt"
	"math"
	"strings"
)

// Datum is one nullable scalar value.
//
// When Null is true, Value is ignored. The built-in comparison rule supports
// int64, uint64, float64, string and bool values; custom Rule.Equal functions
// may support additional scalar types.
type Datum struct {
	Null  bool
	Value any
}

// NullDatum returns a SQL NULL datum.
func NullDatum() Datum { return Datum{Null: true} }

// Int returns a non-NULL int64 datum.
func Int(v int64) Datum { return Datum{Value: v} }

// Uint returns a non-NULL uint64 datum.
func Uint(v uint64) Datum { return Datum{Value: v} }

// Float returns a non-NULL float64 datum.
func Float(v float64) Datum { return Datum{Value: v} }

// StringDatum returns a non-NULL string datum.
func StringDatum(v string) Datum { return Datum{Value: v} }

// Bool returns a non-NULL bool datum.
func Bool(v bool) Datum { return Datum{Value: v} }

// NewDatum converts nil to NULL and any other value to a non-NULL Datum.
func NewDatum(v any) Datum {
	if v == nil {
		return NullDatum()
	}
	return Datum{Value: v}
}

func (d Datum) isNull() bool {
	return d.Null || d.Value == nil
}

// Row is a composite row/parameter value. A nil Datum.Value is treated as
// NULL by the comparison layer even if Null was not explicitly set.
type Row []Datum

// RowOf builds a row from ordinary Go values; untyped nil entries become SQL
// NULL.
func RowOf(values ...any) Row {
	row := make(Row, len(values))
	for i, v := range values {
		row[i] = NewDatum(v)
	}
	return row
}

// NullPositions returns the zero-based indexes of NULL fields.
func (r Row) NullPositions() []int {
	var positions []int
	for i, d := range r {
		if d.isNull() {
			positions = append(positions, i)
		}
	}
	return positions
}

// NullBitmap returns one character per field: '1' for NULL and '0' otherwise.
// It makes different composite null positions easy to print and compare.
func (r Row) NullBitmap() string {
	var b strings.Builder
	b.Grow(len(r))
	for _, d := range r {
		if d.isNull() {
			b.WriteByte('1')
		} else {
			b.WriteByte('0')
		}
	}
	return b.String()
}

func (r Row) validate(fieldName string) error {
	for i, d := range r {
		if d.Null {
			continue
		}
		if d.Value == nil {
			return fmt.Errorf("sharedscan: %s[%d] has nil Value but Null=false", fieldName, i)
		}
	}
	return nil
}

// appendRowKey writes a stable, type-aware physical key. Unlike SQL
// comparison, two NULL fields at the same position hash to the same physical
// dependency group; semantic rules are still applied separately afterwards.
func appendRowKey(b *strings.Builder, r Row) {
	fmt.Fprintf(b, "len=%d;", len(r))
	for _, d := range r {
		if d.isNull() {
			b.WriteString("N;")
			continue
		}
		switch v := d.Value.(type) {
		case int:
			fmt.Fprintf(b, "i%d;", int64(v))
		case int64:
			fmt.Fprintf(b, "i%d;", v)
		case uint64:
			fmt.Fprintf(b, "u%d;", v)
		case float64:
			fmt.Fprintf(b, "f%016x;", math.Float64bits(v))
		case bool:
			if v {
				b.WriteString("b1;")
			} else {
				b.WriteString("b0;")
			}
		case string:
			fmt.Fprintf(b, "s%d:%s;", len(v), v)
		default:
			fmt.Fprintf(b, "x:%T:%#v;", d.Value, d.Value)
		}
	}
}

// PhysicalKey returns a stable key including arity, dynamic type, value and
// the NULL bitmap.
func (r Row) PhysicalKey() string {
	var b strings.Builder
	appendRowKey(&b, r)
	return b.String()
}
