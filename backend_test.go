package blockdev

import (
	"bytes"
	"reflect"
	"testing"
)

// B-1: the backend only accepts whole-block, in-range contiguous writes and
// records exactly the submitted range in its trace.
func TestBackendWriteRangeValidationAndTrace(t *testing.T) {
	backend, err := NewBackend(testBlocks, testBlockSize)
	if err != nil {
		t.Fatalf("NewBackend: %v", err)
	}

	bad := []struct {
		name  string
		start int
		data  []byte
	}{
		{"empty", 0, nil},
		{"partial block", 0, []byte{0x01}},
		{"negative start", -1, span(0x01)},
		{"past end", testBlocks, span(0x01)},
		{"straddles end", testBlocks - 1, span(0x01, 0x02)},
	}
	for _, tc := range bad {
		if err := backend.WriteRange(tc.start, tc.data); err == nil {
			t.Fatalf("%s: WriteRange accepted, want error", tc.name)
		}
	}
	if got := backend.Trace(); len(got) != 0 {
		t.Fatalf("rejected writes were traced: %v", got)
	}
	if got := backend.PendingBlocks(); len(got) != 0 {
		t.Fatalf("rejected writes staged pending blocks: %v", got)
	}

	if err := backend.WriteRange(3, span(0x30, 0x31)); err != nil {
		t.Fatalf("WriteRange: %v", err)
	}
	want := []Op{{Kind: OpWriteRange, Start: 3, Count: 2}}
	if got := backend.Trace(); !reflect.DeepEqual(got, want) {
		t.Fatalf("trace = %v, want %v", got, want)
	}
	if got, wantPending := backend.PendingBlocks(), []int{3, 4}; !reflect.DeepEqual(got, wantPending) {
		t.Fatalf("pending = %v, want %v", got, wantPending)
	}
}

// B-2: staged writes are visible in pending state but only become durable after
// a barrier, and a power cycle discards unbarriered writes.
func TestBackendBarrierThenPowerCycle(t *testing.T) {
	backend, err := NewBackend(testBlocks, testBlockSize)
	if err != nil {
		t.Fatalf("NewBackend: %v", err)
	}

	if err := backend.WriteRange(1, span(0x11)); err != nil {
		t.Fatalf("WriteRange: %v", err)
	}
	if got := backend.Durable()[1]; !bytes.Equal(got, block(0x00)) {
		t.Fatalf("block 1 durable before barrier: %x", got)
	}

	backend.PowerCycle()
	if got := backend.PendingBlocks(); len(got) != 0 {
		t.Fatalf("power cycle kept pending writes: %v", got)
	}
	if got := backend.Durable()[1]; !bytes.Equal(got, block(0x00)) {
		t.Fatalf("power cycle resurrected unbarriered data: %x", got)
	}

	if err := backend.WriteRange(1, span(0x22)); err != nil {
		t.Fatalf("WriteRange after cycle: %v", err)
	}
	if err := backend.Barrier(); err != nil {
		t.Fatalf("Barrier: %v", err)
	}
	backend.PowerCycle()
	if got := backend.Durable()[1]; !bytes.Equal(got, block(0x22)) {
		t.Fatalf("barriered data lost on power cycle: %x", got)
	}
}

// D-1: the cache holds no clean copies, so after a flush the device reads
// straight through to durable content (no stale shadow).
func TestFlushDropsCacheSoReadsSeeDurableContent(t *testing.T) {
	device, backend := newTestDevice(t)

	if err := device.Write(6, span(0x66)); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if err := device.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	// Overwrite block 6 directly on the backend to prove the device has no
	// lingering clean cache entry.
	if err := backend.WriteRange(6, span(0x77)); err != nil {
		t.Fatalf("backend overwrite: %v", err)
	}
	if err := backend.Barrier(); err != nil {
		t.Fatalf("backend barrier: %v", err)
	}

	got, err := device.Read(6, 1)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if want := block(0x77); !bytes.Equal(got, want) {
		t.Fatalf("read after flush = %x, want durable %x", got, want)
	}
}

// D-2: concurrent writers and readers must not race and must leave buffered
// blocks consistent with the last write to each block.
func TestConcurrentAccessIsSafe(t *testing.T) {
	device, _ := newTestDevice(t)

	done := make(chan struct{})
	go func() {
		defer close(done)
		for i := 0; i < 200; i++ {
			// Block 0 and 7 are never written concurrently with the loop
			// below, so their final value is deterministic.
			_ = device.Write(0, span(byte(i%251+1)))
			_ = device.Write(7, span(byte(i%251+1)))
			if i%10 == 0 {
				_ = device.Flush()
			}
		}
	}()

	for i := 0; i < 200; i++ {
		if _, err := device.Read(0, 8); err != nil {
			t.Fatalf("Read: %v", err)
		}
		if got := device.DirtyBlocks(); got == nil {
			t.Fatalf("DirtyBlocks returned nil")
		}
	}
	<-done

	// One more write so the final value is known, then flush and verify.
	if err := device.Write(0, span(0xAB)); err != nil {
		t.Fatalf("final Write: %v", err)
	}
	if err := device.Flush(); err != nil {
		t.Fatalf("final Flush: %v", err)
	}
	if got, err := device.Read(0, 1); err != nil || !bytes.Equal(got, block(0xAB)) {
		t.Fatalf("final read = %x, %v; want ab", got, err)
	}
	if got := device.DirtyBlocks(); len(got) != 0 {
		t.Fatalf("dirty after final flush = %v", got)
	}
}
