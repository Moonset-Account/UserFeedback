package exceptall

import "strings"

// TextCompareFunc compares two non-NULL text values. It must return a negative
// value when a sorts before b, zero when they are equivalent, and positive
// when a sorts after b.
type TextCompareFunc func(a, b string) int

// BinaryTextCompare is the default byte/lexicographic comparison. With this
// comparer, "Alice" and "alice" are distinct.
func BinaryTextCompare(a, b string) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	default:
		return 0
	}
}

// FoldedTextCompare compares ASCII/Unicode text after strings.ToLower. It is a
// convenient case-insensitive collation for mixed-case tests.
func FoldedTextCompare(a, b string) int {
	la := strings.ToLower(a)
	lb := strings.ToLower(b)
	return BinaryTextCompare(la, lb)
}

// CompareNullableText orders NULL first and makes NULL equivalent to NULL.
// Non-NULL values are compared with cmp.
func CompareNullableText(a, b *string, cmp TextCompareFunc) int {
	switch {
	case a == nil && b == nil:
		return 0
	case a == nil:
		return -1
	case b == nil:
		return 1
	default:
		return cmp(*a, *b)
	}
}

// businessKeyComparator compares only the fields that participate in SQL set
// equality. Source is intentionally absent.
type businessKeyComparator func(a, b Row) int

func defaultBusinessKeyComparator(textCmp TextCompareFunc) businessKeyComparator {
	return func(a, b Row) int {
		if c := CompareNullableText(a.Text, b.Text, textCmp); c != 0 {
			return c
		}
		switch {
		case a.Num < b.Num:
			return -1
		case a.Num > b.Num:
			return 1
		default:
			return 0
		}
	}
}

// recordKeyComparator compares business key and then the unique input line.
// The line tie-breaker makes every external sort total and ensures merge loops
// always advance, even when every business key is identical.
func recordKeyComparator(keyCmp businessKeyComparator) func(a, b Record) int {
	return func(a, b Record) int {
		if c := keyCmp(a.Row, b.Row); c != 0 {
			return c
		}
		switch {
		case a.Line < b.Line:
			return -1
		case a.Line > b.Line:
			return 1
		default:
			return 0
		}
	}
}

func recordLineComparator(a, b Record) int {
	switch {
	case a.Line < b.Line:
		return -1
	case a.Line > b.Line:
		return 1
	default:
		return 0
	}
}
