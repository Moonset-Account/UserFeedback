package zonetransfer

import "testing"

func TestCompareSerial(t *testing.T) {
	const max = uint32(0xFFFFFFFF)
	const half = uint32(0x80000000)
	cases := []struct {
		name string
		a, b uint32
		want SerialOrder
	}{
		{"equal", 42, 42, SerialOrderEqual},
		{"small forward", 1, 2, SerialOrderLess},
		{"small backward", 2, 1, SerialOrderGreater},
		// RFC 1982: s1 < s2 and (s2 - s1) >= 2^31 means s1 > s2.
		{"wrap forward by one", max, 0, SerialOrderLess},
		{"wrap backward by one", 0, max, SerialOrderGreater},
		{"wrap forward mid", max - 4, 3, SerialOrderLess},
		{"wrap forward eight", max - 7, 0, SerialOrderLess},
		// Exactly half the circle, in both directions: incomparable.
		{"half circle up", 0, half, SerialOrderIncomparable},
		{"half circle down", half, 0, SerialOrderIncomparable},
		{"half circle shifted", 12345, 12345 + half, SerialOrderIncomparable},
		// One step inside either side of the boundary.
		{"just under half circle", 0, half - 1, SerialOrderLess},
		{"just over half circle", 0, half + 1, SerialOrderGreater},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := CompareSerial(tc.a, tc.b); got != tc.want {
				t.Fatalf("CompareSerial(%d, %d) = %v, want %v", tc.a, tc.b, got, tc.want)
			}
			// Antisymmetry: swapping operands reverses the order, except
			// equality and incomparability which are symmetric.
			got := CompareSerial(tc.a, tc.b)
			rev := CompareSerial(tc.b, tc.a)
			switch got {
			case SerialOrderLess:
				if rev != SerialOrderGreater {
					t.Fatalf("antisymmetry broken: %v vs %v", got, rev)
				}
			case SerialOrderGreater:
				if rev != SerialOrderLess {
					t.Fatalf("antisymmetry broken: %v vs %v", got, rev)
				}
			case SerialOrderEqual, SerialOrderIncomparable:
				if rev != got {
					t.Fatalf("symmetric relation broken: %v vs %v", got, rev)
				}
			}
		})
	}
}

func TestSerialLess(t *testing.T) {
	if SerialLess(0xFFFFFFFF, 0) != true {
		t.Fatal("max serial should be less than 0 after wrap")
	}
	if SerialLess(0, 0x80000000) != false {
		t.Fatal("half-circle-apart serials must not compare as less")
	}
	if SerialLess(5, 5) != false {
		t.Fatal("equal serials are not less")
	}
}
