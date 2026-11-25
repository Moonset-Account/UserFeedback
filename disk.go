package gptrepair

import (
	"fmt"
	"os"
	"sync"
)

// SectorDisk is the minimal block-device contract required by the repair
// routine. Reads and writes always address complete sectors; partial sector
// access is rejected.
type SectorDisk interface {
	// SectorSize returns the logical sector size in bytes (512, 4096, ...).
	SectorSize() int
	// TotalSectors returns the currently addressable number of sectors.
	TotalSectors() uint64
	// ReadSectors reads count sectors starting at lba into dst. dst must be
	// at least count*SectorSize() bytes.
	ReadSectors(lba, count uint64, dst []byte) error
	// WriteSectors writes one or more complete sectors from src at lba.
	WriteSectors(lba uint64, src []byte) error
}

// ResizableDisk is implemented by backends that can change their capacity.
// Grow and shrink never move user bytes: a grow leaves the new tail
// uninitialized (zero), a shrink drops sectors beyond the new end.
type ResizableDisk interface {
	ResizeSectors(totalSectors uint64) error
}

// MemoryDisk is an in-memory SectorDisk, optionally resizable. It is intended
// for tests and synthetic images.
type MemoryDisk struct {
	mu        sync.Mutex
	blockSize int
	blocks    uint64
	data      []byte
	resizable bool
}

// NewMemoryDisk allocates a zeroed disk of totalSectors sectors. When
// resizable is true the disk also implements ResizableDisk.
func NewMemoryDisk(totalSectors uint64, blockSize int, resizable bool) (*MemoryDisk, error) {
	if blockSize < blockSizeMin || blockSize > blockSizeMax || blockSize&(blockSize-1) != 0 {
		return nil, fmt.Errorf("%w: sector size %d", ErrArgument, blockSize)
	}
	if totalSectors == 0 {
		return nil, fmt.Errorf("%w: zero-length disk", ErrArgument)
	}
	size, ok := mulOverflows(totalSectors, uint64(blockSize))
	if !ok {
		return nil, fmt.Errorf("%w: disk size overflow", ErrGeometry)
	}
	return &MemoryDisk{
		blockSize: blockSize,
		blocks:    totalSectors,
		data:      make([]byte, size),
		resizable: resizable,
	}, nil
}

func mulOverflows(a, b uint64) (uint64, bool) {
	if a != 0 && b > (^uint64(0))/a {
		return 0, false
	}
	return a * b, true
}

func (d *MemoryDisk) SectorSize() int { return d.blockSize }

func (d *MemoryDisk) TotalSectors() uint64 {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.blocks
}

// Bytes returns the raw backing bytes. Callers must not modify the slice while
// repair operations are in flight.
func (d *MemoryDisk) Bytes() []byte {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.data
}

// Clone returns an independent deep copy, preserving resize capability.
func (d *MemoryDisk) Clone() *MemoryDisk {
	d.mu.Lock()
	defer d.mu.Unlock()
	cp := make([]byte, len(d.data))
	copy(cp, d.data)
	return &MemoryDisk{blockSize: d.blockSize, blocks: d.blocks, data: cp, resizable: d.resizable}
}

func (d *MemoryDisk) ReadSectors(lba, count uint64, dst []byte) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if count == 0 {
		return fmt.Errorf("%w: zero sector count", ErrArgument)
	}
	end, ok := addOverflows(lba, count)
	if !ok || end > d.blocks {
		return fmt.Errorf("read %d sectors at LBA %d: out of range (disk has %d sectors)", count, lba, d.blocks)
	}
	need := int(count) * d.blockSize
	if len(dst) < need {
		return fmt.Errorf("%w: destination buffer too small", ErrArgument)
	}
	off := int(lba) * d.blockSize
	copy(dst[:need], d.data[off:off+need])
	return nil
}

func addOverflows(a, b uint64) (uint64, bool) {
	if a > ^uint64(0)-b {
		return 0, false
	}
	return a + b, true
}

func (d *MemoryDisk) WriteSectors(lba uint64, src []byte) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(src) == 0 || len(src)%d.blockSize != 0 {
		return fmt.Errorf("%w: write must be whole sectors", ErrArgument)
	}
	count := uint64(len(src) / d.blockSize)
	end, ok := addOverflows(lba, count)
	if !ok || end > d.blocks {
		return fmt.Errorf("write %d sectors at LBA %d: out of range (disk has %d sectors)", count, lba, d.blocks)
	}
	off := int(lba) * d.blockSize
	copy(d.data[off:off+len(src)], src)
	return nil
}

// ResizeSectors grows or shrinks the in-memory disk.
func (d *MemoryDisk) ResizeSectors(totalSectors uint64) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if !d.resizable {
		return fmt.Errorf("gptrepair: backend does not support capacity changes")
	}
	if totalSectors == 0 {
		return fmt.Errorf("%w: zero-length disk", ErrArgument)
	}
	size, ok := mulOverflows(totalSectors, uint64(d.blockSize))
	if !ok {
		return fmt.Errorf("%w: disk size overflow", ErrGeometry)
	}
	next := make([]byte, size)
	copy(next, d.data) // drops tail on shrink, leaves zeros on grow
	d.data = next
	d.blocks = totalSectors
	return nil
}

// FileDisk is a SectorDisk backed by a regular file, supporting growth and
// truncation via ResizeSectors.
type FileDisk struct {
	mu        sync.Mutex
	f         *os.File
	blockSize int
	blocks    uint64
}

// OpenFileDisk opens (or creates) an image file. The file must currently hold
// exactly totalSectors sectors; pass the size recorded by the virtual disk.
func OpenFileDisk(path string, blockSize int, totalSectors uint64) (*FileDisk, error) {
	if blockSize < blockSizeMin || blockSize > blockSizeMax || blockSize&(blockSize-1) != 0 {
		return nil, fmt.Errorf("%w: sector size %d", ErrArgument, blockSize)
	}
	if totalSectors == 0 {
		return nil, fmt.Errorf("%w: zero-length disk", ErrArgument)
	}
	size, ok := mulOverflows(totalSectors, uint64(blockSize))
	if !ok {
		return nil, fmt.Errorf("%w: disk size overflow", ErrGeometry)
	}
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	st, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, err
	}
	if st.Size() != int64(size) {
		f.Close()
		return nil, fmt.Errorf("%w: file size %d does not match %d sectors of %d bytes",
			ErrArgument, st.Size(), totalSectors, blockSize)
	}
	return &FileDisk{f: f, blockSize: blockSize, blocks: totalSectors}, nil
}

// Close releases the underlying file.
func (d *FileDisk) Close() error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.f == nil {
		return nil
	}
	err := d.f.Close()
	d.f = nil
	return err
}

func (d *FileDisk) SectorSize() int { return d.blockSize }

func (d *FileDisk) TotalSectors() uint64 {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.blocks
}

func (d *FileDisk) ReadSectors(lba, count uint64, dst []byte) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if count == 0 {
		return fmt.Errorf("%w: zero sector count", ErrArgument)
	}
	end, ok := addOverflows(lba, count)
	if !ok || end > d.blocks {
		return fmt.Errorf("read %d sectors at LBA %d: out of range (disk has %d sectors)", count, lba, d.blocks)
	}
	need := int(count) * d.blockSize
	if len(dst) < need {
		return fmt.Errorf("%w: destination buffer too small", ErrArgument)
	}
	n, err := d.f.ReadAt(dst[:need], int64(lba)*int64(d.blockSize))
	if err != nil {
		return err
	}
	if n != need {
		return fmt.Errorf("short read: got %d bytes, want %d", n, need)
	}
	return nil
}

func (d *FileDisk) WriteSectors(lba uint64, src []byte) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(src) == 0 || len(src)%d.blockSize != 0 {
		return fmt.Errorf("%w: write must be whole sectors", ErrArgument)
	}
	count := uint64(len(src) / d.blockSize)
	end, ok := addOverflows(lba, count)
	if !ok || end > d.blocks {
		return fmt.Errorf("write %d sectors at LBA %d: out of range (disk has %d sectors)", count, lba, d.blocks)
	}
	if _, err := d.f.WriteAt(src, int64(lba)*int64(d.blockSize)); err != nil {
		return err
	}
	return d.f.Sync()
}

// ResizeSectors grows or truncates the image file.
func (d *FileDisk) ResizeSectors(totalSectors uint64) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if totalSectors == 0 {
		return fmt.Errorf("%w: zero-length disk", ErrArgument)
	}
	size, ok := mulOverflows(totalSectors, uint64(d.blockSize))
	if !ok {
		return fmt.Errorf("%w: disk size overflow", ErrGeometry)
	}
	if err := d.f.Truncate(int64(size)); err != nil {
		return err
	}
	d.blocks = totalSectors
	return d.f.Sync()
}
