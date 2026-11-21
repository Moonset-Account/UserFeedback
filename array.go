// Package raidarray implements a small in-memory four-member array with XOR
// parity.
//
// Every stripe stores three equal-length data blocks followed by one
// byte-wise XOR parity block.  The logical volume is laid out data block by
// data block inside a stripe and then advances to the next stripe.  Member 0,
// 1 and 2 carry data blocks 0, 1 and 2; member 3 carries the parity block.
//
// The array keeps working with exactly one offline member: reads reconstruct
// the missing block from parity, writes update the surviving blocks and the
// parity (or retain the missing block content in an in-memory write log until
// the member is replaced).  A replacement member is filled stripe by stripe
// through RebuildStep, and callers may interleave ordinary volume writes
// between any two steps.  A second member loss before the rebuild finishes
// puts the array into the write-stopped state.
package raidarray

import (
	"errors"
	"fmt"
	"io"
	"sort"
)

const (
	// DataBlocks is the number of data blocks in every stripe.
	DataBlocks = 3
	// ParityIndex is the member slot that stores the XOR parity block.
	ParityIndex = 3
	// MemberCount is the fixed number of members in the array.
	MemberCount = 4
)

var (
	// ErrStopped is returned by writes once two or more members are missing.
	ErrStopped = errors.New("raidarray: write-stopped, two or more members are missing")
	// ErrUnrecoverable is returned when a stripe has more than one missing
	// block and therefore cannot be reconstructed.
	ErrUnrecoverable = errors.New("raidarray: stripe has more than one missing block")
	// ErrNoRebuild is returned when a rebuild step is requested while no
	// single-member rebuild is in progress.
	ErrNoRebuild = errors.New("raidarray: no rebuild in progress")
)

// MissingMembersError carries the offline member slots involved in a failed
// read or write.  It may wrap ErrStopped or ErrUnrecoverable.
type MissingMembersError struct {
	Op      string
	Missing []int
	Cause   error
}

// Error implements the error interface.
func (e *MissingMembersError) Error() string {
	op := e.Op
	if op == "" {
		op = "operation"
	}
	msg := fmt.Sprintf("raidarray: %s blocked, missing members %v", op, e.Missing)
	if e.Cause != nil {
		msg += ": " + e.Cause.Error()
	}
	return msg
}

// Unwrap exposes the sentinel cause so errors.Is works.
func (e *MissingMembersError) Unwrap() error { return e.Cause }

// RebuildProgress describes the stripe-by-stripe rebuild of one member.
type RebuildProgress struct {
	Target int  // member slot being rebuilt, or -1 when none is active
	Done   int  // number of stripes already rebuilt
	Total  int  // total number of stripes in the volume
	Active bool // true while more stripes still need rebuilding
}

// Array is a four-member XOR-parity volume over in-memory members.
//
// An Array is not safe for concurrent use.
type Array struct {
	blockSize int
	stripes   int
	members   []*Member

	// failed holds the member slots that are currently offline.  While a
	// replacement is being rebuilt its slot stays in failed until the last
	// stripe is done.
	failed map[int]bool

	// overlap is the in-memory write log for the offline member's data block.
	// It is keyed by stripe and holds the latest full block content written
	// while that member was offline.
	overlay map[int][]byte

	rebuilding bool
	target     int
	cursor     int
	rebuilt    []bool
}

// NewArray validates the member geometry and returns a healthy array.  The
// members slice must contain MemberCount members of identical geometry.
func NewArray(blockSize, stripes int, members []*Member) (*Array, error) {
	if blockSize <= 0 {
		return nil, fmt.Errorf("raidarray: block size must be positive, got %d", blockSize)
	}
	if stripes <= 0 {
		return nil, fmt.Errorf("raidarray: stripe count must be positive, got %d", stripes)
	}
	if len(members) != MemberCount {
		return nil, fmt.Errorf("raidarray: need %d members, got %d", MemberCount, len(members))
	}
	seen := make(map[*Member]bool, MemberCount)
	for i, m := range members {
		if m == nil {
			return nil, fmt.Errorf("raidarray: member %d is nil", i)
		}
		if seen[m] {
			return nil, fmt.Errorf("raidarray: member %d is a duplicate pointer", i)
		}
		seen[m] = true
		if m.blockSize != blockSize || m.stripes != stripes {
			return nil, fmt.Errorf("raidarray: member %d geometry (%d,%d) does not match array (%d,%d)",
				i, m.blockSize, m.stripes, blockSize, stripes)
		}
	}
	return &Array{
		blockSize: blockSize,
		stripes:   stripes,
		members:   append([]*Member(nil), members...),
		failed:    make(map[int]bool),
		target:    -1,
	}, nil
}

// BlockSize reports the block size of the volume.
func (a *Array) BlockSize() int { return a.blockSize }

// Stripes reports how many stripes the volume contains.
func (a *Array) Stripes() int { return a.stripes }

// Cap reports the logical size of the volume in bytes.
func (a *Array) Cap() int64 {
	return int64(a.stripes) * int64(DataBlocks) * int64(a.blockSize)
}

func (a *Array) stripeBytes() int { return DataBlocks * a.blockSize }

// Missing returns the sorted member slots that are offline.  A slot being
// rebuilt is still reported until the rebuild completes.
func (a *Array) Missing() []int {
	var out []int
	for i := 0; i < MemberCount; i++ {
		if a.failed[i] {
			out = append(out, i)
		}
	}
	sort.Ints(out)
	return out
}

// Writable reports whether writes are currently accepted.  The array is
// writable with zero or one missing member and write-stopped with two or more.
func (a *Array) Writable() bool { return len(a.failed) < 2 }

// Healthy reports whether all members are present and no rebuild is pending.
func (a *Array) Healthy() bool {
	return len(a.failed) == 0 && !a.rebuilding
}

// RebuildStatus returns the current rebuild progress.  Target is -1 when no
// rebuild has ever been started.  If a second member is lost while a rebuild
// is in progress, Active turns false with Done < Total and Target still naming
// the replacement slot, which records where the interrupted rebuild stopped.
func (a *Array) RebuildStatus() RebuildProgress {
	return RebuildProgress{Target: a.target, Done: a.cursor, Total: a.stripes, Active: a.rebuilding}
}

// Fail takes one member offline.  A second failure before a rebuild finishes
// stops the array from accepting writes; Missing still reports both slots.
func (a *Array) Fail(index int) error {
	if err := a.checkIndex(index); err != nil {
		return err
	}
	if a.failed[index] {
		return fmt.Errorf("raidarray: member %d is already offline", index)
	}
	a.failed[index] = true
	if a.rebuilding {
		// The rebuild can no longer make progress.  Keep target/rebuilt so
		// stripes that were already restored remain readable.
		a.rebuilding = false
	}
	return nil
}

// Replace swaps an offline member for a fresh replacement and starts a
// stripe-by-stripe rebuild.  Writes to the volume remain allowed between
// RebuildStep calls.  Replace requires exactly one missing member and no
// rebuild already in progress.
func (a *Array) Replace(index int, replacement *Member) error {
	if err := a.checkIndex(index); err != nil {
		return err
	}
	if replacement == nil {
		return fmt.Errorf("raidarray: replacement for member %d is nil", index)
	}
	if replacement.blockSize != a.blockSize || replacement.stripes != a.stripes {
		return fmt.Errorf("raidarray: replacement geometry (%d,%d) does not match array (%d,%d)",
			replacement.blockSize, replacement.stripes, a.blockSize, a.stripes)
	}
	if !a.failed[index] {
		return fmt.Errorf("raidarray: member %d is not offline", index)
	}
	if len(a.failed) != 1 {
		return fmt.Errorf("raidarray: cannot rebuild with %d members offline: %w", len(a.failed), ErrStopped)
	}
	if a.rebuilding {
		return fmt.Errorf("raidarray: rebuild already in progress")
	}

	a.members[index] = replacement
	a.target = index
	a.cursor = 0
	a.rebuilt = make([]bool, a.stripes)
	a.rebuilding = true
	return nil
}

// RebuildStep rebuilds exactly one more stripe of the replacement member and
// reports the resulting progress.  When the last stripe is restored the member
// slot becomes healthy and Active turns false.
func (a *Array) RebuildStep() (RebuildProgress, error) {
	if !a.rebuilding {
		return RebuildProgress{Target: -1, Total: a.stripes, Done: a.cursor}, ErrNoRebuild
	}
	if a.cursor >= a.stripes {
		return a.finishRebuild(), nil
	}

	s := a.cursor
	block, err := a.rebuildBlock(s)
	if err != nil {
		return a.RebuildStatus(), err
	}
	if err := a.members[a.target].writeBlock(s, block); err != nil {
		return a.RebuildStatus(), err
	}
	a.rebuilt[s] = true
	delete(a.overlay, s)
	a.cursor++

	if a.cursor == a.stripes {
		return a.finishRebuild(), nil
	}
	return a.RebuildStatus(), nil
}

// RebuildAll runs RebuildStep until the replacement is fully rebuilt.  It is a
// convenience for callers that do not need to interleave writes.
func (a *Array) RebuildAll() error {
	steps := 0
	for {
		p, err := a.RebuildStep()
		if err != nil {
			return err
		}
		steps++
		if !p.Active {
			return nil
		}
		if steps > a.stripes+1 {
			return fmt.Errorf("raidarray: rebuild did not converge after %d steps", steps)
		}
	}
}

// rebuildBlock computes the block that belongs on the replacement member for
// one stripe.  A write that arrived while degraded takes precedence over
// reconstruction so the replacement receives the latest bytes.
func (a *Array) rebuildBlock(s int) ([]byte, error) {
	if a.target == ParityIndex {
		parity := make([]byte, a.blockSize)
		for i := 0; i < DataBlocks; i++ {
			b, err := a.members[i].readBlock(s)
			if err != nil {
				return nil, err
			}
			xorInto(parity, b)
		}
		return parity, nil
	}

	if buf, ok := a.overlay[s]; ok {
		return cloneBlock(buf), nil
	}

	parity, err := a.members[ParityIndex].readBlock(s)
	if err != nil {
		return nil, err
	}
	block := parity
	for i := 0; i < DataBlocks; i++ {
		if i == a.target {
			continue
		}
		b, err := a.members[i].readBlock(s)
		if err != nil {
			return nil, err
		}
		xorInto(block, b)
	}
	return block, nil
}

func (a *Array) finishRebuild() RebuildProgress {
	target := a.target
	delete(a.failed, target)
	a.rebuilding = false
	a.rebuilt = nil
	a.overlay = nil
	a.target = -1
	a.cursor = 0
	return RebuildProgress{Target: target, Done: a.stripes, Total: a.stripes, Active: false}
}

// ReadAt reads len(p) bytes starting at logical offset off.  It follows the
// usual io.ReaderAt convention: a short read is reported with io.EOF.
func (a *Array) ReadAt(p []byte, off int64) (int, error) {
	if off < 0 {
		return 0, fmt.Errorf("raidarray: negative read offset %d", off)
	}
	if len(p) == 0 {
		return 0, nil
	}
	limit := a.Cap()
	if off >= limit {
		return 0, io.EOF
	}
	want := len(p)
	if rem := limit - off; int64(want) > rem {
		want = int(rem)
	}

	pos := 0
	cur := off
	for pos < want {
		s := int(cur / int64(a.stripeBytes()))
		inStripe := int(cur % int64(a.stripeBytes()))
		chunk := a.stripeBytes() - inStripe
		if chunk > want-pos {
			chunk = want - pos
		}
		data, err := a.stripeData(s)
		if err != nil {
			return pos, err
		}
		buf := make([]byte, a.stripeBytes())
		for i := 0; i < DataBlocks; i++ {
			copy(buf[i*a.blockSize:], data[i])
		}
		copy(p[pos:pos+chunk], buf[inStripe:inStripe+chunk])
		pos += chunk
		cur += int64(chunk)
	}
	if pos < len(p) {
		return pos, io.EOF
	}
	return pos, nil
}

// WriteAt writes len(p) bytes starting at logical offset off.  Writes are
// accepted while at most one member is missing, including partial writes that
// touch only a few bytes of the missing member's data block.  A write that
// reaches past the end of the volume writes the fitting prefix and returns
// io.EOF.
func (a *Array) WriteAt(p []byte, off int64) (int, error) {
	if off < 0 {
		return 0, fmt.Errorf("raidarray: negative write offset %d", off)
	}
	if len(p) == 0 {
		return 0, nil
	}
	if !a.Writable() {
		return 0, &MissingMembersError{Op: "write", Missing: a.Missing(), Cause: ErrStopped}
	}
	limit := a.Cap()
	if off >= limit {
		return 0, io.EOF
	}
	want := len(p)
	if rem := limit - off; int64(want) > rem {
		want = int(rem)
	}

	pos := 0
	cur := off
	for pos < want {
		s := int(cur / int64(a.stripeBytes()))
		inStripe := int(cur % int64(a.stripeBytes()))
		chunk := a.stripeBytes() - inStripe
		if chunk > want-pos {
			chunk = want - pos
		}
		if err := a.writeStripe(s, inStripe, p[pos:pos+chunk]); err != nil {
			return pos, err
		}
		pos += chunk
		cur += int64(chunk)
	}
	if pos < len(p) {
		return pos, io.EOF
	}
	return pos, nil
}

// stripeData returns the three current data blocks of a stripe, rebuilding a
// missing data block from parity when necessary.  The returned slices are
// owned by the caller.
func (a *Array) stripeData(s int) ([DataBlocks][]byte, error) {
	var data [DataBlocks][]byte
	missing := a.missingInStripe(s)
	if len(missing) > 1 {
		return data, &MissingMembersError{Op: "read", Missing: missing, Cause: ErrUnrecoverable}
	}

	if len(missing) == 1 && missing[0] < DataBlocks {
		m := missing[0]
		if buf, ok := a.overlay[s]; ok {
			data[m] = cloneBlock(buf)
		} else {
			parity, err := a.members[ParityIndex].readBlock(s)
			if err != nil {
				return data, err
			}
			recon := parity
			for i := 0; i < DataBlocks; i++ {
				if i == m {
					continue
				}
				b, err := a.members[i].readBlock(s)
				if err != nil {
					return data, err
				}
				xorInto(recon, b)
			}
			data[m] = recon
		}
	}

	for i := 0; i < DataBlocks; i++ {
		if data[i] != nil {
			continue
		}
		b, err := a.members[i].readBlock(s)
		if err != nil {
			return data, err
		}
		data[i] = b
	}
	return data, nil
}

// writeStripe performs a read-modify-write of one stripe for the byte range
// [inStripe, inStripe+len(src)).
func (a *Array) writeStripe(s, inStripe int, src []byte) error {
	data, err := a.stripeData(s)
	if err != nil {
		return err
	}
	for k := 0; k < len(src); k++ {
		abs := inStripe + k
		blk := abs / a.blockSize
		off := abs % a.blockSize
		data[blk][off] = src[k]
	}

	parity := make([]byte, a.blockSize)
	for i := 0; i < DataBlocks; i++ {
		xorInto(parity, data[i])
	}

	missing := a.missingInStripe(s)
	switch len(missing) {
	case 0:
		for i := 0; i < DataBlocks; i++ {
			if err := a.members[i].writeBlock(s, data[i]); err != nil {
				return err
			}
		}
		return a.members[ParityIndex].writeBlock(s, parity)
	case 1:
		if missing[0] == ParityIndex {
			// The parity member is offline: only the data blocks can be
			// updated.  The parity is recomputed when it is rebuilt.
			for i := 0; i < DataBlocks; i++ {
				if err := a.members[i].writeBlock(s, data[i]); err != nil {
					return err
				}
			}
			return nil
		}
		m := missing[0]
		if a.overlay == nil {
			a.overlay = make(map[int][]byte)
		}
		a.overlay[s] = cloneBlock(data[m])
		for i := 0; i < DataBlocks; i++ {
			if i == m {
				continue
			}
			if err := a.members[i].writeBlock(s, data[i]); err != nil {
				return err
			}
		}
		return a.members[ParityIndex].writeBlock(s, parity)
	default:
		return &MissingMembersError{Op: "write", Missing: a.Missing(), Cause: ErrStopped}
	}
}

func (a *Array) missingInStripe(s int) []int {
	missing := make([]int, 0, MemberCount)
	for i := 0; i < MemberCount; i++ {
		if !a.blockAvailable(s, i) {
			missing = append(missing, i)
		}
	}
	return missing
}

// blockAvailable reports whether member i holds a valid block for stripe s.
// A replacement member only becomes readable stripe by stripe as the rebuild
// advances.
func (a *Array) blockAvailable(s, i int) bool {
	if !a.failed[i] {
		return true
	}
	if i == a.target && a.rebuilt != nil && s >= 0 && s < len(a.rebuilt) && a.rebuilt[s] {
		return true
	}
	return false
}

func (a *Array) checkIndex(index int) error {
	if index < 0 || index >= MemberCount {
		return fmt.Errorf("raidarray: member index %d out of range [0,%d)", index, MemberCount)
	}
	return nil
}

func cloneBlock(b []byte) []byte {
	out := make([]byte, len(b))
	copy(out, b)
	return out
}

func xorInto(dst, src []byte) {
	for i := range dst {
		dst[i] ^= src[i]
	}
}
