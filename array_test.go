package raidarray

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"testing"
)

// newTestArray builds a 4-member array with the given geometry and returns it
// together with its members in slot order.
func newTestArray(t *testing.T, blockSize, stripes int) (*Array, []*Member) {
	t.Helper()
	members := make([]*Member, MemberCount)
	for i := range members {
		members[i] = NewMember(blockSize, stripes)
	}
	a, err := NewArray(blockSize, stripes, members)
	if err != nil {
		t.Fatalf("NewArray: %v", err)
	}
	return a, members
}

// cloneMembers copies every block of every member into fresh members so a
// test can drop different slots without disturbing the source array.
func cloneMembers(t *testing.T, members []*Member) []*Member {
	t.Helper()
	out := make([]*Member, len(members))
	for i, m := range members {
		clone := NewMember(m.BlockSize(), m.Stripes())
		for s := 0; s < m.Stripes(); s++ {
			if err := clone.writeBlock(s, m.Block(s)); err != nil {
				t.Fatalf("clone member %d stripe %d: %v", i, s, err)
			}
		}
		out[i] = clone
	}
	return out
}

// fillPattern writes n bytes of a deterministic, position-dependent pattern
// starting at off and applies the same bytes to the model.
func fillPattern(t *testing.T, a *Array, model []byte, off, n int) {
	t.Helper()
	buf := make([]byte, n)
	for i := range buf {
		buf[i] = byte((off + i*7 + 13) % 251)
	}
	written, err := a.WriteAt(buf, int64(off))
	if err != nil {
		t.Fatalf("WriteAt(off=%d,n=%d): %v", off, n, err)
	}
	if written != n {
		t.Fatalf("WriteAt(off=%d,n=%d) wrote %d bytes", off, n, written)
	}
	copy(model[off:off+n], buf)
}

// readAll reads the whole logical volume through the array.
func readAll(t *testing.T, a *Array) []byte {
	t.Helper()
	buf := make([]byte, a.Cap())
	n, err := a.ReadAt(buf, 0)
	if err != nil && !errors.Is(err, io.EOF) {
		t.Fatalf("ReadAt whole volume: %v", err)
	}
	if n != len(buf) {
		t.Fatalf("ReadAt whole volume returned %d bytes, want %d", n, len(buf))
	}
	return buf
}

func assertContent(t *testing.T, got, want []byte, label string) {
	t.Helper()
	if !bytes.Equal(got, want) {
		t.Fatalf("%s: content mismatch\n got=%x\nwant=%x", label, got, want)
	}
}

// assertParityConsistent verifies that member 3 still holds the XOR of the
// three data blocks in every stripe.
func assertParityConsistent(t *testing.T, a *Array, members []*Member) {
	t.Helper()
	for s := 0; s < a.Stripes(); s++ {
		want := make([]byte, a.BlockSize())
		for i := 0; i < DataBlocks; i++ {
			b := members[i].Block(s)
			if b == nil {
				t.Fatalf("stripe %d: member %d block missing", s, i)
			}
			for k := range want {
				want[k] ^= b[k]
			}
		}
		got := members[ParityIndex].Block(s)
		if !bytes.Equal(got, want) {
			t.Fatalf("stripe %d: parity mismatch got=%x want=%x", s, got, want)
		}
	}
}

func TestLayoutCrossesStripes(t *testing.T) {
	a, members := newTestArray(t, 4, 3) // 3 data blocks * 4 bytes = 12-byte stripes, 36-byte volume
	if got, want := a.Cap(), int64(36); got != want {
		t.Fatalf("Cap()=%d, want %d", got, want)
	}
	model := make([]byte, a.Cap())

	// Straddle the stripe boundary and the block boundary inside a stripe.
	fillPattern(t, a, model, 2, 15)
	fillPattern(t, a, model, 0, 36)

	assertContent(t, readAll(t, a), model, "healthy volume")
	assertParityConsistent(t, a, members)

	// Logical byte 12 must live in stripe 1, data block 0, member 0.
	if got, want := members[0].Block(1)[0], model[12]; got != want {
		t.Fatalf("logical offset 12 mapped to member0 stripe1 byte0 = %#x, want %#x", got, want)
	}
	// Logical bytes 8..11 are in stripe 0 data block 2 on member 2.
	if got, want := members[2].Block(0), model[8:12]; !bytes.Equal(got, want) {
		t.Fatalf("logical offset 8..11 mapped to member2 stripe0 = %x, want %x", got, want)
	}
}

func TestDegradedReadWithEachMemberDown(t *testing.T) {
	const blockSize, stripes = 8, 5
	model := make([]byte, blockSize*DataBlocks*stripes)

	for _, down := range []int{0, 1, 2, ParityIndex} {
		a, _ := newTestArray(t, blockSize, stripes)
		for off := 0; off < len(model); off += 7 {
			n := 11
			if off+n > len(model) {
				n = len(model) - off
			}
			fillPattern(t, a, model, off, n)
		}
		if err := a.Fail(down); err != nil {
			t.Fatalf("Fail(%d): %v", down, err)
		}
		assertContent(t, readAll(t, a), model, fmt.Sprintf("read with member %d down", down))
	}
}

func TestPartialWriteToMissingDataBlock(t *testing.T) {
	const blockSize, stripes = 8, 2
	a, members := newTestArray(t, blockSize, stripes)
	model := make([]byte, a.Cap())
	for i := range model {
		model[i] = 0xA5
	}
	if _, err := a.WriteAt(model, 0); err != nil {
		t.Fatalf("seed write: %v", err)
	}

	if err := a.Fail(1); err != nil {
		t.Fatalf("Fail(1): %v", err)
	}

	// Member 1 holds data block 1 of stripe 0: logical bytes [8,16).
	// Overwrite only three bytes inside that missing block.
	patch := []byte{0x01, 0x02, 0x03}
	n, err := a.WriteAt(patch, 10)
	if err != nil || n != len(patch) {
		t.Fatalf("degraded partial write: n=%d err=%v", n, err)
	}
	copy(model[10:13], patch)

	assertContent(t, readAll(t, a), model, "degraded partial write")

	// The surviving parity must match the reconstructed data so that losing
	// yet another data member would still be reconstructable if it were the
	// only remaining loss.
	parity0 := make([]byte, blockSize)
	for i := 0; i < DataBlocks; i++ {
		var b []byte
		if i == 1 {
			b = model[blockSize : 2*blockSize] // stripe 0 data block 1
		} else {
			b = members[i].Block(0)
		}
		for k := range parity0 {
			parity0[k] ^= b[k]
		}
	}
	if got := members[ParityIndex].Block(0); !bytes.Equal(got, parity0) {
		t.Fatalf("parity after degraded partial write = %x, want %x", got, parity0)
	}

	// A fresh read of just the patched range must also agree.
	got := make([]byte, 3)
	if n, err := a.ReadAt(got, 10); err != nil || n != 3 {
		t.Fatalf("degraded ReadAt: n=%d err=%v", n, err)
	}
	if !bytes.Equal(got, patch) {
		t.Fatalf("degraded ReadAt = %x, want %x", got, patch)
	}
}

func TestSecondFailureStopsWrites(t *testing.T) {
	a, _ := newTestArray(t, 4, 3)
	if _, err := a.WriteAt([]byte{1, 2, 3, 4}, 0); err != nil {
		t.Fatalf("initial write: %v", err)
	}
	if err := a.Fail(0); err != nil {
		t.Fatalf("Fail(0): %v", err)
	}
	if _, err := a.WriteAt([]byte{9}, 5); err != nil {
		t.Fatalf("write with one member down: %v", err)
	}
	if err := a.Fail(2); err != nil {
		t.Fatalf("Fail(2): %v", err)
	}

	if a.Writable() {
		t.Fatal("array should be write-stopped after two failures")
	}
	if got, want := a.Missing(), []int{0, 2}; !equalInts(got, want) {
		t.Fatalf("Missing()=%v, want %v", got, want)
	}
	if _, err := a.WriteAt([]byte{7}, 1); !errors.Is(err, ErrStopped) {
		t.Fatalf("WriteAt error = %v, want ErrStopped", err)
	} else {
		var mme *MissingMembersError
		if !errors.As(err, &mme) {
			t.Fatalf("WriteAt error %v does not expose MissingMembersError", err)
		}
		if !equalInts(mme.Missing, []int{0, 2}) {
			t.Fatalf("MissingMembersError.Missing=%v, want [0 2]", mme.Missing)
		}
	}
	if _, err := a.ReadAt(make([]byte, 4), 0); !errors.Is(err, ErrUnrecoverable) {
		t.Fatalf("ReadAt error = %v, want ErrUnrecoverable", err)
	}
	if err := a.Replace(0, NewMember(4, 3)); !errors.Is(err, ErrStopped) {
		t.Fatalf("Replace with two failures error = %v, want ErrStopped", err)
	}
}

// TestRebuildInterleavedWrites is the central end-to-end scenario: one member
// fails, a replacement is rebuilt one stripe at a time, and the caller writes
// to the volume between every pair of steps.  Afterwards each member is removed
// in turn and the full volume must still read back the latest content.
func TestRebuildInterleavedWrites(t *testing.T) {
	const blockSize, stripes = 8, 6
	for _, down := range []int{0, 1, 2, ParityIndex} {
		t.Run(fmt.Sprintf("member-%d", down), func(t *testing.T) {
			a, members := newTestArray(t, blockSize, stripes)
			model := make([]byte, a.Cap())
			for i := range model {
				model[i] = byte(i)
			}
			if _, err := a.WriteAt(model, 0); err != nil {
				t.Fatalf("seed write: %v", err)
			}

			if err := a.Fail(down); err != nil {
				t.Fatalf("Fail(%d): %v", down, err)
			}
			replacement := NewMember(blockSize, stripes)
			if err := a.Replace(down, replacement); err != nil {
				t.Fatalf("Replace(%d): %v", down, err)
			}
			members[down] = replacement

			step := 0
			for {
				// Write between every pair of rebuild steps.  The offsets walk
				// stripes in a different order than the rebuild so that both
				// already-rebuilt and not-yet-rebuilt stripes receive writes.
				off := (step*17 + 5) % len(model)
				n := 6
				if off+n > len(model) {
					n = len(model) - off
				}
				fillPattern(t, a, model, off, n)

				// The volume must stay readable while the rebuild is partial.
				assertContent(t, readAll(t, a), model, fmt.Sprintf("read during rebuild step %d", step))

				p, err := a.RebuildStep()
				if err != nil {
					t.Fatalf("RebuildStep %d: %v", step, err)
				}
				if p.Target != down {
					t.Fatalf("RebuildStep target = %d, want %d", p.Target, down)
				}
				step++
				if !p.Active {
					if p.Done != stripes {
						t.Fatalf("rebuild finished with Done=%d, want %d", p.Done, stripes)
					}
					break
				}
				if step > stripes+1 {
					t.Fatalf("rebuild did not finish within %d steps", stripes+1)
				}
			}

			if a.Missing() != nil {
				t.Fatalf("after rebuild Missing()=%v, want none", a.Missing())
			}
			if !a.Healthy() {
				t.Fatal("array should be healthy after rebuild")
			}
			assertContent(t, readAll(t, a), model, "after rebuild")
			assertParityConsistent(t, a, members)

			// Remove each member in turn, one at a time, from a snapshot of the
			// real rebuilt array, and verify the whole volume still reads back
			// the latest content.
			for _, victim := range []int{0, 1, 2, ParityIndex} {
				snapshot := cloneMembers(t, members)
				b, err := NewArray(blockSize, stripes, snapshot)
				if err != nil {
					t.Fatalf("clone array for victim %d: %v", victim, err)
				}
				if err := b.Fail(victim); err != nil {
					t.Fatalf("Fail(%d) after rebuild: %v", victim, err)
				}
				assertContent(t, readAll(t, b), model, fmt.Sprintf("remove member %d after rebuild", victim))
			}
		})
	}
}

// TestStopDuringRebuild covers losing a second member before the rebuild
// completes: the array must stop accepting writes and name both missing slots,
// while data that was already rebuilt remains readable.
func TestStopDuringRebuild(t *testing.T) {
	const blockSize, stripes = 4, 4
	a, _ := newTestArray(t, blockSize, stripes)
	model := make([]byte, a.Cap())
	for i := range model {
		model[i] = byte(0x40 + i)
	}
	if _, err := a.WriteAt(model, 0); err != nil {
		t.Fatalf("seed write: %v", err)
	}

	if err := a.Fail(1); err != nil {
		t.Fatalf("Fail(1): %v", err)
	}
	if err := a.Replace(1, NewMember(blockSize, stripes)); err != nil {
		t.Fatalf("Replace(1): %v", err)
	}
	// Complete exactly two of four stripes, then lose a second member.
	for i := 0; i < 2; i++ {
		if _, err := a.RebuildStep(); err != nil {
			t.Fatalf("RebuildStep %d: %v", i, err)
		}
	}
	if err := a.Fail(3); err != nil {
		t.Fatalf("Fail(3): %v", err)
	}

	if a.Writable() {
		t.Fatal("array must be write-stopped after losing a second member during rebuild")
	}
	if got, want := a.Missing(), []int{1, 3}; !equalInts(got, want) {
		t.Fatalf("Missing()=%v, want %v", got, want)
	}
	_, err := a.WriteAt([]byte{0xFF}, 0)
	if !errors.Is(err, ErrStopped) {
		t.Fatalf("WriteAt error = %v, want ErrStopped", err)
	}
	var mme *MissingMembersError
	if !errors.As(err, &mme) || !equalInts(mme.Missing, []int{1, 3}) {
		t.Fatalf("write error missing members = %v, want [1 3]", mme)
	}

	// Stripes that were already rebuilt have all their data blocks back, so
	// they stay readable even though parity is gone.
	rebuiltBytes := 2 * DataBlocks * blockSize
	buf := make([]byte, rebuiltBytes)
	if n, err := a.ReadAt(buf, 0); err != nil || n != rebuiltBytes {
		t.Fatalf("read of rebuilt stripes: n=%d err=%v", n, err)
	}
	assertContent(t, buf, model[:rebuiltBytes], "rebuilt stripes after second failure")

	// Stripes that were not rebuilt are missing two blocks and must fail.
	_, err = a.ReadAt(make([]byte, rebuiltBytes), int64(rebuiltBytes))
	if !errors.Is(err, ErrUnrecoverable) {
		t.Fatalf("read of unbuilt stripe error = %v, want ErrUnrecoverable", err)
	}
	var rme *MissingMembersError
	if !errors.As(err, &rme) || !equalInts(rme.Missing, []int{1, 3}) {
		t.Fatalf("read error missing members = %v, want [1 3]", rme)
	}
}

func TestWriteAndReadEdges(t *testing.T) {
	a, _ := newTestArray(t, 4, 2) // 24-byte volume
	model := make([]byte, a.Cap())

	// Empty operations succeed without touching the volume.
	if n, err := a.WriteAt(nil, 7); err != nil || n != 0 {
		t.Fatalf("empty WriteAt n=%d err=%v", n, err)
	}
	if n, err := a.ReadAt(nil, 7); err != nil || n != 0 {
		t.Fatalf("empty ReadAt n=%d err=%v", n, err)
	}

	// A write that crosses the end writes the fitting prefix and reports EOF.
	n, err := a.WriteAt([]byte{1, 2, 3, 4, 5, 6}, 20)
	if n != 4 || !errors.Is(err, io.EOF) {
		t.Fatalf("WriteAt past end: n=%d err=%v, want n=4 io.EOF", n, err)
	}
	copy(model[20:], []byte{1, 2, 3, 4})

	// A read that crosses the end returns the fitting prefix and EOF.
	buf := make([]byte, 8)
	n, err = a.ReadAt(buf, 20)
	if n != 4 || !errors.Is(err, io.EOF) {
		t.Fatalf("ReadAt past end: n=%d err=%v, want n=4 io.EOF", n, err)
	}
	if !bytes.Equal(buf[:4], model[20:]) {
		t.Fatalf("ReadAt past end = %x, want %x", buf[:4], model[20:])
	}

	// Reading exactly at capacity yields no bytes and EOF.
	if n, err := a.ReadAt(make([]byte, 1), a.Cap()); n != 0 || !errors.Is(err, io.EOF) {
		t.Fatalf("ReadAt at capacity: n=%d err=%v", n, err)
	}
	// Negative offsets are rejected.
	if _, err := a.WriteAt([]byte{1}, -1); err == nil {
		t.Fatal("WriteAt with negative offset should fail")
	}
	if _, err := a.ReadAt(make([]byte, 1), -1); err == nil {
		t.Fatal("ReadAt with negative offset should fail")
	}
}

func TestReplaceValidation(t *testing.T) {
	a, _ := newTestArray(t, 4, 3)

	if err := a.Replace(0, NewMember(4, 3)); err == nil {
		t.Fatal("Replace on a healthy member should be rejected")
	}
	if err := a.Replace(9, NewMember(4, 3)); err == nil {
		t.Fatal("Replace with out-of-range index should be rejected")
	}
	if err := a.Fail(1); err != nil {
		t.Fatalf("Fail(1): %v", err)
	}
	if err := a.Replace(1, NewMember(8, 3)); err == nil {
		t.Fatal("Replace with mismatched geometry should be rejected")
	}
	if err := a.Replace(1, nil); err == nil {
		t.Fatal("Replace with nil member should be rejected")
	}
	if err := a.Replace(2, NewMember(4, 3)); err == nil {
		t.Fatal("Replace of a member that is still online should be rejected")
	}
	if err := a.Replace(1, NewMember(4, 3)); err != nil {
		t.Fatalf("Replace(1): %v", err)
	}
	if err := a.Replace(1, NewMember(4, 3)); err == nil {
		t.Fatal("Replace while a rebuild is in progress should be rejected")
	}
	if _, err := a.RebuildStep(); err != nil {
		t.Fatalf("RebuildStep: %v", err)
	}
	if _, err := a.RebuildStep(); err != nil {
		t.Fatalf("RebuildStep: %v", err)
	}
	if _, err := a.RebuildStep(); err != nil {
		t.Fatalf("RebuildStep: %v", err)
	}
	if _, err := a.RebuildStep(); !errors.Is(err, ErrNoRebuild) {
		t.Fatalf("RebuildStep after completion = %v, want ErrNoRebuild", err)
	}
}

// TestWriteBeforeRebuildReachesItsStripe targets one not-yet-rebuilt stripe
// while a data member is offline.  The write covers only a few bytes of the
// missing block; after the rebuild reaches that stripe the replacement member
// must hold the latest bytes, not the stale reconstruction.
func TestWriteBeforeRebuildReachesItsStripe(t *testing.T) {
	const blockSize, stripes = 8, 4
	a, members := newTestArray(t, blockSize, stripes)
	model := make([]byte, a.Cap())
	for i := range model {
		model[i] = byte(0x10 * (i % 7))
	}
	if _, err := a.WriteAt(model, 0); err != nil {
		t.Fatalf("seed write: %v", err)
	}

	if err := a.Fail(2); err != nil {
		t.Fatalf("Fail(2): %v", err)
	}
	replacement := NewMember(blockSize, stripes)
	if err := a.Replace(2, replacement); err != nil {
		t.Fatalf("Replace(2): %v", err)
	}
	members[2] = replacement

	// Leave stripe 2 unbuilt and patch three bytes inside member 2's data
	// block there: logical offset stripe 2 * 24 + block 2 * 8 + 1 = 65.
	patch := []byte{0xDE, 0xAD, 0xBE}
	if n, err := a.WriteAt(patch, 65); err != nil || n != 3 {
		t.Fatalf("write into unbuilt stripe: n=%d err=%v", n, err)
	}
	copy(model[65:68], patch)

	// The patch must already be visible while the stripe is still unbuilt.
	got := make([]byte, 3)
	if n, err := a.ReadAt(got, 65); err != nil || n != 3 {
		t.Fatalf("read of patched bytes: n=%d err=%v", n, err)
	}
	if !bytes.Equal(got, patch) {
		t.Fatalf("patched bytes before rebuild = %x, want %x", got, patch)
	}

	if err := a.RebuildAll(); err != nil {
		t.Fatalf("RebuildAll: %v", err)
	}
	if !a.Healthy() {
		t.Fatal("array should be healthy after rebuild")
	}
	if !bytes.Equal(replacement.Block(2), model[2*a.stripeBytes()+2*blockSize:2*a.stripeBytes()+3*blockSize]) {
		t.Fatalf("rebuilt block %x does not carry the pre-rebuild write", replacement.Block(2))
	}
	assertContent(t, readAll(t, a), model, "after rebuild of patched stripe")
	assertParityConsistent(t, a, members)
}

func TestNewArrayValidation(t *testing.T) {
	members := []*Member{NewMember(4, 2), NewMember(4, 2), NewMember(4, 2), NewMember(4, 2)}
	if _, err := NewArray(0, 2, members); err == nil {
		t.Fatal("zero block size should be rejected")
	}
	if _, err := NewArray(4, 0, members); err == nil {
		t.Fatal("zero stripes should be rejected")
	}
	if _, err := NewArray(4, 2, members[:3]); err == nil {
		t.Fatal("wrong member count should be rejected")
	}
	bad := []*Member{members[0], members[1], members[2], NewMember(8, 2)}
	if _, err := NewArray(4, 2, bad); err == nil {
		t.Fatal("mismatched geometry should be rejected")
	}
	dup := []*Member{members[0], members[0], members[2], members[3]}
	if _, err := NewArray(4, 2, dup); err == nil {
		t.Fatal("duplicate member pointer should be rejected")
	}
}

func equalInts(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
