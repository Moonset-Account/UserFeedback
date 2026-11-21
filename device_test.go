package blockdev

import (
	"bytes"
	"reflect"
	"testing"
)

const (
	testBlockSize = 4
	testBlocks    = 8
)

// newTestDevice builds a zero-filled device and its backend.
func newTestDevice(t *testing.T) (*Device, *Backend) {
	t.Helper()
	backend, err := NewBackend(testBlocks, testBlockSize)
	if err != nil {
		t.Fatalf("NewBackend: %v", err)
	}
	return Open(backend), backend
}

// block fills a block-sized buffer with value.
func block(value byte) []byte {
	out := make([]byte, testBlockSize)
	for i := range out {
		out[i] = value
	}
	return out
}

// span concatenates block values into one contiguous buffer.
func span(values ...byte) []byte {
	var out []byte
	for _, value := range values {
		out = append(out, block(value)...)
	}
	return out
}

// writeRangeOps extracts only the backend write ranges from a trace.
func writeRangeOps(trace []Op) []Op {
	var out []Op
	for _, op := range trace {
		if op.Kind == OpWriteRange {
			out = append(out, op)
		}
	}
	return out
}

// countBarriers counts barrier operations in a trace.
func countBarriers(trace []Op) int {
	n := 0
	for _, op := range trace {
		if op.Kind == OpBarrier {
			n++
		}
	}
	return n
}

// durableBlocks snapshots every durable block from the backend.
func durableBlocks(t *testing.T, backend *Backend) [][]byte {
	t.Helper()
	return backend.Durable()
}

// AC-1: a buffered write is visible to read immediately but not durable until
// a flush, and a later write to the same block replaces the earlier value.
func TestWriteBufferOverwriteAndReadLatest(t *testing.T) {
	device, backend := newTestDevice(t)

	if err := device.Write(2, span(0x11)); err != nil {
		t.Fatalf("Write first: %v", err)
	}
	if err := device.Write(2, span(0x22, 0x33)); err != nil {
		t.Fatalf("Write overwriting 2..3: %v", err)
	}

	got, err := device.Read(2, 2)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if want := span(0x22, 0x33); !bytes.Equal(got, want) {
		t.Fatalf("read newest value = %x, want %x", got, want)
	}

	if got, want := device.DirtyBlocks(), []int{2, 3}; !reflect.DeepEqual(got, want) {
		t.Fatalf("dirty blocks = %v, want %v", got, want)
	}

	if got := backend.Trace(); len(got) != 0 {
		t.Fatalf("backend touched before flush: %v", got)
	}
	if got := durableBlocks(t, backend)[2]; !bytes.Equal(got, block(0x00)) {
		t.Fatalf("block 2 became durable before flush: %x", got)
	}
}

// AC-2: flush persists every dirty block, merging contiguous runs into one
// backend write per run plus a single barrier, and leaves two non-adjacent
// dirty regions as two separate ranges.
func TestFlushCoalescesContiguousRanges(t *testing.T) {
	device, backend := newTestDevice(t)

	if err := device.Write(0, span(0xA0, 0xA1)); err != nil {
		t.Fatalf("Write first region: %v", err)
	}
	if err := device.Write(5, span(0xB0, 0xB1, 0xB2)); err != nil {
		t.Fatalf("Write second region: %v", err)
	}
	if err := device.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	wantRanges := []Op{
		{Kind: OpWriteRange, Start: 0, Count: 2},
		{Kind: OpWriteRange, Start: 5, Count: 3},
	}
	if got := writeRangeOps(backend.Trace()); !reflect.DeepEqual(got, wantRanges) {
		t.Fatalf("write ranges = %v, want %v", got, wantRanges)
	}
	if got := countBarriers(backend.Trace()); got != 1 {
		t.Fatalf("barrier count = %d, want 1", got)
	}

	durable := durableBlocks(t, backend)
	if !bytes.Equal(durable[0], block(0xA0)) || !bytes.Equal(durable[1], block(0xA1)) {
		t.Fatalf("first region not durable: %x %x", durable[0], durable[1])
	}
	if !bytes.Equal(durable[5], block(0xB0)) || !bytes.Equal(durable[7], block(0xB2)) {
		t.Fatalf("second region not durable: %x .. %x", durable[5], durable[7])
	}
	if got := device.DirtyBlocks(); len(got) != 0 {
		t.Fatalf("dirty blocks after flush = %v, want none", got)
	}

	// A second flush with no dirty blocks must not write or barrier again.
	before := len(backend.Trace())
	if err := device.Flush(); err != nil {
		t.Fatalf("empty Flush: %v", err)
	}
	if got := len(backend.Trace()); got != before {
		t.Fatalf("empty flush issued backend ops: %v", backend.Trace()[before:])
	}
}

// AC-3 (core power-loss drill): dirty two non-adjacent regions, persist only
// one of them with WriteDurable, then power cycle. The persisted region keeps
// its new content; the other region must still read its original on-disk
// content. The trace proves the merge scope covered only the requested blocks.
func TestWriteDurableKeepsOtherDirtyRegionVolatile(t *testing.T) {
	device, backend := newTestDevice(t)

	// Pre-existing, already durable content on both regions.
	if err := backend.WriteRange(0, span(0x01, 0x02)); err != nil {
		t.Fatalf("seed first region: %v", err)
	}
	if err := backend.WriteRange(4, span(0x41, 0x42)); err != nil {
		t.Fatalf("seed second region: %v", err)
	}
	if err := backend.Barrier(); err != nil {
		t.Fatalf("seed barrier: %v", err)
	}
	backend.ResetTrace()

	// Dirty two non-adjacent regions through the cache.
	if err := device.Write(0, span(0x11, 0x12)); err != nil {
		t.Fatalf("Write first region: %v", err)
	}
	if err := device.Write(4, span(0x51, 0x52)); err != nil {
		t.Fatalf("Write second region: %v", err)
	}
	if got, want := device.DirtyBlocks(), []int{0, 1, 4, 5}; !reflect.DeepEqual(got, want) {
		t.Fatalf("dirty before persist = %v, want %v", got, want)
	}

	// Persist only the first region; the second stays buffered.
	if err := device.WriteDurable(0, span(0x11, 0x12)); err != nil {
		t.Fatalf("WriteDurable: %v", err)
	}

	wantRanges := []Op{{Kind: OpWriteRange, Start: 0, Count: 2}}
	if got := writeRangeOps(backend.Trace()); !reflect.DeepEqual(got, wantRanges) {
		t.Fatalf("write ranges = %v, want %v (second region must not be merged)", got, wantRanges)
	}
	if got := countBarriers(backend.Trace()); got != 1 {
		t.Fatalf("barrier count = %d, want 1", got)
	}
	if got, want := device.DirtyBlocks(), []int{4, 5}; !reflect.DeepEqual(got, want) {
		t.Fatalf("dirty after persist = %v, want %v", got, want)
	}
	// Read of the still-dirty region must surface the buffered value.
	if got, err := device.Read(4, 2); err != nil || !bytes.Equal(got, span(0x51, 0x52)) {
		t.Fatalf("buffered read = %x, %v; want 5151..", got, err)
	}

	// Power loss.
	device.PowerCycle()

	gotFirst, err := device.Read(0, 2)
	if err != nil {
		t.Fatalf("Read first region after power cycle: %v", err)
	}
	if want := span(0x11, 0x12); !bytes.Equal(gotFirst, want) {
		t.Fatalf("persisted region after power cycle = %x, want %x", gotFirst, want)
	}
	gotSecond, err := device.Read(4, 2)
	if err != nil {
		t.Fatalf("Read second region after power cycle: %v", err)
	}
	if want := span(0x41, 0x42); !bytes.Equal(gotSecond, want) {
		t.Fatalf("unpersisted region after power cycle = %x, want original %x", gotSecond, want)
	}
	if got := device.DirtyBlocks(); len(got) != 0 {
		t.Fatalf("dirty blocks after power cycle = %v, want none", got)
	}
	if op := backend.Trace()[len(backend.Trace())-1]; op.Kind != OpPowerCycle {
		t.Fatalf("last trace op = %v, want power-cycle", op)
	}
}

// AC-4: only the blocks covered by a WriteDurable request leave the dirty set,
// even when neighbouring blocks (below and above) are dirty in the same run.
func TestWriteDurableCommitsOnlyCoveredBlocksInsideDirtyRun(t *testing.T) {
	device, backend := newTestDevice(t)

	if err := device.Write(0, span(0x10, 0x11, 0x12, 0x13, 0x14)); err != nil {
		t.Fatalf("Write run: %v", err)
	}
	if err := device.WriteDurable(2, span(0xD2)); err != nil {
		t.Fatalf("WriteDurable middle: %v", err)
	}

	if got, want := writeRangeOps(backend.Trace()), []Op{{Kind: OpWriteRange, Start: 2, Count: 1}}; !reflect.DeepEqual(got, want) {
		t.Fatalf("write ranges = %v, want %v", got, want)
	}
	if got, want := device.DirtyBlocks(), []int{0, 1, 3, 4}; !reflect.DeepEqual(got, want) {
		t.Fatalf("dirty = %v, want %v", got, want)
	}

	// A later full flush must still merge only the remaining dirty run, which
	// now has a clean hole at block 2.
	backend.ResetTrace()
	if err := device.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}
	wantRanges := []Op{
		{Kind: OpWriteRange, Start: 0, Count: 2},
		{Kind: OpWriteRange, Start: 3, Count: 2},
	}
	if got := writeRangeOps(backend.Trace()); !reflect.DeepEqual(got, wantRanges) {
		t.Fatalf("flush ranges = %v, want %v", got, wantRanges)
	}

	durable := durableBlocks(t, backend)
	for index, want := range map[int]byte{0: 0x10, 1: 0x11, 2: 0xD2, 3: 0x13, 4: 0x14} {
		if !bytes.Equal(durable[index], block(want)) {
			t.Fatalf("durable[%d] = %x, want %x", index, durable[index], block(want))
		}
	}
}

// AC-5: interleaved writes, reads, full flushes and durable writes all observe
// the newest value and never resurrect stale buffered data.
func TestInterleavedWritesReadsFlushDurable(t *testing.T) {
	device, backend := newTestDevice(t)

	steps := []struct {
		name string
		run  func() error
		want []byte // expected read of blocks 0..2 after the step
	}{
		{
			name: "write 0..1",
			run:  func() error { return device.Write(0, span(0x01, 0x02)) },
			want: span(0x01, 0x02, 0x00),
		},
		{
			name: "overwrite block 1, write block 2",
			run:  func() error { return device.Write(1, span(0x22, 0x03)) },
			want: span(0x01, 0x22, 0x03),
		},
		{
			name: "durable-write block 0 only",
			run:  func() error { return device.WriteDurable(0, span(0xAA)) },
			want: span(0xAA, 0x22, 0x03),
		},
		{
			name: "flush remaining dirt",
			run:  func() error { return device.Flush() },
			want: span(0xAA, 0x22, 0x03),
		},
		{
			name: "write block 2 then block 0",
			run: func() error {
				if err := device.Write(2, span(0xCC)); err != nil {
					return err
				}
				return device.Write(0, span(0xDD))
			},
			want: span(0xDD, 0x22, 0xCC),
		},
	}

	for _, step := range steps {
		if err := step.run(); err != nil {
			t.Fatalf("%s: %v", step.name, err)
		}
		got, err := device.Read(0, 3)
		if err != nil {
			t.Fatalf("%s: Read: %v", step.name, err)
		}
		if !bytes.Equal(got, step.want) {
			t.Fatalf("%s: read = %x, want %x", step.name, got, step.want)
		}
	}

	// Power cycle drops the final two dirty blocks, restoring flushed state.
	device.PowerCycle()
	got, err := device.Read(0, 3)
	if err != nil {
		t.Fatalf("Read after power cycle: %v", err)
	}
	if want := span(0xAA, 0x22, 0x03); !bytes.Equal(got, want) {
		t.Fatalf("read after power cycle = %x, want %x", got, want)
	}
	if got := durableBlocks(t, backend)[1]; !bytes.Equal(got, block(0x22)) {
		t.Fatalf("durable block 1 = %x, want 0x22", got)
	}
}

// AC-6: rejected inputs must not change device or backend state.
func TestRejectsUnalignedAndOutOfRange(t *testing.T) {
	device, backend := newTestDevice(t)

	badWrites := []struct {
		name  string
		start int
		data  []byte
	}{
		{"partial block", 0, []byte{0x01}},
		{"empty", 0, nil},
		{"negative start", -1, span(0x01)},
		{"past end", testBlocks, span(0x01)},
		{"straddles end", testBlocks - 1, span(0x01, 0x02)},
	}
	for _, tc := range badWrites {
		if err := device.Write(tc.start, tc.data); err == nil {
			t.Fatalf("%s: Write accepted, want error", tc.name)
		}
		if err := device.WriteDurable(tc.start, tc.data); err == nil {
			t.Fatalf("%s: WriteDurable accepted, want error", tc.name)
		}
	}

	if _, err := device.Read(0, 0); err == nil {
		t.Fatal("Read of zero blocks accepted, want error")
	}
	if _, err := device.Read(-1, 1); err == nil {
		t.Fatal("Read negative start accepted, want error")
	}
	if _, err := device.Read(testBlocks-1, 2); err == nil {
		t.Fatal("Read straddling end accepted, want error")
	}

	if got := backend.Trace(); len(got) != 0 {
		t.Fatalf("rejected ops reached backend: %v", got)
	}
	if got := device.DirtyBlocks(); len(got) != 0 {
		t.Fatalf("rejected ops dirtied cache: %v", got)
	}
}

// AC-7: a WriteDurable that overlaps a buffered dirty block supersedes the
// buffered value for that block while leaving the rest of the dirty run
// buffered.
func TestWriteDurableSupersedesBufferedValue(t *testing.T) {
	device, backend := newTestDevice(t)

	if err := device.Write(3, span(0x30, 0x31)); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if err := device.WriteDurable(3, span(0x99)); err != nil {
		t.Fatalf("WriteDurable: %v", err)
	}

	if got, want := writeRangeOps(backend.Trace()), []Op{{Kind: OpWriteRange, Start: 3, Count: 1}}; !reflect.DeepEqual(got, want) {
		t.Fatalf("write ranges = %v, want %v", got, want)
	}
	if got, want := device.DirtyBlocks(), []int{4}; !reflect.DeepEqual(got, want) {
		t.Fatalf("dirty = %v, want %v", got, want)
	}

	got, err := device.Read(3, 2)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if want := span(0x99, 0x31); !bytes.Equal(got, want) {
		t.Fatalf("read = %x, want %x", got, want)
	}

	device.PowerCycle()
	got, err = device.Read(3, 2)
	if err != nil {
		t.Fatalf("Read after power cycle: %v", err)
	}
	if want := span(0x99, 0x00); !bytes.Equal(got, want) {
		t.Fatalf("read after power cycle = %x, want %x (block 4 volatile)", got, want)
	}
}
