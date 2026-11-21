package raidarray

import "fmt"

// Member is an in-memory block device that backs one slot of the array.
//
// A member holds one fixed-size block per stripe. The array is the only
// component that decides which slots are available; a Member itself never
// fails, which keeps fault injection deterministic in tests.
type Member struct {
	blockSize int
	stripes   int
	blocks    [][]byte
}

// NewMember returns a zero-filled member able to store stripes blocks of
// blockSize bytes each.
func NewMember(blockSize, stripes int) *Member {
	blocks := make([][]byte, stripes)
	for i := range blocks {
		blocks[i] = make([]byte, blockSize)
	}
	return &Member{blockSize: blockSize, stripes: stripes, blocks: blocks}
}

// BlockSize reports the size of one block on this member.
func (m *Member) BlockSize() int { return m.blockSize }

// Stripes reports the number of blocks this member can hold.
func (m *Member) Stripes() int { return m.stripes }

// Block returns a copy of the raw block at index, for diagnostics and tests.
// It returns nil when index is out of range.
func (m *Member) Block(index int) []byte {
	if index < 0 || index >= m.stripes {
		return nil
	}
	out := make([]byte, m.blockSize)
	copy(out, m.blocks[index])
	return out
}

func (m *Member) readBlock(index int) ([]byte, error) {
	if index < 0 || index >= m.stripes {
		return nil, fmt.Errorf("raidarray: member block index %d out of range [0,%d)", index, m.stripes)
	}
	out := make([]byte, m.blockSize)
	copy(out, m.blocks[index])
	return out, nil
}

func (m *Member) writeBlock(index int, src []byte) error {
	if index < 0 || index >= m.stripes {
		return fmt.Errorf("raidarray: member block index %d out of range [0,%d)", index, m.stripes)
	}
	if len(src) != m.blockSize {
		return fmt.Errorf("raidarray: block write has %d bytes, want %d", len(src), m.blockSize)
	}
	copy(m.blocks[index], src)
	return nil
}
