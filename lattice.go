package errfeedback

import (
	"math"
	"sort"
)

// RoundingMode selects how a value is snapped onto the integer lattice.
type RoundingMode int

const (
	// RoundNearestEven rounds to the nearest lattice point and breaks ties by
	// choosing the point whose lattice index is even (banker's rounding).
	RoundNearestEven RoundingMode = iota
	// RoundNearestAway rounds to the nearest lattice point and breaks ties by
	// moving away from zero.
	RoundNearestAway
	// RoundTowardZero truncates toward zero.
	RoundTowardZero
	// RoundFloor rounds toward negative infinity.
	RoundFloor
	// RoundCeil rounds toward positive infinity.
	RoundCeil
)

func (r RoundingMode) valid() bool {
	return r >= RoundNearestEven && r <= RoundCeil
}

func (r RoundingMode) String() string {
	switch r {
	case RoundNearestEven:
		return "nearest-even"
	case RoundNearestAway:
		return "nearest-away"
	case RoundTowardZero:
		return "toward-zero"
	case RoundFloor:
		return "floor"
	case RoundCeil:
		return "ceil"
	default:
		return "invalid"
	}
}

// StepSpec is the per-step quantization and sparsity rule.
//
//   - Lattice is the spacing of the integer lattice; sent values are always
//     integer multiples of it.
//   - Rounding picks the lattice point used for a value.
//   - Keep is the maximum number of coordinates transmitted per bucket,
//     selected by descending magnitude of the quantized value (ties by
//     ascending Coord). Keep <= 0 sends nothing; Keep >= bucket size sends all
//     non-zero quantized values.
//   - MaxAbs, when positive, clamps values before quantization and must be a
//     multiple of Lattice. It is the per-coordinate transmission bound.
type StepSpec struct {
	Lattice  int64
	Rounding RoundingMode
	Keep     int
	MaxAbs   int64
}

// Validate reports whether the spec can be used for quantization.
func (s StepSpec) Validate() error {
	if s.Lattice <= 0 {
		return ErrInvalidSpec
	}
	if !s.Rounding.valid() {
		return ErrInvalidSpec
	}
	if s.Keep < 0 {
		return ErrInvalidSpec
	}
	if s.MaxAbs < 0 {
		return ErrInvalidSpec
	}
	if s.MaxAbs > 0 && s.MaxAbs%s.Lattice != 0 {
		return ErrInvalidSpec
	}
	return nil
}

// quantize maps v onto the integer lattice described by spec. It returns the
// lattice point without applying the sparse selection.
func quantize(v int64, spec StepSpec) (int64, error) {
	l := spec.Lattice
	if l <= 0 || !spec.Rounding.valid() {
		return 0, ErrInvalidSpec
	}
	if spec.MaxAbs > 0 {
		if v > spec.MaxAbs {
			v = spec.MaxAbs
		} else if v < -spec.MaxAbs {
			v = -spec.MaxAbs
		}
	}
	q := v / l
	r := v % l
	if r == 0 {
		return v, nil
	}

	var kLow, kUp, dLow, dUp int64
	if v > 0 {
		kLow, dLow = q, r
		kUp, dUp = q+1, l-r
	} else {
		kLow, dLow = q, -r
		kUp, dUp = q-1, l+r
	}

	var k int64
	switch spec.Rounding {
	case RoundTowardZero:
		k = kLow
	case RoundFloor:
		if v > 0 {
			k = kLow
		} else {
			k = kUp
		}
	case RoundCeil:
		if v > 0 {
			k = kUp
		} else {
			k = kLow
		}
	case RoundNearestAway:
		if dLow < dUp {
			k = kLow
		} else {
			k = kUp
		}
	case RoundNearestEven:
		switch {
		case dLow < dUp:
			k = kLow
		case dUp < dLow:
			k = kUp
		default:
			if kLow%2 == 0 {
				k = kLow
			} else {
				k = kUp
			}
		}
	default:
		return 0, ErrInvalidSpec
	}

	if (k > 0 && k > math.MaxInt64/l) || (k < 0 && k < math.MinInt64/l) {
		return 0, ErrOverflow
	}
	return k * l, nil
}

// magnitude returns |v| as a uint64 so that math.MinInt64 is safe.
func magnitude(v int64) uint64 {
	if v < 0 {
		return uint64(-(v + 1)) + 1
	}
	return uint64(v)
}

// checkAdd returns a+b or ErrOverflow.
func checkAdd(a, b int64) (int64, error) {
	if (b > 0 && a > math.MaxInt64-b) || (b < 0 && a < math.MinInt64-b) {
		return 0, ErrOverflow
	}
	return a + b, nil
}

// checkSub returns a-b or ErrOverflow.
func checkSub(a, b int64) (int64, error) {
	if (b < 0 && a > math.MaxInt64+b) || (b > 0 && a < math.MinInt64+b) {
		return 0, ErrOverflow
	}
	return a - b, nil
}

// selectSparse chooses at most keep coordinates from candidates by descending
// magnitude of q, breaking ties by ascending logical coordinate. The returned
// coordinates are sorted ascending.
func selectSparse(candidates []CoordValue, keep int) []CoordValue {
	if keep <= 0 || len(candidates) == 0 {
		return nil
	}
	ordered := make([]CoordValue, 0, len(candidates))
	for _, cv := range candidates {
		if cv.Value != 0 {
			ordered = append(ordered, cv)
		}
	}
	sort.Slice(ordered, func(i, j int) bool {
		mi, mj := magnitude(ordered[i].Value), magnitude(ordered[j].Value)
		if mi != mj {
			return mi > mj
		}
		return ordered[i].Coord < ordered[j].Coord
	})
	if len(ordered) > keep {
		ordered = ordered[:keep]
	}
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].Coord < ordered[j].Coord })
	return ordered
}
