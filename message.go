package errfeedback

import (
	"fmt"
	"sort"
)

// ErrOverflow is returned when an integer operation would overflow int64.
// The module never silently wraps; callers see this error instead.
var ErrOverflow = fmt.Errorf("errfeedback: int64 overflow")

// CoordValue is one weighted entry: the logical coordinate and its integer
// value.
type CoordValue struct {
	Coord Coord
	Value int64
}

// SparseMessage is one sparse bucket payload as it travels on the wire.
//
// Entries is the actual sparse payload: only the quantized values selected by
// the step's Keep rule.
//
// Quantized records the full per-coordinate lattice point for the step input,
// including coordinates that were not sent. The receiver ignores it while
// aggregating; it exists so recovery can prove that a regenerated packet is
// identical to the original (see Checksum) and so tests can reconstruct the
// quantization error per coordinate.
type SparseMessage struct {
	Step      StepID
	Source    SourceID
	Bucket    BucketID
	Attempt   uint64
	Layout    LayoutID
	Plan      PlanID
	Entries   []CoordValue
	Quantized []CoordValue
	Checksum  uint64
}

// ComputeChecksum returns the deterministic checksum of the message content.
// The Checksum field itself is excluded from the computation.
func (m SparseMessage) ComputeChecksum() uint64 {
	const offset64 = 1469598103934665603
	const prime64 = 1099511628211
	var h uint64 = offset64
	mix := func(v uint64) {
		for i := 0; i < 8; i++ {
			h ^= v & 0xff
			h *= prime64
			v >>= 8
		}
	}
	mix(uint64(m.Step))
	mix(uint64(int64(m.Source)))
	mix(uint64(int64(m.Bucket)))
	mix(m.Attempt)
	mix(uint64(m.Layout))
	mix(uint64(m.Plan))
	mix(uint64(len(m.Entries)))
	for _, e := range m.Entries {
		mix(uint64(int64(e.Coord)))
		mix(uint64(e.Value))
	}
	mix(uint64(len(m.Quantized)))
	for _, e := range m.Quantized {
		mix(uint64(int64(e.Coord)))
		mix(uint64(e.Value))
	}
	return h
}

// normalized returns a copy with Entries and Quantized sorted by coordinate and
// Entries de-duplicated (last write wins). It never mutates shared slices.
func (m SparseMessage) normalized() SparseMessage {
	m.Entries = dedupeSorted(m.Entries)
	m.Quantized = dedupeSorted(m.Quantized)
	return m
}

func dedupeSorted(in []CoordValue) []CoordValue {
	if len(in) == 0 {
		return nil
	}
	byCoord := make(map[Coord]int64, len(in))
	for _, e := range in {
		byCoord[e.Coord] = e.Value
	}
	coords := make([]Coord, 0, len(byCoord))
	for c := range byCoord {
		coords = append(coords, c)
	}
	sort.Slice(coords, func(i, j int) bool { return coords[i] < coords[j] })
	out := make([]CoordValue, len(coords))
	for i, c := range coords {
		out[i] = CoordValue{Coord: c, Value: byCoord[c]}
	}
	return out
}

// clone makes a deep copy so callers cannot mutate the sender's or receiver's
// stored message.
func (m SparseMessage) clone() SparseMessage {
	m.Entries = append([]CoordValue(nil), m.Entries...)
	m.Quantized = append([]CoordValue(nil), m.Quantized...)
	return m
}
