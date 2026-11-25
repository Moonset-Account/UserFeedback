package mirror

import (
	"fmt"
	"sort"
)

// truncate walks header ranges in frame order and returns the longest confirmed
// byte prefix that the depth limit permits.
//
// A header is confirmed only when all of the following hold:
//   - its Depth is defined and not deeper than max;
//   - both boundaries are present;
//   - End is greater than Start;
//   - Start equals the current prefix length (no gap and no overlap);
//   - End is within the frame.
//
// The first header that fails a check stops the walk. Headers after it are not
// copied, because their position relative to unconfirmed bytes is unknown.
func truncate(frame []byte, fields []FieldRange, max Depth) (int, []string, *Truncation) {
	ordered := orderFields(fields)
	copied := make([]string, 0, len(ordered))
	cut := 0

	if len(ordered) == 0 {
		if len(frame) == 0 {
			return 0, copied, nil
		}
		return 0, copied, &Truncation{
			Reason: ReasonNoFieldData,
			Offset: 0,
			Detail: "no header boundary data is available",
		}
	}

	for _, field := range ordered {
		if !field.Depth.Valid() {
			return cut, copied, &Truncation{
				Reason: ReasonUnknownDepth,
				Field:  field.Name,
				Depth:  field.Depth,
				Offset: cut,
				Detail: fmt.Sprintf("header %q has undefined depth %d", field.Name, int(field.Depth)),
			}
		}
		if field.Depth > max {
			return cut, copied, &Truncation{
				Reason: ReasonDepthLimit,
				Field:  field.Name,
				Depth:  field.Depth,
				Offset: cut,
				Detail: fmt.Sprintf("header %q is deeper than the receiver limit %s", field.Name, max),
			}
		}
		if !field.HasBounds() {
			return cut, copied, &Truncation{
				Reason: ReasonMissingBounds,
				Field:  field.Name,
				Depth:  field.Depth,
				Offset: cut,
				Detail: fmt.Sprintf("header %q has incomplete boundaries (start=%d, end=%d)", field.Name, field.Start, field.End),
			}
		}
		if field.End <= field.Start {
			return cut, copied, &Truncation{
				Reason: ReasonInvalidBounds,
				Field:  field.Name,
				Depth:  field.Depth,
				Offset: cut,
				Detail: fmt.Sprintf("header %q end %d is not after start %d", field.Name, field.End, field.Start),
			}
		}
		if field.Start < cut {
			return cut, copied, &Truncation{
				Reason: ReasonOverlap,
				Field:  field.Name,
				Depth:  field.Depth,
				Offset: cut,
				Detail: fmt.Sprintf("header %q starts at %d, inside the confirmed prefix ending at %d", field.Name, field.Start, cut),
			}
		}
		if field.Start > cut {
			return cut, copied, &Truncation{
				Reason: ReasonGap,
				Field:  field.Name,
				Depth:  field.Depth,
				Offset: cut,
				Detail: fmt.Sprintf("header %q starts at %d but the confirmed prefix ends at %d", field.Name, field.Start, cut),
			}
		}
		if field.End > len(frame) {
			return cut, copied, &Truncation{
				Reason: ReasonOutOfBounds,
				Field:  field.Name,
				Depth:  field.Depth,
				Offset: cut,
				Detail: fmt.Sprintf("header %q ends at %d but the frame has %d bytes", field.Name, field.End, len(frame)),
			}
		}

		cut = field.End
		copied = append(copied, field.Name)
	}

	if cut < len(frame) {
		return cut, copied, &Truncation{
			Reason: ReasonUncoveredBytes,
			Offset: cut,
			Detail: fmt.Sprintf("%d trailing byte(s) are not covered by any header range", len(frame)-cut),
		}
	}
	return cut, copied, nil
}

// orderFields sorts fields by Start when every field has a known, non-negative
// Start. If any Start is unknown the record order is preserved, because a
// missing boundary must stop the walk rather than be hidden by sorting.
func orderFields(fields []FieldRange) []FieldRange {
	if len(fields) < 2 {
		return fields
	}
	for _, field := range fields {
		if field.Start < 0 {
			return fields
		}
	}
	ordered := append([]FieldRange(nil), fields...)
	sort.SliceStable(ordered, func(i, j int) bool {
		return ordered[i].Start < ordered[j].Start
	})
	return ordered
}
