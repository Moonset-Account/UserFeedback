package fecrx_test

import (
	"fmt"

	fecrx "fecrx"
)

func xor(a, b []byte) []byte {
	n := len(a)
	if len(b) > n {
		n = len(b)
	}
	out := make([]byte, n)
	for i := 0; i < n; i++ {
		var x, y byte
		if i < len(a) {
			x = a[i]
		}
		if i < len(b) {
			y = b[i]
		}
		out[i] = x ^ y
	}
	return out
}

// ExampleReceiver demonstrates the signature slide case: with only
// A xor B and A xor C, sliding past A keeps the derived B xor C
// equation, so C is recovered once B arrives.
func ExampleReceiver() {
	h := fecrx.Hooks{
		OnDeliver: func(id uint64, data []byte) {
			fmt.Printf("deliver %d: %s\n", id, data)
		},
		OnGap: func(from, to uint64) {
			fmt.Printf("gap [%d,%d)\n", from, to)
		},
	}
	r := fecrx.New(0, 3, h)

	A := []byte("AA")
	B := []byte("BB")
	C := []byte("CC")

	// Repair packet 1 = A xor B, packet 2 = A xor C.
	if err := r.AddEquation(1, 1, []uint64{0, 1}, xor(A, B)); err != nil {
		panic(err)
	}
	if err := r.AddEquation(2, 1, []uint64{0, 2}, xor(A, C)); err != nil {
		panic(err)
	}

	// Slide past A (id 0). The caller is told about the gap and the
	// derived B xor C row survives elimination.
	if err := r.SlideTo(1); err != nil {
		panic(err)
	}

	// B arrives as an original packet; B and C are then delivered
	// contiguously and exactly once.
	if err := r.AddEquation(3, 1, []uint64{1}, B); err != nil {
		panic(err)
	}

	// Output:
	// gap [0,1)
	// deliver 1: BB
	// deliver 2: CC
}
