package dmaring

import (
	"errors"
	"testing"
)

// flatten expands segments/descriptors into the ordered list of physical byte
// addresses they cover, allowing exact byte-coverage comparison.
func flattenSegs(segs []Segment) []uint64 {
	out := make([]uint64, 0)
	for _, s := range segs {
		for i := uint64(0); i < s.Length; i++ {
			out = append(out, s.PhysAddr+i)
		}
	}
	return out
}

func flattenDescs(descs []Descriptor) []uint64 {
	out := make([]uint64, 0)
	for _, d := range descs {
		for i := uint64(0); i < d.Length; i++ {
			out = append(out, d.PhysAddr+i)
		}
	}
	return out
}

// assertLegal verifies every descriptor satisfies the hardware limits and that
// descriptors as a whole cover exactly the input bytes in order.
func assertLegal(t *testing.T, segs []Segment, descs []Descriptor, l Limits) {
	t.Helper()

	if len(descs) == 0 {
		t.Fatalf("no descriptors produced")
	}
	for i, d := range descs {
		if d.Length == 0 {
			t.Errorf("descriptor %d has zero length", i)
		}
		if d.Length > l.MaxBytes {
			t.Errorf("descriptor %d length %d exceeds MaxBytes %d", i, d.Length, l.MaxBytes)
		}
		if l.Boundary != 0 {
			if room := boundaryRoom(d.PhysAddr, l.Boundary); d.Length > room {
				t.Errorf("descriptor %d at %d length %d crosses boundary %d (room %d)",
					i, d.PhysAddr, d.Length, l.Boundary, room)
			}
		}
	}

	got := flattenDescs(descs)
	want := flattenSegs(segs)
	if len(got) != len(want) {
		t.Fatalf("coverage mismatch: descriptors cover %d bytes, segments contain %d",
			len(got), len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("byte order mismatch at index %d: got addr %d, want %d", i, got[i], want[i])
		}
	}
}

// Acceptable result: physically adjacent segments must be merged into one
// descriptor when no limit is violated, minimizing entry count.
func TestBuildAdjacentMerge(t *testing.T) {
	l := Limits{MaxBytes: 100, Boundary: 128}
	segs := []Segment{
		{PhysAddr: 10, Length: 20},
		{PhysAddr: 30, Length: 30}, // exactly adjacent: starts at 10+20
		{PhysAddr: 60, Length: 10},
	}
	descs, err := BuildDescriptors(segs, l)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(descs) != 1 {
		t.Fatalf("adjacent segments should merge into 1 descriptor, got %d: %+v", len(descs), descs)
	}
	if descs[0] != (Descriptor{PhysAddr: 10, Length: 60}) {
		t.Fatalf("unexpected merged descriptor: %+v", descs[0])
	}
	assertLegal(t, segs, descs, l)
}

// Boundary scenario: a segment crossing a power-of-two boundary must be split
// exactly at the boundary even though MaxBytes would allow more.
func TestBuildBoundarySplit(t *testing.T) {
	l := Limits{MaxBytes: 100, Boundary: 128}
	segs := []Segment{{PhysAddr: 90, Length: 40}} // 90..130 crosses 128
	descs, err := BuildDescriptors(segs, l)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []Descriptor{
		{PhysAddr: 90, Length: 38},
		{PhysAddr: 128, Length: 2},
	}
	if len(descs) != len(want) {
		t.Fatalf("want %d descriptors, got %d: %+v", len(want), len(descs), descs)
	}
	for i := range want {
		if descs[i] != want[i] {
			t.Fatalf("descriptor %d = %+v, want %+v", i, descs[i], want[i])
		}
	}
	assertLegal(t, segs, descs, l)
}

// Interleaved constraints: MaxBytes and the boundary alternate in deciding each
// split. Greedy filling must still produce the minimum number of descriptors.
func TestBuildBoundaryAndMaxBytesInterleave(t *testing.T) {
	l := Limits{MaxBytes: 100, Boundary: 128}
	segs := []Segment{{PhysAddr: 90, Length: 250}} // 90..340
	descs, err := BuildDescriptors(segs, l)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []Descriptor{
		{PhysAddr: 90, Length: 38},   // boundary limits to 128
		{PhysAddr: 128, Length: 100}, // MaxBytes
		{PhysAddr: 228, Length: 28},  // boundary limits to 256
		{PhysAddr: 256, Length: 84},  // remainder
	}
	if len(descs) != len(want) {
		t.Fatalf("want %d descriptors, got %d: %+v", len(want), len(descs), descs)
	}
	for i := range want {
		if descs[i] != want[i] {
			t.Fatalf("descriptor %d = %+v, want %+v", i, descs[i], want[i])
		}
	}
	assertLegal(t, segs, descs, l)
}

// MaxBytes only: large contiguous segment split into full MaxBytes chunks.
func TestBuildMaxBytesSplit(t *testing.T) {
	l := Limits{MaxBytes: 100} // boundary disabled
	segs := []Segment{{PhysAddr: 500, Length: 250}}
	descs, err := BuildDescriptors(segs, l)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []Descriptor{
		{PhysAddr: 500, Length: 100},
		{PhysAddr: 600, Length: 100},
		{PhysAddr: 700, Length: 50},
	}
	if len(descs) != 3 {
		t.Fatalf("want 3 descriptors, got %d: %+v", len(descs), descs)
	}
	for i := range want {
		if descs[i] != want[i] {
			t.Fatalf("descriptor %d = %+v, want %+v", i, descs[i], want[i])
		}
	}
	assertLegal(t, segs, descs, l)
}

// Non-adjacent segments with a hole can never be merged, even when both are
// individually small.
func TestBuildNonAdjacentNoMerge(t *testing.T) {
	l := Limits{MaxBytes: 100, Boundary: 128}
	segs := []Segment{
		{PhysAddr: 10, Length: 10},
		{PhysAddr: 40, Length: 10}, // gap of 20 bytes -> separate descriptor
	}
	descs, err := BuildDescriptors(segs, l)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(descs) != 2 {
		t.Fatalf("non-adjacent segments must stay separate, got %+v", descs)
	}
	assertLegal(t, segs, descs, l)
}

// Greedy extension across many adjacent segments packs them as tightly as the
// boundary and MaxBytes permit.
func TestBuildGreedyAcrossSegments(t *testing.T) {
	l := Limits{MaxBytes: 100, Boundary: 128}
	// 108..118,118..128 fills exactly to the boundary; next segment starts after.
	segs := []Segment{
		{PhysAddr: 108, Length: 10},
		{PhysAddr: 118, Length: 10},
		{PhysAddr: 128, Length: 50},
	}
	descs, err := BuildDescriptors(segs, l)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	want := []Descriptor{
		{PhysAddr: 108, Length: 20},
		{PhysAddr: 128, Length: 50},
	}
	if len(descs) != 2 {
		t.Fatalf("want 2 descriptors, got %d: %+v", len(descs), descs)
	}
	for i := range want {
		if descs[i] != want[i] {
			t.Fatalf("descriptor %d = %+v, want %+v", i, descs[i], want[i])
		}
	}
	assertLegal(t, segs, descs, l)
}

// Rejected inputs.
func TestBuildInvalidInputs(t *testing.T) {
	if _, err := BuildDescriptors(nil, Limits{MaxBytes: 100}); !errors.Is(err, ErrEmptyRequest) {
		t.Errorf("empty request: want ErrEmptyRequest, got %v", err)
	}
	if _, err := BuildDescriptors([]Segment{{PhysAddr: 0, Length: 1}}, Limits{MaxBytes: 0}); !errors.Is(err, ErrInvalidLimits) {
		t.Errorf("zero MaxBytes: want ErrInvalidLimits, got %v", err)
	}
	if _, err := BuildDescriptors([]Segment{{PhysAddr: 0, Length: 1}}, Limits{MaxBytes: 100, Boundary: 24}); !errors.Is(err, ErrInvalidLimits) {
		t.Errorf("non power-of-two boundary: want ErrInvalidLimits, got %v", err)
	}
	if _, err := BuildDescriptors([]Segment{{PhysAddr: 10, Length: 0}}, Limits{MaxBytes: 100}); !errors.Is(err, ErrZeroLength) {
		t.Errorf("zero length segment: want ErrZeroLength, got %v", err)
	}
	// PhysAddr + Length wraps uint64.
	if _, err := BuildDescriptors(
		[]Segment{{PhysAddr: ^uint64(0) - 4, Length: 10}},
		Limits{MaxBytes: 100},
	); !errors.Is(err, ErrOverflow) {
		t.Errorf("overflowing segment: want ErrOverflow, got %v", err)
	}
}

// Boundary value behavior: a descriptor ending exactly on a boundary is legal.
func TestBuildEndsExactlyOnBoundary(t *testing.T) {
	l := Limits{MaxBytes: 100, Boundary: 128}
	segs := []Segment{{PhysAddr: 78, Length: 50}} // 78..128
	descs, err := BuildDescriptors(segs, l)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(descs) != 1 || descs[0] != (Descriptor{PhysAddr: 78, Length: 50}) {
		t.Fatalf("single boundary-ending descriptor expected, got %+v", descs)
	}
	assertLegal(t, segs, descs, l)
}
