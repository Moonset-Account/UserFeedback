package errfeedback

import (
	"errors"
	"math"
	"testing"
)

func TestQuantizeRoundingModes(t *testing.T) {
	cases := []struct {
		v     int64
		mode  RoundingMode
		want  int64
		latti int64
	}{
		// Lattice 4, ties at 2 and 6.
		{2, RoundNearestEven, 0, 4},   // tie, low index 0 is even
		{6, RoundNearestEven, 8, 4},   // tie, low index 1 odd -> up to 2
		{-2, RoundNearestEven, 0, 4},  // tie toward even index 0
		{-6, RoundNearestEven, -8, 4}, // tie, indexes -2/-1 -> -2 even
		{2, RoundNearestAway, 4, 4},   // tie away from zero
		{-2, RoundNearestAway, -4, 4}, // tie away from zero
		{3, RoundNearestAway, 4, 4},
		{-3, RoundNearestAway, -4, 4},
		{7, RoundTowardZero, 4, 4},
		{-7, RoundTowardZero, -4, 4},
		{7, RoundFloor, 4, 4},
		{-7, RoundFloor, -8, 4},
		{7, RoundCeil, 8, 4},
		{-7, RoundCeil, -4, 4},
	}
	for _, c := range cases {
		got, err := quantize(c.v, StepSpec{Lattice: c.latti, Rounding: c.mode})
		if err != nil {
			t.Fatalf("quantize(%d,%v): %v", c.v, c.mode, err)
		}
		if got != c.want {
			t.Errorf("quantize(%d,%v)=%d want %d", c.v, c.mode, got, c.want)
		}
	}
}

func TestQuantizeClampAndExact(t *testing.T) {
	spec := StepSpec{Lattice: 3, Rounding: RoundNearestAway, MaxAbs: 9}
	got, err := quantize(100, spec)
	if err != nil {
		t.Fatal(err)
	}
	if got != 9 {
		t.Fatalf("clamped quantize= %d want 9", got)
	}
	if _, err := quantize(1, StepSpec{Lattice: 3, Rounding: RoundNearestAway, MaxAbs: 4}); !errors.Is(err, ErrInvalidSpec) {
		// MaxAbs must be a multiple of the lattice; caught by StepSpec.Validate.
		if err := (StepSpec{Lattice: 3, Rounding: RoundNearestAway, MaxAbs: 4}).Validate(); !errors.Is(err, ErrInvalidSpec) {
			t.Fatalf("expected invalid spec, got %v", err)
		}
	}
}

func TestQuantizeExtremesDoNotWrap(t *testing.T) {
	spec := StepSpec{Lattice: math.MaxInt64, Rounding: RoundNearestAway, MaxAbs: math.MaxInt64}
	for _, v := range []int64{math.MinInt64, math.MaxInt64, math.MaxInt64 / 2} {
		got, err := quantize(v, spec)
		if err != nil {
			// Overflow is an acceptable, explicit outcome; wrapping is not.
			if !errors.Is(err, ErrOverflow) {
				t.Fatalf("quantize(%d): unexpected error %v", v, err)
			}
			continue
		}
		if got%spec.Lattice != 0 {
			t.Fatalf("quantize(%d)=%d is off-lattice", v, got)
		}
	}
}

func TestSelectSparseOrdering(t *testing.T) {
	cands := []CoordValue{
		{Coord: 5, Value: 4},
		{Coord: 2, Value: -9}, // magnitude 9 wins
		{Coord: 7, Value: 9},  // tie with 2, lower coord first
		{Coord: 1, Value: 0},  // zero never selected
		{Coord: 3, Value: 4},
	}
	got := selectSparse(cands, 2)
	if len(got) != 2 {
		t.Fatalf("got %d entries want 2", len(got))
	}
	// Magnitudes: 9,9,4,4 -> coords 2 and 7. Result sorted ascending.
	if got[0].Coord != 2 || got[1].Coord != 7 {
		t.Fatalf("selected coords = %v, want [2 7]", got)
	}

	all := selectSparse(cands, 100)
	if len(all) != 4 {
		t.Fatalf("keep>=n should select all non-zero, got %d", len(all))
	}
	if selectSparse(cands, 0) != nil {
		t.Fatal("keep=0 must select nothing")
	}
}

func TestCheckArithmetic(t *testing.T) {
	if _, err := checkAdd(math.MaxInt64, 1); !errors.Is(err, ErrOverflow) {
		t.Fatalf("expected add overflow, got %v", err)
	}
	if _, err := checkSub(math.MinInt64, 1); !errors.Is(err, ErrOverflow) {
		t.Fatalf("expected sub overflow, got %v", err)
	}
	if v, err := checkAdd(2, 3); err != nil || v != 5 {
		t.Fatalf("add(2,3)=%d,%v", v, err)
	}
}
