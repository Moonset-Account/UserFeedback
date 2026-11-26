package overlay

import "sync"

// Op identifies a backend block operation, passed to Hook.
type Op int

const (
	OpAlloc Op = iota
	OpPut
	OpGet
	OpFree
)

func (o Op) String() string {
	switch o {
	case OpAlloc:
		return "alloc"
	case OpPut:
		return "put"
	case OpGet:
		return "get"
	case OpFree:
		return "free"
	}
	return "?"
}

// Hook is invoked by MemBackend around every block operation. It runs
// WITHOUT the backend lock held, so it may block (pausing the caller,
// e.g. a copy-up in progress) and may return an error to inject a
// failure into that single operation.
type Hook func(op Op, id uint64) error

// MemBackend is a pausable in-memory block backend. Upper-layer file
// content lives in fixed-size blocks here; the block size also defines
// the granularity of copy-up, so tests can pause or fail a promotion
// halfway through.
type MemBackend struct {
	mu        sync.Mutex
	blockSize int
	blocks    map[uint64][]byte
	next      uint64
	hook      Hook
}

// NewMemBackend returns a backend with the given block size in bytes
// (values <= 0 select a default of 64).
func NewMemBackend(blockSize int) *MemBackend {
	if blockSize <= 0 {
		blockSize = 64
	}
	return &MemBackend{blockSize: blockSize, blocks: make(map[uint64][]byte)}
}

// BlockSize reports the backend block size.
func (m *MemBackend) BlockSize() int { return m.blockSize }

// SetHook installs (or clears, with nil) the operation hook.
func (m *MemBackend) SetHook(h Hook) {
	m.mu.Lock()
	m.hook = h
	m.mu.Unlock()
}

func (m *MemBackend) runHook(op Op, id uint64) error {
	m.mu.Lock()
	h := m.hook
	m.mu.Unlock()
	if h != nil {
		return h(op, id)
	}
	return nil
}

// Alloc reserves a fresh block id. The block has no content until Put.
func (m *MemBackend) Alloc() (uint64, error) {
	m.mu.Lock()
	m.next++
	id := m.next
	m.mu.Unlock()
	if err := m.runHook(OpAlloc, id); err != nil {
		return 0, err
	}
	return id, nil
}

// Put stores a copy of data under id, replacing any previous content.
func (m *MemBackend) Put(id uint64, data []byte) error {
	if err := m.runHook(OpPut, id); err != nil {
		return err
	}
	cp := append([]byte(nil), data...)
	m.mu.Lock()
	m.blocks[id] = cp
	m.mu.Unlock()
	return nil
}

// Get returns a copy of the block's content.
func (m *MemBackend) Get(id uint64) ([]byte, error) {
	if err := m.runHook(OpGet, id); err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	b, ok := m.blocks[id]
	if !ok {
		return nil, ErrNoBlock
	}
	return append([]byte(nil), b...), nil
}

// Free releases a block.
func (m *MemBackend) Free(id uint64) error {
	if err := m.runHook(OpFree, id); err != nil {
		return err
	}
	m.mu.Lock()
	delete(m.blocks, id)
	m.mu.Unlock()
	return nil
}

// LiveBlocks reports how many blocks currently hold content. Tests use
// it to verify copy-up failure leaves no garbage and that reclamation
// frees blocks exactly at last close.
func (m *MemBackend) LiveBlocks() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.blocks)
}
