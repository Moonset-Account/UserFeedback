package egress

import (
	"fmt"
	"sort"
)

type window struct {
	start int
	end   int // exclusive raw gate-close tick
}

func mod(a, b int) int {
	m := a % b
	if m < 0 {
		m += b
	}
	return m
}

// EffectivePeriod returns the explicit Period or the sum of entry durations.
func (t *GateTable) EffectivePeriod() int {
	if t.Period > 0 {
		return t.Period
	}
	p := 0
	for _, e := range t.Entries {
		p += e.Duration
	}
	return p
}

// Validate checks the table without expanding an unbounded timeline.
func (t *GateTable) Validate() error {
	if t == nil {
		return fmt.Errorf("nil table")
	}
	if len(t.Entries) == 0 {
		return fmt.Errorf("gate table has no entries")
	}
	sum := 0
	for i, e := range t.Entries {
		if e.Duration <= 0 {
			return fmt.Errorf("gate entry %d has non-positive duration", i)
		}
		if e.Gates > GateBoth {
			return fmt.Errorf("gate entry %d has invalid mask %d", i, e.Gates)
		}
		sum += e.Duration
	}
	if t.Period != 0 && t.Period != sum {
		return fmt.Errorf("period %d does not match entry duration sum %d", t.Period, sum)
	}
	if t.Guard < 0 {
		return fmt.Errorf("guard must be non-negative")
	}
	if t.Guard > sum {
		return fmt.Errorf("guard %d exceeds period %d", t.Guard, sum)
	}
	return nil
}

func (t *GateTable) windows(class Class) []window {
	bit := GateNormal
	if class == Fast {
		bit = GateFast
	}
	p := t.EffectivePeriod()
	type seg struct{ s, e int }
	var segs []seg
	cur := 0
	for _, ent := range t.Entries {
		if ent.Gates&bit != 0 {
			segs = append(segs, seg{cur, cur + ent.Duration})
		}
		cur += ent.Duration
	}
	var merged []window
	for _, sg := range segs {
		if n := len(merged); n > 0 && merged[n-1].end == sg.s {
			merged[n-1].end = sg.e
		} else {
			merged = append(merged, window{sg.s, sg.e})
		}
	}
	// Merge a run crossing the period boundary into one extended window.
	if len(merged) >= 2 && merged[0].start == 0 && merged[len(merged)-1].end == p {
		first, last := merged[0], merged[len(merged)-1]
		cross := window{last.start, first.end + p}
		rest := append([]window{}, merged[1:len(merged)-1]...)
		rest = append(rest, cross)
		merged = rest
	}
	sort.Slice(merged, func(i, j int) bool { return merged[i].start < merged[j].start })
	return merged
}

func (t *GateTable) absoluteWindows(class Class, low int) []window {
	p := t.EffectivePeriod()
	base := t.windows(class)
	local := low - t.Origin
	k0 := local / p
	if local < 0 && local%p != 0 {
		k0--
	}
	var out []window
	for k := k0 - 2; k <= k0+3; k++ {
		for _, w := range base {
			out = append(out, window{t.Origin + w.start + k*p, t.Origin + w.end + k*p})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].start < out[j].start })
	return out
}

// MaxPayload returns the largest data payload that can fit in one fragment of
// this class after per-fragment overhead and the close-switch guard band.
func (t *GateTable) MaxPayload(class Class, overhead int) int {
	best := 0
	for _, w := range t.windows(class) {
		if c := w.end - w.start - t.Guard - overhead; c > best {
			best = c
		}
	}
	return best
}

// FitsAt reports whether a fragment starting exactly at start can transmit
// overhead and data and leave the required guard before the class gate closes.
func (t *GateTable) FitsAt(start, data, overhead int, class Class) bool {
	if data <= 0 || overhead < 0 {
		return false
	}
	for _, w := range t.absoluteWindows(class, start) {
		if start < w.start || start >= w.end {
			continue
		}
		end := start + overhead + data
		return end+t.Guard <= w.end
	}
	return false
}

// EarliestFit returns the earliest start >= low at which this exact payload
// can be sent as one legal fragment.
func (t *GateTable) EarliestFit(low, data, overhead int, class Class) (int, bool) {
	if data <= 0 {
		return 0, false
	}
	best := 0
	found := false
	for _, w := range t.absoluteWindows(class, low) {
		s := low
		if w.start > s {
			s = w.start
		}
		if s >= w.end {
			continue
		}
		if s+overhead+data+t.Guard <= w.end {
			if !found || s < best {
				best, found = s, true
			}
		}
	}
	return best, found
}

// GateOpenAt reports whether the class gate is open at an integer tick.
func (t *GateTable) GateOpenAt(at int, class Class) bool {
	for _, w := range t.absoluteWindows(class, at) {
		if at >= w.start && at < w.end {
			return true
		}
	}
	return false
}

// NextEdgeTimestamp returns the first raw gate-entry boundary strictly after
// readAt. It is a small hardware-timestamp model for observations.
func (t *GateTable) NextEdgeTimestamp(readAt int) int {
	p := t.EffectivePeriod()
	local := readAt - t.Origin
	pos := mod(local, p)
	cur := 0
	nextOffset := p
	for _, e := range t.Entries {
		end := cur + e.Duration
		if pos < end {
			nextOffset = end
			break
		}
		cur = end
	}
	// If readAt is exactly an entry boundary, take the following edge.
	if nextOffset == pos {
		cur = pos
		for _, e := range t.Entries {
			end := cur + e.Duration
			if end > pos {
				nextOffset = end
				break
			}
			cur = end
		}
	}
	k := (readAt - t.Origin - pos) / p
	if readAt-t.Origin < 0 && (readAt-t.Origin)%p != 0 {
		k--
	}
	return t.Origin + nextOffset + k*p
}
