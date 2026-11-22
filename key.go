package txstore

import (
	"strconv"
	"strings"
)

// Value is one nullable text column of a composite key. Null models SQL
// NULL: a key containing at least one NULL column is exempt from the
// unique constraint. Text is ignored when Null is true.
type Value struct {
	Null bool
	Text string
}

// Str returns a non-NULL text value.
func Str(text string) Value { return Value{Text: text} }

// Null returns a NULL value.
func Null() Value { return Value{Null: true} }

// Equal reports whether two values are equal. Two NULLs are equal for key
// comparison purposes; a NULL never equals a non-NULL value.
func (v Value) Equal(other Value) bool {
	if v.Null || other.Null {
		return v.Null == other.Null
	}
	return v.Text == other.Text
}

// String renders a value for logs and tests. NULL is rendered literally and
// non-NULL text is Go-quoted so the representation is unambiguous.
func (v Value) String() string {
	if v.Null {
		return "NULL"
	}
	return strconv.Quote(v.Text)
}

// Key is an ordered composite key. Keys of different arity are distinct.
// If any column is NULL, the key does not participate in uniqueness checks.
type Key []Value

// HasNull reports whether any column of the key is NULL.
func (k Key) HasNull() bool {
	for _, v := range k {
		if v.Null {
			return true
		}
	}
	return false
}

// Equal reports whether two keys contain the same values in the same order.
func (k Key) Equal(other Key) bool {
	if len(k) != len(other) {
		return false
	}
	for i := range k {
		if !k[i].Equal(other[i]) {
			return false
		}
	}
	return true
}

// String renders the key in a canonical, unambiguous form such as
// ("tenant","alice") or ("tenant",NULL). It is suitable for sorted key-set
// comparisons in tests and diagnostics.
func (k Key) String() string {
	var b strings.Builder
	b.WriteByte('(')
	for i, v := range k {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString(v.String())
	}
	b.WriteByte(')')
	return b.String()
}

// cloneKey normalizes NULL values and copies the slice so callers cannot
// mutate stored keys through the slice they passed in.
func cloneKey(k Key) Key {
	if k == nil {
		return nil
	}
	out := make(Key, len(k))
	for i, v := range k {
		if v.Null {
			out[i] = Null()
		} else {
			out[i] = Value{Text: v.Text}
		}
	}
	return out
}
