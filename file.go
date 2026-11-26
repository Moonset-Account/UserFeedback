package overlay

import (
	"errors"
	"io"
)

var errEOF = errors.New("overlay: end of file")

// File is an open handle. It pins its node: the object stays alive and
// readable/writable even after every name is unlinked, and it observes
// promotion done through any other name or handle.
type File struct {
	fs     *FS
	node   *node
	flags  int
	off    int
	closed bool
}

// Read reads at the current offset, advancing it.
func (f *File) Read(p []byte) (int, error) {
	if f.closed {
		return 0, ErrBadFD
	}
	if f.flags&3 == O_WRONLY {
		return 0, ErrBadFD
	}
	n := f.node
	n.mu.Lock()
	defer n.mu.Unlock()
	nr, err := f.fs.readAtLocked(n, p, f.off)
	f.off += nr
	if err == errEOF {
		return nr, io.EOF
	}
	return nr, err
}

// Write promotes the node on first write, then writes at the current
// offset, advancing it. A failed promotion leaves the node untouched.
func (f *File) Write(p []byte) (int, error) {
	if f.closed {
		return 0, ErrBadFD
	}
	if f.flags&3 == O_RDONLY {
		return 0, ErrBadFD
	}
	if err := f.fs.promote(f.node); err != nil {
		return 0, err
	}
	n := f.node
	n.mu.Lock()
	defer n.mu.Unlock()
	if err := f.fs.writeAtLocked(n, p, f.off); err != nil {
		return 0, err
	}
	f.off += len(p)
	return len(p), nil
}

// Seek sets the offset for the next Read or Write.
func (f *File) Seek(offset int64, whence int) (int64, error) {
	if f.closed {
		return 0, ErrBadFD
	}
	var base int64
	switch whence {
	case io.SeekStart:
		base = 0
	case io.SeekCurrent:
		base = int64(f.off)
	case io.SeekEnd:
		f.node.mu.Lock()
		if f.node.lf != nil {
			base = int64(len(f.node.lf.data))
		} else {
			base = int64(f.node.size)
		}
		f.node.mu.Unlock()
	default:
		return 0, ErrInvalid
	}
	abs := base + offset
	if abs < 0 {
		return 0, ErrInvalid
	}
	f.off = int(abs)
	return abs, nil
}

// Close releases the handle; the node is reclaimed if this was the last
// handle and no names remain.
func (f *File) Close() error {
	if f.closed {
		return nil
	}
	f.closed = true
	fs := f.fs
	fs.mu.Lock()
	defer fs.mu.Unlock()
	f.node.nopen--
	fs.maybeReclaimLocked(f.node)
	return nil
}
