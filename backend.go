// Package blockdev implements a virtual block device with a volatile
// write-back cache in front of a backend that only understands contiguous
// block writes plus a single persistence barrier.
//
// Semantics:
//
//   - Write buffers data in the cache. A later write to the same block
//     replaces the earlier one; reads observe the newest value.
//   - Flush persists every dirty block, coalescing contiguous dirty blocks
//     into contiguous backend writes, followed by one barrier.
//   - WriteDurable persists exactly the blocks covered by that request and
//     leaves every other dirty block buffered.
//   - PowerCycle drops all volatile state (device cache and backend pending
//     writes), so only content that passed a barrier survives.
package blockdev

import (
	"errors"
	"fmt"
	"sort"
	"sync"
)

// Sentinel errors returned by backend and device operations.
var (
	// ErrUnaligned reports a buffer whose length is not a whole number of
	// blocks, or a zero-length/partial-block access where a full block range
	// is required.
	ErrUnaligned = errors.New("blockdev: data is not block aligned")
	// ErrOutOfRange reports an access outside the device geometry.
	ErrOutOfRange = errors.New("blockdev: access outside device")
	// ErrInvalidGeometry reports a non-positive block size or block count.
	ErrInvalidGeometry = errors.New("blockdev: invalid geometry")
)

// OpKind identifies a backend operation recorded in the trace.
type OpKind string

const (
	// OpWriteRange is a contiguous multi-block write accepted by the backend.
	OpWriteRange OpKind = "write-range"
	// OpBarrier is a persistence barrier.
	OpBarrier OpKind = "barrier"
	// OpPowerCycle is a simulated power loss.
	OpPowerCycle OpKind = "power-cycle"
)

// Op is one entry of the backend operation trace. Start and Count are block
// coordinates and are only meaningful for OpWriteRange.
type Op struct {
	Kind  OpKind
	Start int
	Count int
}

// String renders an operation for human-readable traces.
func (o Op) String() string {
	switch o.Kind {
	case OpWriteRange:
		return fmt.Sprintf("write-range start=%d count=%d", o.Start, o.Count)
	case OpBarrier:
		return "barrier"
	case OpPowerCycle:
		return "power-cycle"
	default:
		return string(o.Kind)
	}
}

// Backend is a durable block store that accepts only contiguous block writes
// and persists them on Barrier. Writes staged after the last barrier live in
// volatile pending state and are discarded by PowerCycle.
type Backend struct {
	mu        sync.Mutex
	blockSize int
	durable   [][]byte
	pending   map[int][]byte
	trace     []Op
	barriers  int
}

// NewBackend creates a zero-filled backend with the given geometry.
func NewBackend(blocks, blockSize int) (*Backend, error) {
	if blocks <= 0 || blockSize <= 0 {
		return nil, ErrInvalidGeometry
	}
	durable := make([][]byte, blocks)
	for i := range durable {
		durable[i] = make([]byte, blockSize)
	}
	return &Backend{
		blockSize: blockSize,
		durable:   durable,
		pending:   make(map[int][]byte),
	}, nil
}

// BlockSize returns the size of one block in bytes.
func (b *Backend) BlockSize() int { return b.blockSize }

// Blocks returns the number of blocks in the device.
func (b *Backend) Blocks() int { return len(b.durable) }

// WriteRange stages data at contiguous blocks starting at start. The data is
// visible to a later Barrier but not durable until Barrier is called.
func (b *Backend) WriteRange(start int, data []byte) error {
	if len(data) == 0 || len(data)%b.blockSize != 0 {
		return ErrUnaligned
	}
	count := len(data) / b.blockSize
	if start < 0 || start+count > len(b.durable) {
		return ErrOutOfRange
	}

	b.mu.Lock()
	defer b.mu.Unlock()
	for i := 0; i < count; i++ {
		block := make([]byte, b.blockSize)
		copy(block, data[i*b.blockSize:(i+1)*b.blockSize])
		b.pending[start+i] = block
	}
	b.trace = append(b.trace, Op{Kind: OpWriteRange, Start: start, Count: count})
	return nil
}

// Barrier commits every pending write to durable storage. It is the only
// operation that makes staged writes survive a PowerCycle.
func (b *Backend) Barrier() error {
	b.mu.Lock()
	defer b.mu.Unlock()
	for index, data := range b.pending {
		b.durable[index] = data
	}
	b.pending = make(map[int][]byte)
	b.barriers++
	b.trace = append(b.trace, Op{Kind: OpBarrier})
	return nil
}

// PowerCycle discards volatile pending writes, simulating an abrupt power
// loss. Durable content is untouched.
func (b *Backend) PowerCycle() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.pending = make(map[int][]byte)
	b.trace = append(b.trace, Op{Kind: OpPowerCycle})
}

// ReadBlock returns a copy of the durable content of one block.
func (b *Backend) ReadBlock(index int) ([]byte, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if index < 0 || index >= len(b.durable) {
		return nil, ErrOutOfRange
	}
	out := make([]byte, b.blockSize)
	copy(out, b.durable[index])
	return out, nil
}

// Durable returns a copy of every durable block.
func (b *Backend) Durable() [][]byte {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([][]byte, len(b.durable))
	for i := range b.durable {
		out[i] = make([]byte, b.blockSize)
		copy(out[i], b.durable[i])
	}
	return out
}

// Trace returns a copy of the recorded backend operations.
func (b *Backend) Trace() []Op {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]Op, len(b.trace))
	copy(out, b.trace)
	return out
}

// ResetTrace clears the operation trace. Durable and pending state are kept.
func (b *Backend) ResetTrace() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.trace = nil
}

// Barriers reports how many barriers have been issued since creation.
func (b *Backend) Barriers() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.barriers
}

// PendingBlocks reports the sorted block indexes staged but not yet barriered.
func (b *Backend) PendingBlocks() []int {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]int, 0, len(b.pending))
	for index := range b.pending {
		out = append(out, index)
	}
	sort.Ints(out)
	return out
}
