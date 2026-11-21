package dmaring

import (
	"errors"
	"testing"
)

func mustRing(t *testing.T, capacity int, l Limits) *Ring {
	t.Helper()
	r, err := NewRing(capacity, l)
	if err != nil {
		t.Fatalf("NewRing: unexpected error: %v", err)
	}
	return r
}

// Slot accounting on a simple, non-wrapping request. The 512-byte boundary is
// far enough away that only MaxBytes drives the splits in this file, except
// where a test explicitly crosses a boundary.
func TestRingSubmitSlotAccounting(t *testing.T) {
	r := mustRing(t, 4, Limits{MaxBytes: 100, Boundary: 512})

	// Start at 128 so the whole 150-byte transfer stays inside the 0..512 region.
	descs, err := r.Submit(1, []Segment{{PhysAddr: 128, Length: 150}})
	if err != nil {
		t.Fatalf("Submit: unexpected error: %v", err)
	}
	if len(descs) != 2 {
		t.Fatalf("want 2 descriptors, got %d", len(descs))
	}
	if r.Used() != 2 || r.Free() != 2 || r.Outstanding() != 1 {
		t.Fatalf("after submit: used=%d free=%d outstanding=%d", r.Used(), r.Free(), r.Outstanding())
	}
	if d, _ := r.Slot(0); d != (Descriptor{PhysAddr: 128, Length: 100}) {
		t.Fatalf("slot 0 = %+v", d)
	}
	if d, _ := r.Slot(1); d != (Descriptor{PhysAddr: 228, Length: 50}) {
		t.Fatalf("slot 1 = %+v", d)
	}
}

// Rejected result: when the request needs more slots than free, Submit must
// return ErrNoSpace and leave the production pointer and all slots untouched.
func TestRingNoSpaceDoesNotAdvance(t *testing.T) {
	r := mustRing(t, 4, Limits{MaxBytes: 100, Boundary: 512})

	// Occupy 3 of 4 slots with a boundary-safe input.
	if _, err := r.Submit(1, []Segment{{PhysAddr: 128, Length: 300}}); err != nil {
		t.Fatalf("Submit: unexpected error: %v", err)
	}
	if r.Free() != 1 {
		t.Fatalf("precondition: want 1 free slot, got %d", r.Free())
	}

	// Need 2 slots but only 1 is free -> wait, nothing must move.
	_, err := r.Submit(2, []Segment{{PhysAddr: 1024, Length: 150}})
	if !errors.Is(err, ErrNoSpace) {
		t.Fatalf("want ErrNoSpace, got %v", err)
	}
	if r.Used() != 3 || r.Free() != 1 || r.Outstanding() != 1 {
		t.Fatalf("production state changed after failed submit: used=%d free=%d outstanding=%d",
			r.Used(), r.Free(), r.Outstanding())
	}
	if snap := r.Snapshot(); len(snap) != 1 || snap[0].ID != 1 {
		t.Fatalf("failed submit must not register a request: %+v", snap)
	}
	// The empty slot must still read as zero.
	if d, _ := r.Slot(3); d != (Descriptor{}) {
		t.Fatalf("empty slot was clobbered: %+v", d)
	}

	// A request that fits the single remaining slot must still be accepted.
	if _, err := r.Submit(3, []Segment{{PhysAddr: 2048, Length: 80}}); err != nil {
		t.Fatalf("small Submit after rejection failed: %v", err)
	}
	if r.Free() != 0 || r.Outstanding() != 2 {
		t.Fatalf("used=%d free=%d outstanding=%d", r.Used(), r.Free(), r.Outstanding())
	}
}

// Headline scenario:
//
//	A 4-slot ring. A preliminary request P fills slots 0,1 and is reaped, so the
//	production pointer sits at slot 2. Request A then takes slots 2,3 (the tail),
//	and request B wraps around the tail into slots 0,1. B completes before A: no
//	slot can be returned until the oldest outstanding request A is also complete,
//	at which point both are reaped in submission order and all slots recycle.
func TestRingWrapAndOutOfOrderCompletion(t *testing.T) {
	const cap = 4
	r := mustRing(t, cap, Limits{MaxBytes: 100, Boundary: 512})

	// Preliminary P: 2 descriptors in slots 0,1; completed and reaped immediately
	// so the producer moves to slot 2 while the consumers catch up.
	if _, err := r.Submit(1, []Segment{{PhysAddr: 600, Length: 200}}); err != nil {
		t.Fatalf("submit P: %v", err)
	}
	if d, _ := r.Slot(0); d != (Descriptor{PhysAddr: 600, Length: 100}) {
		t.Fatalf("P slot 0 = %+v", d)
	}
	if err := r.Complete(1); err != nil {
		t.Fatalf("Complete P: %v", err)
	}
	if reaped := r.Reap(); len(reaped) != 1 || reaped[0].ID != 1 {
		t.Fatalf("reap P = %+v", reaped)
	}
	if r.Free() != cap || r.Used() != 0 {
		t.Fatalf("after P reclaim: free=%d used=%d", r.Free(), r.Used())
	}

	// A: 2 descriptors, occupies slots 2,3 (the tail end of the ring). Start
	// 300 leaves 212 bytes before the 512 boundary, so only MaxBytes splits it.
	aDescs, err := r.Submit(10, []Segment{{PhysAddr: 300, Length: 200}})
	if err != nil {
		t.Fatalf("submit A: %v", err)
	}
	if len(aDescs) != 2 {
		t.Fatalf("A: want 2 descriptors, got %d", len(aDescs))
	}
	if r.Used() != 2 || r.Free() != 2 {
		t.Fatalf("after A: used=%d free=%d", r.Used(), r.Free())
	}
	if d, _ := r.Slot(2); d != (Descriptor{PhysAddr: 300, Length: 100}) {
		t.Fatalf("A slot 2 = %+v", d)
	}
	if d, _ := r.Slot(3); d != (Descriptor{PhysAddr: 400, Length: 100}) {
		t.Fatalf("A slot 3 = %+v", d)
	}

	// B: 2 descriptors, must wrap around the tail into slots 0,1. Its input
	// crosses the 512-byte power-of-two boundary and is split exactly there.
	bDescs, err := r.Submit(20, []Segment{{PhysAddr: 500, Length: 112}})
	if err != nil {
		t.Fatalf("submit B: %v", err)
	}
	if len(bDescs) != 2 {
		t.Fatalf("B: want 2 descriptors, got %d", len(bDescs))
	}
	if bDescs[0] != (Descriptor{PhysAddr: 500, Length: 12}) ||
		bDescs[1] != (Descriptor{PhysAddr: 512, Length: 100}) {
		t.Fatalf("unexpected B descriptors: %+v", bDescs)
	}
	if r.Used() != 4 || r.Free() != 0 {
		t.Fatalf("ring should be full: used=%d free=%d", r.Used(), r.Free())
	}

	// Physical ranges after the wrap: slots 0,1 hold B and slots 2,3 hold A.
	checks := []struct {
		slot int
		want Descriptor
	}{
		{0, Descriptor{PhysAddr: 500, Length: 12}},
		{1, Descriptor{PhysAddr: 512, Length: 100}},
		{2, Descriptor{PhysAddr: 300, Length: 100}},
		{3, Descriptor{PhysAddr: 400, Length: 100}},
	}
	for _, c := range checks {
		if d, _ := r.Slot(c.slot); d != c.want {
			t.Errorf("slot %d = %+v, want %+v", c.slot, d, c.want)
		}
	}

	// Ring is full: another request must wait without moving the producer.
	if _, err := r.Submit(30, []Segment{{PhysAddr: 2048, Length: 10}}); !errors.Is(err, ErrNoSpace) {
		t.Fatalf("full ring Submit: want ErrNoSpace, got %v", err)
	}
	if r.Used() != 4 || r.Outstanding() != 2 {
		t.Fatalf("failed submit altered state: used=%d outstanding=%d", r.Used(), r.Outstanding())
	}

	// Device reports the younger request B first. Out of order is allowed.
	if err := r.Complete(20); err != nil {
		t.Fatalf("Complete B: %v", err)
	}
	snap := r.Snapshot()
	if len(snap) != 2 || !snap[1].Done || snap[0].Done {
		t.Fatalf("only B should be done: %+v", snap)
	}
	// B cannot release anything while the oldest outstanding request A is unfinished.
	if reaped := r.Reap(); len(reaped) != 0 {
		t.Fatalf("Reap with oldest unfinished must return nothing, got %+v", reaped)
	}
	if r.Free() != 0 || r.Used() != 4 {
		t.Fatalf("no slot may be freed: free=%d used=%d", r.Free(), r.Used())
	}

	// Now complete the oldest request A; both become continuously complete.
	if err := r.Complete(10); err != nil {
		t.Fatalf("Complete A: %v", err)
	}
	reaped := r.Reap()
	if len(reaped) != 2 {
		t.Fatalf("want 2 reaped requests, got %+v", reaped)
	}
	if reaped[0].ID != 10 || reaped[1].ID != 20 {
		t.Fatalf("reap order must follow submission order: %+v", reaped)
	}
	// Reaped metadata carries the wrapped starting slot and full descriptors.
	if reaped[0].StartSlot != 2 || reaped[0].Count != 2 {
		t.Errorf("A metadata = %+v", reaped[0])
	}
	if reaped[1].StartSlot != 0 || reaped[1].Count != 2 {
		t.Errorf("B metadata = %+v", reaped[1])
	}
	if r.Used() != 0 || r.Free() != cap || r.Outstanding() != 0 {
		t.Fatalf("after reap: used=%d free=%d outstanding=%d", r.Used(), r.Free(), r.Outstanding())
	}
	if reaped := r.Reap(); len(reaped) != 0 {
		t.Fatalf("second Reap must be empty, got %+v", reaped)
	}

	// All slots are reusable again. The monotonic production pointer is now 10,
	// so this 4-descriptor request wraps once more: it lands in slots 2,3 then
	// 0,1, demonstrating repeated wrap-around recycling.
	recycled, err := r.Submit(40, []Segment{{PhysAddr: 1024, Length: 400}})
	if err != nil {
		t.Fatalf("Submit after recycle: %v", err)
	}
	if len(recycled) != 4 {
		t.Fatalf("want 4 recycled descriptors, got %d", len(recycled))
	}
	if r.Used() != 4 || r.Free() != 0 {
		t.Fatalf("recycled ring should be full again: used=%d free=%d", r.Used(), r.Free())
	}
	recycledChecks := []struct {
		slot int
		want Descriptor
	}{
		{2, Descriptor{PhysAddr: 1024, Length: 100}},
		{3, Descriptor{PhysAddr: 1124, Length: 100}},
		{0, Descriptor{PhysAddr: 1224, Length: 100}},
		{1, Descriptor{PhysAddr: 1324, Length: 100}},
	}
	for _, c := range recycledChecks {
		if d, _ := r.Slot(c.slot); d != c.want {
			t.Errorf("recycled slot %d = %+v, want %+v", c.slot, d, c.want)
		}
	}
}

// Partial reclamation: after reaping a leading request, a still-outstanding
// younger request keeps its slots and stays the new oldest.
func TestRingPartialReapKeepsYounger(t *testing.T) {
	r := mustRing(t, 4, Limits{MaxBytes: 100, Boundary: 512})

	if _, err := r.Submit(1, []Segment{{PhysAddr: 128, Length: 100}}); err != nil {
		t.Fatalf("submit A: %v", err)
	}
	if _, err := r.Submit(2, []Segment{{PhysAddr: 256, Length: 128}}); err != nil {
		t.Fatalf("submit B: %v", err)
	}
	if _, err := r.Submit(3, []Segment{{PhysAddr: 900, Length: 50}}); err != nil {
		t.Fatalf("submit C: %v", err)
	}
	if r.Used() != 4 || r.Free() != 0 {
		t.Fatalf("precondition used=%d free=%d", r.Used(), r.Free())
	}

	// Complete the middle and youngest requests: nothing can move yet.
	if err := r.Complete(2); err != nil {
		t.Fatalf("Complete B: %v", err)
	}
	if err := r.Complete(3); err != nil {
		t.Fatalf("Complete C: %v", err)
	}
	if reaped := r.Reap(); len(reaped) != 0 {
		t.Fatalf("oldest A unfinished, reap got %+v", reaped)
	}

	// Complete A: all three are now continuously complete and free 4 slots.
	if err := r.Complete(1); err != nil {
		t.Fatalf("Complete A: %v", err)
	}
	reaped := r.Reap()
	if len(reaped) != 3 {
		t.Fatalf("want 3 reaped, got %d", len(reaped))
	}
	if reaped[0].ID != 1 || reaped[1].ID != 2 || reaped[2].ID != 3 {
		t.Fatalf("wrong reap order: %+v", reaped)
	}
	if r.Free() != 4 || r.Outstanding() != 0 {
		t.Fatalf("free=%d outstanding=%d", r.Free(), r.Outstanding())
	}
}

// A request that could never fit even an empty ring is still reported as
// ErrNoSpace rather than being partially queued.
func TestRingRequestLargerThanCapacity(t *testing.T) {
	r := mustRing(t, 2, Limits{MaxBytes: 100, Boundary: 512})
	_, err := r.Submit(1, []Segment{{PhysAddr: 128, Length: 300}})
	if !errors.Is(err, ErrNoSpace) {
		t.Fatalf("want ErrNoSpace, got %v", err)
	}
	if r.Used() != 0 || r.Outstanding() != 0 {
		t.Fatalf("oversized request must leave ring empty")
	}
}

func TestRingDuplicateAndUnknownRequests(t *testing.T) {
	r := mustRing(t, 4, Limits{MaxBytes: 100, Boundary: 512})
	if _, err := r.Submit(7, []Segment{{PhysAddr: 128, Length: 10}}); err != nil {
		t.Fatalf("submit: %v", err)
	}
	if _, err := r.Submit(7, []Segment{{PhysAddr: 128, Length: 10}}); !errors.Is(err, ErrDuplicateRequest) {
		t.Fatalf("duplicate Submit: want ErrDuplicateRequest, got %v", err)
	}
	if err := r.Complete(99); !errors.Is(err, ErrUnknownRequest) {
		t.Fatalf("unknown Complete: want ErrUnknownRequest, got %v", err)
	}
	if err := r.Complete(7); err != nil {
		t.Fatalf("Complete 7: %v", err)
	}
	if reaped := r.Reap(); len(reaped) != 1 || reaped[0].ID != 7 {
		t.Fatalf("reap = %+v", reaped)
	}
	if err := r.Complete(7); !errors.Is(err, ErrUnknownRequest) {
		t.Fatalf("Complete after reap: want ErrUnknownRequest, got %v", err)
	}
}

func TestNewRingRejectsInvalidConstruction(t *testing.T) {
	if _, err := NewRing(0, Limits{MaxBytes: 100}); !errors.Is(err, ErrInvalidLimits) {
		t.Errorf("zero capacity: want ErrInvalidLimits, got %v", err)
	}
	if _, err := NewRing(4, Limits{MaxBytes: 0}); !errors.Is(err, ErrInvalidLimits) {
		t.Errorf("zero MaxBytes: want ErrInvalidLimits, got %v", err)
	}
	if _, err := NewRing(4, Limits{MaxBytes: 100, Boundary: 7}); !errors.Is(err, ErrInvalidLimits) {
		t.Errorf("non power-of-two boundary: want ErrInvalidLimits, got %v", err)
	}
}

func TestRingSlotBounds(t *testing.T) {
	r := mustRing(t, 2, Limits{MaxBytes: 100, Boundary: 512})
	if _, err := r.Slot(-1); !errors.Is(err, ErrInvalidLimits) {
		t.Errorf("Slot(-1): want ErrInvalidLimits, got %v", err)
	}
	if _, err := r.Slot(2); !errors.Is(err, ErrInvalidLimits) {
		t.Errorf("Slot(2): want ErrInvalidLimits, got %v", err)
	}
}
