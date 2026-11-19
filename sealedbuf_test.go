package sealedbuf

import (
	"bytes"
	"errors"
	"io"
	"math/rand"
	"sync"
	"testing"
)

// fill writes an ascending byte pattern 1,2,3,... into the whole buffer.
func fill(t *testing.T, b *PagedBuffer) {
	t.Helper()
	pat := make([]byte, b.Size())
	for i := range pat {
		pat[i] = byte(i + 1)
	}
	if n, err := b.WriteAt(pat, 0); err != nil || n != len(pat) {
		t.Fatalf("initial WriteAt: n=%d err=%v", n, err)
	}
}

func mustView(t *testing.T, b *PagedBuffer, off, length int) *View {
	t.Helper()
	v, err := b.View(off, length)
	if err != nil {
		t.Fatalf("View(%d,%d) unexpected error: %v", off, length, err)
	}
	return v
}

// TestSealedViewSurvivesOverlappingEdit covers the primary acceptable path:
// a check pins a byte range at start, while the editor keeps mutating the
// live buffer through arbitrary overlapping slices. The sealed content must
// stay frozen while the live content changes, and every distinct touched page
// version is retained exactly once.
func TestSealedViewSurvivesOverlappingEdit(t *testing.T) {
	const size, pageSize = 40, 16
	b, err := New(size, pageSize) // slots: [0,16) [16,32) [32,40)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	fill(t, b)

	original := make([]byte, size)
	if _, err := b.ReadAt(original, 0); err != nil {
		t.Fatalf("ReadAt: %v", err)
	}

	// Background content check starts: pin the whole range at version 1.
	check := mustView(t, b, 0, size)
	defer check.Close()
	if check.PinnedPageCount() != 3 {
		t.Fatalf("pinned pages = %d, want 3", check.PinnedPageCount())
	}
	for i := 0; i < 3; i++ {
		if v, _ := check.PinnedPageVersion(i); v != 1 {
			t.Fatalf("pinned version slot %d = %d, want 1", i, v)
		}
	}
	if got := b.RetainedPageCount(); got != 0 {
		t.Fatalf("retention before edits = %d, want 0", got)
	}

	// Editor mutates an overlapping slice crossing the first page boundary.
	if err := b.Edit(10, 20, func(s []byte) {
		for i := range s {
			s[i] = 0xAA
		}
	}); err != nil {
		t.Fatalf("Edit: %v", err)
	}
	// Slots 0 and 1 were pinned and changed -> two retained old versions.
	if got := b.RetainedPageCount(); got != 2 {
		t.Fatalf("retention after cross-page edit = %d, want 2", got)
	}

	// Another overlapping write crossing the second boundary (28..35).
	overlap := []byte{0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08}
	if _, err := b.WriteAt(overlap, 28); err != nil {
		t.Fatalf("WriteAt: %v", err)
	}
	// Slot 1's successor is private (no view pins it): in-place change, no
	// new retention. Slot 2 is pinned and changes: one more retained page.
	if got := b.RetainedPageCount(); got != 3 {
		t.Fatalf("retention after second overlapping write = %d, want 3", got)
	}

	// The sealed view still reads the bytes fixed at check start.
	sealed, err := check.Bytes()
	if err != nil {
		t.Fatalf("sealed Bytes: %v", err)
	}
	if !bytes.Equal(sealed, original) {
		t.Fatalf("sealed content drifted:\n got %v\nwant %v", sealed, original)
	}

	// The live buffer reflects both edits. The first edit painted [10,30)
	// with 0xAA; the second write at [28,36) overwrites part of it.
	live := make([]byte, size)
	if _, err := b.ReadAt(live, 0); err != nil {
		t.Fatalf("live ReadAt: %v", err)
	}
	if bytes.Equal(live, original) {
		t.Fatal("live buffer was not modified")
	}
	// [10,28) belongs only to the first edit.
	if !bytes.Equal(live[10:28], bytes.Repeat([]byte{0xAA}, 18)) {
		t.Fatalf("edit region wrong: %x", live[10:28])
	}
	// [28,36) holds the second write verbatim.
	if !bytes.Equal(live[28:36], overlap) {
		t.Fatalf("write region wrong: %x", live[28:36])
	}
	// [36,40) was never touched and keeps the original pattern.
	if !bytes.Equal(live[36:40], original[36:40]) {
		t.Fatalf("untouched region changed: %x", live[36:40])
	}

	// ReadAt on the view returns the same frozen bytes at arbitrary offsets.
	probe := make([]byte, 8)
	if _, err := check.ReadAt(probe, 12); err != nil {
		t.Fatalf("view ReadAt: %v", err)
	}
	if !bytes.Equal(probe, original[12:20]) {
		t.Fatalf("view ReadAt mismatch: got %v want %v", probe, original[12:20])
	}

	// Releasing the last view frees every retained old page.
	if err := check.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	if got := b.RetainedPageCount(); got != 0 {
		t.Fatalf("retention after close = %d, want 0", got)
	}
}

// TestSameVersionSharedRetention verifies that two checks observing the same
// version of a page share one retained page object rather than each keeping a
// private copy.
func TestSameVersionSharedRetention(t *testing.T) {
	const size, pageSize = 32, 16
	b, err := New(size, pageSize)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	fill(t, b)

	v1 := mustView(t, b, 0, size) // pins both slots at v1
	v2 := mustView(t, b, 4, 8)    // pins only slot 0 at v1

	ver1, _ := v1.PinnedPageVersion(0)
	ver2, _ := v2.PinnedPageVersion(0)
	if ver1 != 1 || ver2 != 1 || ver1 != ver2 {
		t.Fatalf("versions v1=%d v2=%d, want shared v1", ver1, ver2)
	}
	// White-box: both views hold the very same page object.
	if v1.pages[0] != v2.pages[0] {
		t.Fatal("views of the same page version do not share the page object")
	}

	if err := b.Edit(0, 16, func(s []byte) { s[0] = 0x7F }); err != nil {
		t.Fatalf("Edit: %v", err)
	}
	if got := b.RetainedPageCount(); got != 1 {
		t.Fatalf("retention with two views on one old version = %d, want 1", got)
	}

	// Both views still read the pre-edit content of slot 0.
	old, _ := v1.Bytes()
	sub, _ := v2.Bytes()
	if old[0] != 1 || sub[0] != 5 {
		t.Fatalf("sealed content changed: old[0]=%d sub[0]=%d", old[0], sub[0])
	}

	// Closing one view does not release the shared page while the other lives.
	if err := v2.Close(); err != nil {
		t.Fatalf("v2 Close: %v", err)
	}
	if got := b.RetainedPageCount(); got != 1 {
		t.Fatalf("retention after first close = %d, want 1", got)
	}
	// The remaining view is still fully readable.
	if _, err := v1.ReadAt(make([]byte, 4), 0); err != nil {
		t.Fatalf("v1 read after v2 close: %v", err)
	}

	if err := v1.Close(); err != nil {
		t.Fatalf("v1 Close: %v", err)
	}
	if got := b.RetainedPageCount(); got != 0 {
		t.Fatalf("retention after last close = %d, want 0", got)
	}

	// Views created at different times pin different versions and are both
	// retained independently while alive.
	early := mustView(t, b, 0, 16)
	if err := b.Edit(0, 1, func(s []byte) { s[0] = 0x11 }); err != nil {
		t.Fatalf("Edit: %v", err)
	}
	middle := mustView(t, b, 0, 16)
	if err := b.Edit(0, 1, func(s []byte) { s[0] = 0x22 }); err != nil {
		t.Fatalf("Edit: %v", err)
	}
	ev, _ := early.PinnedPageVersion(0)
	mv, _ := middle.PinnedPageVersion(0)
	live, _ := b.LivePageVersion(0)
	if !(ev < mv && mv < live) {
		t.Fatalf("expected monotonic versions early=%d middle=%d live=%d", ev, mv, live)
	}
	if got := b.RetainedPageCount(); got != 2 {
		t.Fatalf("retention of two distinct old versions = %d, want 2", got)
	}
	early.Close()
	middle.Close()
	if got := b.RetainedPageCount(); got != 0 {
		t.Fatalf("retention after both close = %d, want 0", got)
	}
}

// TestCrossPageViewPartialAndRepeatedEdit is the boundary/interleaving case:
// a view spanning exactly two pages; editing only one page retains one old
// page, re-editing the same page never double-retains, and an unchanged write
// retains nothing.
func TestCrossPageViewPartialAndRepeatedEdit(t *testing.T) {
	const size, pageSize = 32, 16
	b, err := New(size, pageSize)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	fill(t, b)

	// View [8,24) overlaps both pages partially.
	v := mustView(t, b, 8, 16)
	defer v.Close()
	if v.PinnedPageCount() != 2 {
		t.Fatalf("pinned = %d, want 2", v.PinnedPageCount())
	}

	// Change page 0 only.
	if err := b.Edit(0, 16, func(s []byte) {
		for i := range s {
			s[i] = 0x33
		}
	}); err != nil {
		t.Fatalf("Edit page0: %v", err)
	}
	if got := b.RetainedPageCount(); got != 1 {
		t.Fatalf("after editing one of two pages: retention=%d want 1", got)
	}

	// Re-edit the same page: its successor is private, so it changes in place
	// and no additional old page is retained.
	if err := b.Edit(0, 4, func(s []byte) {
		for i := range s {
			s[i] = 0x44
		}
	}); err != nil {
		t.Fatalf("re-edit page0: %v", err)
	}
	if got := b.RetainedPageCount(); got != 1 {
		t.Fatalf("after re-editing same page: retention=%d want 1", got)
	}

	// A write that leaves bytes identical must not retain anything either.
	same := make([]byte, 4)
	if _, err := b.ReadAt(same, 0); err != nil {
		t.Fatalf("ReadAt: %v", err)
	}
	if _, err := b.WriteAt(same, 0); err != nil {
		t.Fatalf("identical WriteAt: %v", err)
	}
	if got := b.RetainedPageCount(); got != 1 {
		t.Fatalf("after identical write: retention=%d want 1", got)
	}

	// Now touch page 1: the retained count grows by exactly one.
	if err := b.Edit(16, 16, func(s []byte) { s[15] = 0x55 }); err != nil {
		t.Fatalf("Edit page1: %v", err)
	}
	if got := b.RetainedPageCount(); got != 2 {
		t.Fatalf("after editing second page: retention=%d want 2", got)
	}

	// The sealed view still exposes the original pattern on [8,24).
	want := make([]byte, 16)
	for i := range want {
		want[i] = byte(8 + i + 1)
	}
	got, err := v.Bytes()
	if err != nil {
		t.Fatalf("sealed Bytes: %v", err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("sealed range mismatch:\n got %v\nwant %v", got, want)
	}
}

// TestCloseReleasesOldPages checks release timing: an old page stays retained
// until the last referencing view closes, then disappears; the live buffer
// remains fully usable afterwards.
func TestCloseReleasesOldPages(t *testing.T) {
	b, err := New(24, 8)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	fill(t, b)

	v1 := mustView(t, b, 0, 24)
	v2 := mustView(t, b, 0, 8)
	if err := b.Edit(0, 8, func(s []byte) { s[0] = 0x66 }); err != nil {
		t.Fatalf("Edit: %v", err)
	}
	if got := b.RetainedPageCount(); got != 1 {
		t.Fatalf("retention = %d want 1", got)
	}

	if err := v1.Close(); err != nil {
		t.Fatalf("v1 Close: %v", err)
	}
	if got := b.RetainedPageCount(); got != 1 {
		t.Fatalf("retention after first close = %d want 1", got)
	}
	if err := v2.Close(); err != nil {
		t.Fatalf("v2 Close: %v", err)
	}
	if got := b.RetainedPageCount(); got != 0 {
		t.Fatalf("retention after last close = %d want 0", got)
	}

	// Live buffer survived and still serves the edited content.
	live := make([]byte, 24)
	if _, err := b.ReadAt(live, 0); err != nil {
		t.Fatalf("ReadAt after release: %v", err)
	}
	if live[0] != 0x66 {
		t.Fatalf("live[0] = %x want 0x66", live[0])
	}

	// Double close and use-after-close are rejected.
	if err := v2.Close(); !errors.Is(err, ErrClosed) {
		t.Fatalf("double Close err = %v, want ErrClosed", err)
	}
	if _, err := v1.Bytes(); !errors.Is(err, ErrClosed) {
		t.Fatalf("Bytes on closed view err = %v, want ErrClosed", err)
	}
	if _, err := v1.ReadAt(make([]byte, 1), 0); !errors.Is(err, ErrClosed) {
		t.Fatalf("ReadAt on closed view err = %v, want ErrClosed", err)
	}
	if _, err := v1.Seek(0, io.SeekStart); !errors.Is(err, ErrClosed) {
		t.Fatalf("Seek on closed view err = %v, want ErrClosed", err)
	}
}

// TestRejectedOperations covers the results that must be blocked or rejected:
// invalid construction parameters and every out-of-range access. Rejected
// operations must leave retention untouched.
func TestRejectedOperations(t *testing.T) {
	if _, err := New(16, 0); !errors.Is(err, ErrInvalidPageSize) {
		t.Fatalf("New(16,0) err = %v, want ErrInvalidPageSize", err)
	}
	if _, err := New(16, -1); !errors.Is(err, ErrInvalidPageSize) {
		t.Fatalf("New(16,-1) err = %v, want ErrInvalidPageSize", err)
	}
	if _, err := New(-1, 8); !errors.Is(err, ErrOutOfRange) {
		t.Fatalf("New(-1,8) err = %v, want ErrOutOfRange", err)
	}

	b, err := New(24, 8)
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	badViews := [][2]int{{-1, 1}, {0, -1}, {24, 1}, {20, 5}, {1, 24}}
	for _, c := range badViews {
		if v, err := b.View(c[0], c[1]); err == nil {
			v.Close()
			t.Fatalf("View(%d,%d) expected ErrOutOfRange", c[0], c[1])
		} else if !errors.Is(err, ErrOutOfRange) {
			t.Fatalf("View(%d,%d) err = %v, want ErrOutOfRange", c[0], c[1], err)
		}
	}
	if got := b.RetainedPageCount(); got != 0 {
		t.Fatalf("retention after rejected views = %d, want 0", got)
	}

	if _, err := b.WriteAt([]byte{1, 2}, 23); !errors.Is(err, ErrOutOfRange) {
		t.Fatalf("WriteAt past end err = %v, want ErrOutOfRange", err)
	}
	if _, err := b.WriteAt([]byte{1}, -1); !errors.Is(err, ErrOutOfRange) {
		t.Fatalf("WriteAt negative err = %v, want ErrOutOfRange", err)
	}
	if _, err := b.ReadAt(make([]byte, 1), 24); !errors.Is(err, ErrOutOfRange) {
		t.Fatalf("ReadAt past end err = %v, want ErrOutOfRange", err)
	}
	if err := b.Edit(20, 5, func([]byte) {}); !errors.Is(err, ErrOutOfRange) {
		t.Fatalf("Edit past end err = %v, want ErrOutOfRange", err)
	}
	// Rejected Edit must not invoke the callback with a buffer.
	b.Edit(0, 25, func(s []byte) {
		if s != nil {
			t.Fatal("callback of rejected Edit received non-nil scratch")
		}
	})

	v := mustView(t, b, 0, 8)
	defer v.Close()
	if _, err := v.ReadAt(make([]byte, 2), 7); !errors.Is(err, ErrOutOfRange) {
		t.Fatalf("view ReadAt past end err = %v, want ErrOutOfRange", err)
	}
	if _, err := v.ReadAt(make([]byte, 1), -1); !errors.Is(err, ErrOutOfRange) {
		t.Fatalf("view ReadAt negative err = %v, want ErrOutOfRange", err)
	}
	if _, err := v.Seek(-1, io.SeekStart); !errors.Is(err, ErrOutOfRange) {
		t.Fatalf("Seek negative err = %v, want ErrOutOfRange", err)
	}
}

// TestBoundaryRanges exercises edge shapes: zero-length views, one-byte
// views on a page boundary, exact-multiple sizing, short final pages and an
// entirely empty buffer.
func TestBoundaryRanges(t *testing.T) {
	// Zero-size buffer.
	empty, err := New(0, 16)
	if err != nil {
		t.Fatalf("New(0,16): %v", err)
	}
	if empty.PageCount() != 0 || empty.Size() != 0 {
		t.Fatal("zero-size buffer has pages")
	}
	ev, err := empty.View(0, 0)
	if err != nil {
		t.Fatalf("zero-length view on empty buffer: %v", err)
	}
	if data, err := ev.Bytes(); err != nil || len(data) != 0 || ev.PinnedPageCount() != 0 {
		t.Fatalf("empty view invariants failed: data=%v err=%v pinned=%d", data, err, ev.PinnedPageCount())
	}
	if err := ev.Close(); err != nil {
		t.Fatalf("empty view close: %v", err)
	}

	// Size exactly divisible by page size, plus short final page case.
	b, err := New(33, 16) // 16, 16, 1
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	fill(t, b)

	// Zero-length view at the very end is legal and pins nothing.
	tail := mustView(t, b, 33, 0)
	if tail.PinnedPageCount() != 0 {
		t.Fatal("zero-length view pinned pages")
	}
	tail.Close()

	// One-byte view on the short final page pins exactly that page.
	one := mustView(t, b, 32, 1)
	if one.PinnedPageCount() != 1 {
		t.Fatalf("one-byte view pinned %d pages, want 1", one.PinnedPageCount())
	}
	// Edit the short final page while pinned: retention rises by one.
	if err := b.Edit(32, 1, func(s []byte) { s[0] = 0x99 }); err != nil {
		t.Fatalf("short-page edit: %v", err)
	}
	if got := b.RetainedPageCount(); got != 1 {
		t.Fatalf("short-page retention = %d, want 1", got)
	}
	sealed, _ := one.Bytes()
	if sealed[0] != byte(33) { // original pattern value at index 32
		t.Fatalf("sealed short page = %d, want 33", sealed[0])
	}
	one.Close()
	if got := b.RetainedPageCount(); got != 0 {
		t.Fatalf("retention after short-page close = %d, want 0", got)
	}

	// One-byte view exactly on a page boundary pins only the second page.
	boundary := mustView(t, b, 16, 1)
	if boundary.PinnedPageCount() != 1 {
		t.Fatalf("boundary view pinned %d pages, want 1", boundary.PinnedPageCount())
	}
	if v, _ := boundary.PinnedPageVersion(0); v != 1 {
		t.Fatalf("boundary view version = %d, want 1", v)
	}
	boundary.Close()
}

// TestViewReaderSeeker checks cursor reads, EOF delivery and Seek modes.
func TestViewReaderSeeker(t *testing.T) {
	b, err := New(10, 4)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	fill(t, b)
	v := mustView(t, b, 0, 10)
	defer v.Close()

	got := make([]byte, 0, 10)
	buf := make([]byte, 3)
	for {
		n, err := v.Read(buf)
		got = append(got, buf[:n]...)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatalf("Read: %v", err)
		}
	}
	want := make([]byte, 10)
	for i := range want {
		want[i] = byte(i + 1)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("cursor read = %v, want %v", got, want)
	}

	pos, err := v.Seek(-4, io.SeekEnd)
	if err != nil || pos != 6 {
		t.Fatalf("Seek end: pos=%d err=%v", pos, err)
	}
	reread := make([]byte, 4)
	if _, err := io.ReadFull(v, reread); err != nil {
		t.Fatalf("ReadFull after seek: %v", err)
	}
	if !bytes.Equal(reread, want[6:]) {
		t.Fatalf("reread = %v, want %v", reread, want[6:])
	}
	if _, err := v.Seek(0, 42); err == nil {
		t.Fatal("invalid whence should error")
	}
}

// TestConcurrentEditorsAndCheckers interleaves editors mutating overlapping
// ranges with background checks creating sealed views. Invariants under race:
// sealed content is immutable for the view's lifetime, retention never goes
// negative, and everything is released once all views close.
func TestConcurrentEditorsAndCheckers(t *testing.T) {
	const (
		size       = 128
		pageSize   = 16
		editors    = 4
		checkers   = 6
		writeIters = 80
		checkIters = 40
	)
	b, err := New(size, pageSize)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	fill(t, b)

	var wg sync.WaitGroup
	for e := 0; e < editors; e++ {
		wg.Add(1)
		go func(seed int64) {
			defer wg.Done()
			r := rand.New(rand.NewSource(seed))
			payload := make([]byte, 32)
			for i := 0; i < writeIters; i++ {
				off := r.Intn(size)
				n := r.Intn(size-off) + 1
				if n > len(payload) {
					n = len(payload)
				}
				for k := 0; k < n; k++ {
					payload[k] = byte(r.Intn(256))
				}
				if _, err := b.WriteAt(append([]byte(nil), payload[:n]...), int64(off)); err != nil {
					t.Errorf("concurrent WriteAt: %v", err)
					return
				}
			}
		}(int64(e + 1))
	}

	for c := 0; c < checkers; c++ {
		wg.Add(1)
		go func(seed int64) {
			defer wg.Done()
			r := rand.New(rand.NewSource(seed + 100))
			for i := 0; i < checkIters; i++ {
				off := r.Intn(size)
				n := r.Intn(size-off) + 1
				v, err := b.View(off, n)
				if err != nil {
					t.Errorf("concurrent View: %v", err)
					return
				}
				first, err := v.Bytes()
				if err != nil {
					t.Errorf("concurrent Bytes: %v", err)
					v.Close()
					return
				}
				again := make([]byte, n)
				if _, err := v.ReadAt(again, 0); err != nil {
					t.Errorf("concurrent ReadAt: %v", err)
					v.Close()
					return
				}
				if !bytes.Equal(first, again) {
					t.Errorf("sealed view mutated while pinned")
					v.Close()
					return
				}
				if pos, err := v.Seek(0, io.SeekStart); err != nil || pos != 0 {
					t.Errorf("concurrent Seek: pos=%d err=%v", pos, err)
				}
				if err := v.Close(); err != nil {
					t.Errorf("concurrent Close: %v", err)
					return
				}
				if b.RetainedPageCount() < 0 {
					t.Errorf("negative retention")
					return
				}
			}
		}(int64(c + 1))
	}
	wg.Wait()

	if got := b.RetainedPageCount(); got != 0 {
		t.Fatalf("retention after all views closed = %d, want 0", got)
	}
	// White-box invariant checks once quiescent.
	b.mu.Lock()
	if len(b.retention) != 0 {
		t.Fatalf("retention index not empty: %d", len(b.retention))
	}
	for i, p := range b.pages {
		if p.refs != 1 {
			t.Fatalf("live page %d refs = %d, want 1", i, p.refs)
		}
		if len(p.data) == 0 {
			t.Fatalf("live page %d has no data", i)
		}
	}
	b.mu.Unlock()

	// Full-buffer read still works after the storm.
	out := make([]byte, size)
	if n, err := b.ReadAt(out, 0); err != nil || n != size {
		t.Fatalf("post-storm read: n=%d err=%v", n, err)
	}
}
