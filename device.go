package blockdev

import (
	"fmt"
	"sort"
	"sync"
)

// Device is a virtual block device with a volatile write-back cache layered on
// top of a Backend. All accesses are expressed in whole blocks.
//
// The cache holds only blocks whose buffered value is newer than durable
// storage; every cached block is also dirty. A write to a cached block
// overwrites the buffered value in place, so a block is never queued twice and
// reads always observe the newest write. Once a block is persisted it leaves
// the cache, so reads fall through to durable content exactly.
type Device struct {
	mu        sync.Mutex
	backend   *Backend
	blockSize int
	blocks    int
	cache     map[int][]byte // newest unpersisted values
	dirty     map[int]struct{}
}

// Open attaches a fresh, empty cache to backend.
func Open(backend *Backend) *Device {
	return &Device{
		backend:   backend,
		blockSize: backend.BlockSize(),
		blocks:    backend.Blocks(),
		cache:     make(map[int][]byte),
		dirty:     make(map[int]struct{}),
	}
}

// BlockSize returns the size of one block in bytes.
func (d *Device) BlockSize() int { return d.blockSize }

// Blocks returns the device capacity in blocks.
func (d *Device) Blocks() int { return d.blocks }

func (d *Device) checkAligned(data []byte) (int, error) {
	if len(data) == 0 || len(data)%d.blockSize != 0 {
		return 0, ErrUnaligned
	}
	return len(data) / d.blockSize, nil
}

// Write buffers data at block start. Blocks are stored verbatim in the cache;
// a later write to the same block replaces this one. Nothing reaches the
// backend until Flush or WriteDurable.
func (d *Device) Write(start int, data []byte) error {
	count, err := d.checkAligned(data)
	if err != nil {
		return err
	}
	if start < 0 || start+count > d.blocks {
		return ErrOutOfRange
	}

	d.mu.Lock()
	defer d.mu.Unlock()
	for i := 0; i < count; i++ {
		block := make([]byte, d.blockSize)
		copy(block, data[i*d.blockSize:(i+1)*d.blockSize])
		d.cache[start+i] = block
		d.dirty[start+i] = struct{}{}
	}
	return nil
}

// Read returns a copy of the newest value of count blocks starting at start.
// Buffered values win over durable content.
func (d *Device) Read(start, count int) ([]byte, error) {
	if count <= 0 {
		return nil, ErrUnaligned
	}
	if start < 0 || start+count > d.blocks {
		return nil, ErrOutOfRange
	}

	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]byte, count*d.blockSize)
	for i := 0; i < count; i++ {
		index := start + i
		if block, ok := d.cache[index]; ok {
			copy(out[i*d.blockSize:], block)
			continue
		}
		block, err := d.backend.ReadBlock(index)
		if err != nil {
			return nil, err
		}
		copy(out[i*d.blockSize:], block)
	}
	return out, nil
}

// Flush persists every dirty block. Contiguous dirty blocks are coalesced into
// a single backend write range, and all ranges share one trailing barrier.
func (d *Device) Flush() error {
	d.mu.Lock()
	defer d.mu.Unlock()
	ranges := dirtyRanges(d.dirty)
	if err := d.persistRanges(ranges); err != nil {
		return err
	}
	for _, r := range ranges {
		for i := 0; i < r.count; i++ {
			delete(d.dirty, r.start+i)
			delete(d.cache, r.start+i)
		}
	}
	return nil
}

// WriteDurable persists exactly the blocks covered by this request, coalescing
// only that request's contiguous run, then drains those blocks from the cache
// as clean. Every other dirty block stays buffered and volatile. Buffered
// values inside the covered range are superseded by data.
func (d *Device) WriteDurable(start int, data []byte) error {
	count, err := d.checkAligned(data)
	if err != nil {
		return err
	}
	if start < 0 || start+count > d.blocks {
		return ErrOutOfRange
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	// Persist only this request's range: one contiguous write plus one
	// barrier. Other dirty blocks are deliberately excluded from the range.
	if err := d.backend.WriteRange(start, data); err != nil {
		return err
	}
	if err := d.backend.Barrier(); err != nil {
		return err
	}

	// Only the covered blocks become clean; their buffered copies are
	// superseded by the just-persisted data, so they leave the cache. All
	// other dirt stays.
	for i := 0; i < count; i++ {
		delete(d.dirty, start+i)
		delete(d.cache, start+i)
	}
	return nil
}

// PowerCycle discards all volatile state: buffered dirty blocks, cached reads,
// and the backend's unbarriered pending writes. The device is reopened with an
// empty cache over durable content only.
func (d *Device) PowerCycle() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.cache = make(map[int][]byte)
	d.dirty = make(map[int]struct{})
	d.backend.PowerCycle()
}

// DirtyBlocks returns the sorted block indexes currently held dirty in the
// cache. Useful for assertions and diagnostics.
func (d *Device) DirtyBlocks() []int {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]int, 0, len(d.dirty))
	for index := range d.dirty {
		out = append(out, index)
	}
	sort.Ints(out)
	return out
}

// barrieredRange is one coalesced contiguous run sent to the backend.
type barrieredRange struct {
	start int
	count int
}

// persistRanges writes each merged range and issues a single barrier if any
// range was written. Ranges must be disjoint and sorted by start.
func (d *Device) persistRanges(ranges []barrieredRange) error {
	if len(ranges) == 0 {
		return nil
	}
	for _, r := range ranges {
		data := make([]byte, r.count*d.blockSize)
		for i := 0; i < r.count; i++ {
			block, ok := d.cache[r.start+i]
			if !ok {
				return fmt.Errorf("blockdev: dirty block %d missing from cache", r.start+i)
			}
			copy(data[i*d.blockSize:], block)
		}
		if err := d.backend.WriteRange(r.start, data); err != nil {
			return err
		}
	}
	return d.backend.Barrier()
}

// dirtyRanges merges a dirty block set into maximal contiguous runs sorted by
// start block.
func dirtyRanges(dirty map[int]struct{}) []barrieredRange {
	if len(dirty) == 0 {
		return nil
	}
	indexes := make([]int, 0, len(dirty))
	for index := range dirty {
		indexes = append(indexes, index)
	}
	sort.Ints(indexes)

	ranges := make([]barrieredRange, 0, len(indexes))
	for _, index := range indexes {
		if n := len(ranges); n > 0 && ranges[n-1].start+ranges[n-1].count == index {
			ranges[n-1].count++
			continue
		}
		ranges = append(ranges, barrieredRange{start: index, count: 1})
	}
	return ranges
}
