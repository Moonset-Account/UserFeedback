// Package sealedbuf implements a paged byte buffer that can hand out sealed
// ("封存") views while the original buffer keeps being mutated through arbitrary
// overlapping slices.
//
// The buffer is split into fixed-size pages. Every page object carries a
// version number and a reference count:
//
//   - When a view is created it pins the page versions that cover its byte
//     range at that moment. Sealed views therefore always read the bytes that
//     were visible when the check started, even if the live buffer changes.
//   - The first write to a pinned page performs copy-on-write: the old page is
//     retained for the views still referencing it and a fresh, higher-versioned
//     page becomes the live page.
//   - Two views that observe the same version of a page share the single
//     retained page object (one retained page, not one per view).
//   - Closing the last view referencing an old page drops the last reference
//     and the retained page is released.
//
// All operations are safe for concurrent use: an editor mutating the buffer
// may run in parallel with background content checks holding sealed views.
package sealedbuf

import (
	"errors"
	"io"
	"sync"
)

// Errors returned by buffer and view operations.
var (
	// ErrInvalidPageSize is returned when a buffer is created with a page
	// size smaller than one byte.
	ErrInvalidPageSize = errors.New("sealedbuf: page size must be positive")

	// ErrOutOfRange is returned when a view or read/write range falls
	// outside the buffer or view bounds.
	ErrOutOfRange = errors.New("sealedbuf: range out of bounds")

	// ErrClosed is returned when an operation is attempted on a view whose
	// Close method has already been called.
	ErrClosed = errors.New("sealedbuf: view already closed")
)

// page is one fixed-size chunk of buffer content. A page is immutable once
// any view may reference it; copy-on-write always installs a brand new page
// rather than mutating a shared one.
//
// Reference-count convention:
//   - the live page of a slot is referenced by the buffer (1) plus every view
//     pinning that version, so refs == 1 + number of pinning views;
//   - a retained old page is referenced only by pinning views, so refs ==
//     number of pinning views, and refs dropping to zero releases it.
type page struct {
	data    []byte
	version uint64
	refs    int
}

// PagedBuffer is a fixed-length byte buffer backed by fixed-size pages. It
// implements io.ReaderAt and io.WriterAt.
type PagedBuffer struct {
	mu        sync.Mutex
	pageSize  int
	size      int
	pages     []*page
	retention map[*page]struct{}
}

// New creates a buffer of size bytes backed by pages of pageSize bytes. The
// initial content is all zero and every page starts at version 1.
func New(size, pageSize int) (*PagedBuffer, error) {
	if pageSize <= 0 {
		return nil, ErrInvalidPageSize
	}
	if size < 0 {
		return nil, ErrOutOfRange
	}
	pageCount := (size + pageSize - 1) / pageSize
	pages := make([]*page, pageCount)
	for i := range pages {
		start := i * pageSize
		n := pageSize
		if start+n > size {
			n = size - start
		}
		pages[i] = &page{
			data:    make([]byte, n),
			version: 1,
			refs:    1, // referenced by the live buffer
		}
	}
	return &PagedBuffer{
		pageSize:  pageSize,
		size:      size,
		pages:     pages,
		retention: make(map[*page]struct{}),
	}, nil
}

// Size returns the logical buffer length in bytes.
func (b *PagedBuffer) Size() int {
	return b.size
}

// PageSize returns the configured page size. The final page may be shorter.
func (b *PagedBuffer) PageSize() int {
	return b.pageSize
}

// PageCount returns the number of page slots backing the buffer, including a
// possible short final page.
func (b *PagedBuffer) PageCount() int {
	return len(b.pages)
}

// RetainedPageCount returns the number of old page versions currently kept
// alive for sealed views. Each distinct retained (page-slot, version) pair
// counts once no matter how many views reference it, so two views observing
// the same version of a page share a single retained page.
func (b *PagedBuffer) RetainedPageCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.retention)
}

// LivePageVersion returns the version of the page currently backing page slot
// pageIdx. Versions increase by one on every copy-on-write of the slot.
func (b *PagedBuffer) LivePageVersion(pageIdx int) (uint64, error) {
	if pageIdx < 0 || pageIdx >= len(b.pages) {
		return 0, ErrOutOfRange
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.pages[pageIdx].version, nil
}

// WriteAt writes len(p) bytes at absolute offset off. It implements
// io.WriterAt. Writes are applied page by page; a page whose bytes do not
// actually change is not copied, so no spurious old version is retained.
func (b *PagedBuffer) WriteAt(p []byte, off int64) (int, error) {
	if off < 0 || off > int64(b.size) || int64(len(p)) > int64(b.size)-off {
		return 0, ErrOutOfRange
	}
	total := len(p)
	if total == 0 {
		return 0, nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()

	for len(p) > 0 {
		idx := int(off) / b.pageSize
		pageOff := int(off) % b.pageSize
		n := len(b.pages[idx].data) - pageOff
		if n > len(p) {
			n = len(p)
		}
		b.mutatePageLocked(idx, pageOff, p[:n])
		p = p[n:]
		off += int64(n)
	}
	return total, nil
}

// mutatePageLocked applies src to the live page of slot idx at byte offset
// pageOff, performing copy-on-write when the live page is pinned by one or
// more views. If the bytes are unchanged nothing is copied and no old version
// is retained.
func (b *PagedBuffer) mutatePageLocked(idx, pageOff int, src []byte) {
	live := b.pages[idx]
	if bytesEqual(live.data[pageOff:pageOff+len(src)], src) {
		return
	}
	if live.refs > 1 {
		// The live version is pinned: preserve it for existing views and
		// install a private successor. The buffer reference moves to the
		// successor; the old page is now held by pinning views only.
		fresh := &page{
			data:    append([]byte(nil), live.data...),
			version: live.version + 1,
			refs:    1,
		}
		live.refs--
		b.retention[live] = struct{}{}
		b.pages[idx] = fresh
		live = fresh
	}
	copy(live.data[pageOff:], src)
}

// bytesEqual reports whether two byte slices have identical contents.
func bytesEqual(a, c []byte) bool {
	if len(a) != len(c) {
		return false
	}
	for i := range a {
		if a[i] != c[i] {
			return false
		}
	}
	return true
}

// ReadAt reads len(p) bytes at absolute offset off into p. It implements
// io.ReaderAt and fills p fully or returns ErrOutOfRange.
func (b *PagedBuffer) ReadAt(p []byte, off int64) (int, error) {
	if off < 0 || off > int64(b.size) || int64(len(p)) > int64(b.size)-off {
		return 0, ErrOutOfRange
	}
	if len(p) == 0 {
		return 0, nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	return gather(b.pageSize, b.pages, p, off), nil
}

// gather copies len(dst) bytes starting at absolute buffer offset off out of
// the given page array (live pages for the buffer, pinned pages for a view).
func gather(pageSize int, pages []*page, dst []byte, off int64) int {
	written := 0
	for len(dst) > 0 {
		idx := int(off) / pageSize
		pageOff := int(off) % pageSize
		p := pages[idx]
		n := copy(dst, p.data[pageOff:])
		written += n
		dst = dst[n:]
		off += int64(n)
	}
	return written
}

// Edit hands the caller a scratch slice covering the live bytes
// [off, off+length) so the editor can mutate arbitrary overlapping ranges.
// The scratch slice is a private working copy; when the callback returns its
// contents are written back through the copy-on-write path, so sealed views
// remain unaffected. The callback must not call back into the buffer.
func (b *PagedBuffer) Edit(off, length int, fn func(scratch []byte)) error {
	if off < 0 || length < 0 || off+length < 0 || off+length > b.size {
		return ErrOutOfRange
	}
	if length == 0 {
		fn(nil)
		return nil
	}
	scratch := make([]byte, length)
	b.mu.Lock()
	gather(b.pageSize, b.pages, scratch, int64(off))
	b.mu.Unlock()

	fn(scratch)

	b.mu.Lock()
	defer b.mu.Unlock()
	// Re-write page by page so unchanged pages are not needlessly retained.
	rel := 0
	for rel < length {
		abs := off + rel
		idx := abs / b.pageSize
		pageOff := abs % b.pageSize
		n := b.pageSize - pageOff
		if rem := length - rel; n > rem {
			n = rem
		}
		b.mutatePageLocked(idx, pageOff, scratch[rel:rel+n])
		rel += n
	}
	return nil
}

// View creates a sealed view over the byte range [off, off+length). The view
// pins the page versions visible at call time; later writes to the buffer do
// not change what the view reads. A zero-length view is legal and pins no
// pages. The view must be Closed after use.
func (b *PagedBuffer) View(off, length int) (*View, error) {
	if off < 0 || length < 0 || off+length < 0 || off+length > b.size {
		return nil, ErrOutOfRange
	}
	v := &View{
		buf:    b,
		off:    off,
		length: length,
	}
	if length == 0 {
		return v, nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	first := off / b.pageSize
	last := (off + length - 1) / b.pageSize
	v.pages = make([]*page, 0, last-first+1)
	for idx := first; idx <= last; idx++ {
		p := b.pages[idx]
		p.refs++
		v.pages = append(v.pages, p)
	}
	v.firstPage = first
	return v, nil
}

// View is an immutable sealed snapshot over a byte range of a PagedBuffer.
// It implements io.ReaderAt (offsets relative to the view start), io.Reader
// and io.Seeker.
type View struct {
	buf       *PagedBuffer
	off       int
	length    int
	firstPage int
	pages     []*page
	cursor    int64
	closed    bool
}

// Length returns the sealed byte range length.
func (v *View) Length() int {
	return v.length
}

// PinnedPageCount returns the number of page objects pinned by this view
// (pages overlapping its range, including partial overlaps).
func (v *View) PinnedPageCount() int {
	return len(v.pages)
}

// PinnedPageVersion returns the sealed version of the i-th pinned page, where
// i ranges over [0, PinnedPageCount()).
func (v *View) PinnedPageVersion(i int) (uint64, error) {
	if v.closed {
		return 0, ErrClosed
	}
	if i < 0 || i >= len(v.pages) {
		return 0, ErrOutOfRange
	}
	return v.pages[i].version, nil
}

// Bytes returns a copy of the full sealed content.
func (v *View) Bytes() ([]byte, error) {
	if v.closed {
		return nil, ErrClosed
	}
	out := make([]byte, v.length)
	if v.length == 0 {
		return out, nil
	}
	b := v.buf
	b.mu.Lock()
	defer b.mu.Unlock()
	gather(b.pageSize, v.pages, out, int64(v.off%b.pageSize))
	return out, nil
}

// readLocked copies len(dst) bytes from relative view offset relOff out of the
// pinned pages. Bounds are checked by callers.
func (v *View) readLocked(dst []byte, relOff int64) int {
	b := v.buf
	absOff := int64(v.off) + relOff
	written := 0
	for len(dst) > 0 {
		slot := int(absOff)/b.pageSize - v.firstPage
		pageOff := int(absOff) % b.pageSize
		p := v.pages[slot]
		n := copy(dst, p.data[pageOff:])
		written += n
		dst = dst[n:]
		absOff += int64(n)
	}
	return written
}

// ReadAt reads len(p) bytes at offset off relative to the view start. It
// implements io.ReaderAt.
func (v *View) ReadAt(p []byte, off int64) (int, error) {
	if v.closed {
		return 0, ErrClosed
	}
	if off < 0 || off > int64(v.length) || int64(len(p)) > int64(v.length)-off {
		return 0, ErrOutOfRange
	}
	if len(p) == 0 {
		return 0, nil
	}
	v.buf.mu.Lock()
	defer v.buf.mu.Unlock()
	return v.readLocked(p, off), nil
}

// Read reads up to len(p) bytes at the view's internal cursor and advances it.
// It implements io.Reader.
func (v *View) Read(p []byte) (int, error) {
	if v.closed {
		return 0, ErrClosed
	}
	if len(p) == 0 {
		return 0, nil
	}
	if v.cursor >= int64(v.length) {
		return 0, io.EOF
	}
	avail := int64(v.length) - v.cursor
	if int64(len(p)) > avail {
		p = p[:avail]
	}
	v.buf.mu.Lock()
	n := v.readLocked(p, v.cursor)
	v.cursor += int64(n)
	atEnd := v.cursor >= int64(v.length)
	v.buf.mu.Unlock()
	if atEnd {
		return n, io.EOF
	}
	return n, nil
}

// Seek sets the view's internal read cursor. It implements io.Seeker.
func (v *View) Seek(offset int64, whence int) (int64, error) {
	if v.closed {
		return 0, ErrClosed
	}
	var target int64
	switch whence {
	case io.SeekStart:
		target = offset
	case io.SeekCurrent:
		target = v.cursor + offset
	case io.SeekEnd:
		target = int64(v.length) + offset
	default:
		return 0, errors.New("sealedbuf: invalid whence")
	}
	if target < 0 || target > int64(v.length) {
		return 0, ErrOutOfRange
	}
	v.cursor = target
	return target, nil
}

// Close releases the pages pinned by this view. When this is the last view
// referencing an old page version, that retained page is freed. Closing an
// already-closed view returns ErrClosed.
func (v *View) Close() error {
	b := v.buf
	b.mu.Lock()
	defer b.mu.Unlock()
	if v.closed {
		return ErrClosed
	}
	v.closed = true
	for _, p := range v.pages {
		p.refs--
		if p.refs == 0 {
			// Last pinning view of a retained old version: release it.
			delete(b.retention, p)
		}
	}
	v.pages = nil
	return nil
}

var (
	_ io.ReaderAt = (*PagedBuffer)(nil)
	_ io.WriterAt = (*PagedBuffer)(nil)
	_ io.Reader   = (*View)(nil)
	_ io.Seeker   = (*View)(nil)
	_ io.ReaderAt = (*View)(nil)
)
