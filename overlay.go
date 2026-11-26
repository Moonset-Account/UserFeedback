// Package overlay implements an embeddable copy-on-write layer over a
// read-only base image. Lower files are promoted (copied up) to a single
// upper object on the first write through any of their names; every
// visible hard link and every already-open handle then observes that one
// object. Promotion is atomic with respect to readers and to concurrent
// promoters, and is keyed by lower inode identity rather than by path,
// so renaming directories mid-copy can never resurrect a file at its old
// path.
package overlay

import (
	"errors"
	"sort"
	"strings"
	"sync"
)

var (
	ErrNotExist = errors.New("overlay: path does not exist")
	ErrExist    = errors.New("overlay: path already exists")
	ErrIsDir    = errors.New("overlay: is a directory")
	ErrNotDir   = errors.New("overlay: not a directory")
	ErrInvalid  = errors.New("overlay: invalid argument")
	ErrBadFD    = errors.New("overlay: bad file descriptor")
	ErrNoBlock  = errors.New("overlay: backend block missing")
)

// Open flag bits.
const (
	O_RDONLY = 0
	O_WRONLY = 1
	O_RDWR   = 2
	O_CREATE = 1 << 6
	O_TRUNC  = 1 << 9
)

// node is an overlay inode. All names of one lower inode resolve to the
// same node, so promotion and reclamation are naturally per-object, not
// per-name.
type node struct {
	isDir bool

	// Directory state, guarded by FS.mu.
	parent    *node // nil for root; files may have several parents
	children  map[string]*node
	whiteouts map[string]bool // lower names deleted or moved away
	ld        *lowerDir       // lower backing for lazy entry merge

	// File content state, guarded by mu.
	mu     sync.Mutex
	lf     *lowerFile // non-nil while still backed by the lower image
	blocks []uint64   // upper blocks, valid once lf == nil
	size   int

	// promoMu serializes copy-up so concurrent first writes promote once.
	promoMu sync.Mutex

	// Identity/link bookkeeping, guarded by FS.mu.
	lowerKey *lowerFile // reverse key into FS.lowerFiles, kept after promotion
	nlink    int        // visible directory entries pointing here
	nopen    int        // open handles
	dead     bool       // reclaimed
}

// FS is the writable overlay. The zero value is not usable; call New.
type FS struct {
	mu         sync.Mutex
	backend    *MemBackend
	root       *node
	lowerFiles map[*lowerFile]*node // promotion table, keyed by lower inode
	lowerDirs  map[*lowerDir]*node
	promotions int
	reclaimed  int
}

// New returns an overlay on top of li, storing upper content in be.
func New(li *LowerImage, be *MemBackend) *FS {
	root := &node{
		isDir:     true,
		children:  make(map[string]*node),
		whiteouts: make(map[string]bool),
		ld:        li.root,
	}
	fs := &FS{
		backend:    be,
		root:       root,
		lowerFiles: make(map[*lowerFile]*node),
		lowerDirs:  make(map[*lowerDir]*node),
	}
	fs.lowerDirs[li.root] = root
	return fs
}

// Promotions reports how many lower files have been copied up so far.
func (fs *FS) Promotions() int {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	return fs.promotions
}

// Reclaimed reports how many upper objects have been destroyed after
// losing their last name and last open handle.
func (fs *FS) Reclaimed() int {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	return fs.reclaimed
}

func splitPath(p string) ([]string, error) {
	if !strings.HasPrefix(p, "/") {
		return nil, ErrInvalid
	}
	var out []string
	for _, c := range strings.Split(p, "/") {
		switch c {
		case "", ".":
		case "..":
			return nil, ErrInvalid
		default:
			out = append(out, c)
		}
	}
	return out, nil
}

// childLocked resolves one path component, materializing overlay nodes
// for lower entries on first touch. All names of one lower inode map to
// one shared node. Callers must hold fs.mu.
func (fs *FS) childLocked(dir *node, name string) (*node, error) {
	if !dir.isDir {
		return nil, ErrNotDir
	}
	if c, ok := dir.children[name]; ok {
		return c, nil
	}
	if dir.whiteouts[name] {
		return nil, ErrNotExist
	}
	if dir.ld == nil {
		return nil, ErrNotExist
	}
	e, ok := dir.ld.entries[name]
	if !ok {
		return nil, ErrNotExist
	}
	switch e := e.(type) {
	case *lowerFile:
		if n, ok := fs.lowerFiles[e]; ok {
			dir.children[name] = n
			return n, nil
		}
		n := &node{lf: e, lowerKey: e, nlink: e.nlink}
		fs.lowerFiles[e] = n
		dir.children[name] = n
		return n, nil
	case *lowerDir:
		if n, ok := fs.lowerDirs[e]; ok {
			dir.children[name] = n
			return n, nil
		}
		n := &node{
			isDir:     true,
			parent:    dir,
			children:  make(map[string]*node),
			whiteouts: make(map[string]bool),
			ld:        e,
		}
		fs.lowerDirs[e] = n
		dir.children[name] = n
		return n, nil
	}
	return nil, ErrNotExist
}

func (fs *FS) resolveLocked(path string) (*node, error) {
	comps, err := splitPath(path)
	if err != nil {
		return nil, err
	}
	n := fs.root
	for _, c := range comps {
		n, err = fs.childLocked(n, c)
		if err != nil {
			return nil, err
		}
	}
	return n, nil
}

func (fs *FS) resolveParentLocked(path string) (*node, string, error) {
	comps, err := splitPath(path)
	if err != nil {
		return nil, "", err
	}
	if len(comps) == 0 {
		return nil, "", ErrInvalid
	}
	n := fs.root
	for _, c := range comps[:len(comps)-1] {
		n, err = fs.childLocked(n, c)
		if err != nil {
			return nil, "", err
		}
	}
	if !n.isDir {
		return nil, "", ErrNotDir
	}
	return n, comps[len(comps)-1], nil
}

// whiteoutIfLower records that name, if it came from the lower image,
// must no longer be shown for dir.
func whiteoutIfLower(dir *node, name string) {
	if dir.ld == nil {
		return
	}
	if _, ok := dir.ld.entries[name]; ok {
		dir.whiteouts[name] = true
	}
}

// maybeReclaimLocked destroys a file node once it has neither visible
// names nor open handles. Callers must hold fs.mu.
func (fs *FS) maybeReclaimLocked(n *node) {
	if n.isDir || n.dead || n.nlink > 0 || n.nopen > 0 {
		return
	}
	n.dead = true
	if n.lowerKey != nil {
		delete(fs.lowerFiles, n.lowerKey)
	}
	n.mu.Lock()
	blocks := n.blocks
	n.blocks = nil
	n.lf = nil
	n.size = 0
	n.mu.Unlock()
	for _, id := range blocks {
		fs.backend.Free(id)
	}
	fs.reclaimed++
}

// promote copies a lower-backed file into upper blocks and atomically
// flips the node to the upper object. It holds no namespace lock while
// copying, so renames and unlinks may proceed concurrently; publishing
// touches only the node, never a path. On any error the node is left
// fully lower-backed and partial blocks are freed.
func (fs *FS) promote(n *node) error {
	n.promoMu.Lock()
	defer n.promoMu.Unlock()

	n.mu.Lock()
	lf := n.lf
	n.mu.Unlock()
	if lf == nil {
		return nil // already promoted, or born in the upper layer
	}

	bs := fs.backend.BlockSize()
	var ids []uint64
	cleanup := func() {
		for _, id := range ids {
			fs.backend.Free(id)
		}
	}
	for off := 0; off < len(lf.data); off += bs {
		end := off + bs
		if end > len(lf.data) {
			end = len(lf.data)
		}
		chunk := make([]byte, bs)
		copy(chunk, lf.data[off:end])
		id, err := fs.backend.Alloc()
		if err != nil {
			cleanup()
			return err
		}
		if err := fs.backend.Put(id, chunk); err != nil {
			fs.backend.Free(id)
			cleanup()
			return err
		}
		ids = append(ids, id)
	}

	n.mu.Lock()
	if n.lf == lf {
		n.blocks = ids
		n.size = len(lf.data)
		n.lf = nil
		ids = nil
	}
	n.mu.Unlock()
	if ids != nil {
		// promoMu makes this unreachable; stay defensive and never
		// publish a second copy of the same object.
		cleanup()
		return nil
	}
	fs.mu.Lock()
	fs.promotions++
	fs.mu.Unlock()
	return nil
}

// writeAtLocked writes p at off, growing the block list as needed.
// Callers must hold n.mu and the node must already be promoted.
func (fs *FS) writeAtLocked(n *node, p []byte, off int) error {
	bs := fs.backend.BlockSize()
	need := off + len(p)
	for len(n.blocks)*bs < need {
		id, err := fs.backend.Alloc()
		if err != nil {
			return err
		}
		if err := fs.backend.Put(id, make([]byte, bs)); err != nil {
			fs.backend.Free(id)
			return err
		}
		n.blocks = append(n.blocks, id)
	}
	for len(p) > 0 {
		bi := off / bs
		boff := off % bs
		blk, err := fs.backend.Get(n.blocks[bi])
		if err != nil {
			return err
		}
		if len(blk) < bs {
			blk = append(blk, make([]byte, bs-len(blk))...)
		}
		m := copy(blk[boff:], p)
		if err := fs.backend.Put(n.blocks[bi], blk); err != nil {
			return err
		}
		p = p[m:]
		off += m
	}
	if need > n.size {
		n.size = need
	}
	return nil
}

// readAtLocked reads from the lower image or the upper blocks,
// whichever currently backs the node. Callers must hold n.mu.
func (fs *FS) readAtLocked(n *node, p []byte, off int) (int, error) {
	if n.lf != nil {
		if off >= len(n.lf.data) {
			return 0, errEOF
		}
		return copy(p, n.lf.data[off:]), nil
	}
	if off >= n.size {
		return 0, errEOF
	}
	if rem := n.size - off; len(p) > rem {
		p = p[:rem]
	}
	bs := fs.backend.BlockSize()
	total := 0
	for len(p) > 0 {
		bi := off / bs
		boff := off % bs
		blk, err := fs.backend.Get(n.blocks[bi])
		if err != nil {
			return total, err
		}
		m := copy(p, blk[boff:])
		p = p[m:]
		off += m
		total += m
	}
	return total, nil
}

// Open opens path. With O_CREATE a missing final component is created as
// a new upper file. O_TRUNC truncates writable opens (promoting first).
func (fs *FS) Open(path string, flags int) (*File, error) {
	fs.mu.Lock()
	n, err := fs.resolveLocked(path)
	if err == ErrNotExist && flags&O_CREATE != 0 {
		n, err = fs.createLocked(path)
	}
	if err != nil {
		fs.mu.Unlock()
		return nil, err
	}
	if n.isDir {
		fs.mu.Unlock()
		return nil, ErrIsDir
	}
	n.nopen++
	fs.mu.Unlock()

	if flags&O_TRUNC != 0 && flags&3 != O_RDONLY {
		if err := fs.promote(n); err != nil {
			fs.mu.Lock()
			n.nopen--
			fs.maybeReclaimLocked(n)
			fs.mu.Unlock()
			return nil, err
		}
		n.mu.Lock()
		blocks := n.blocks
		n.blocks = nil
		n.size = 0
		n.mu.Unlock()
		for _, id := range blocks {
			fs.backend.Free(id)
		}
	}
	return &File{fs: fs, node: n, flags: flags}, nil
}

func (fs *FS) createLocked(path string) (*node, error) {
	parent, name, err := fs.resolveParentLocked(path)
	if err != nil {
		return nil, err
	}
	if _, err := fs.childLocked(parent, name); err == nil {
		return nil, ErrExist
	} else if err != ErrNotExist {
		return nil, err
	}
	n := &node{nlink: 1}
	parent.children[name] = n
	delete(parent.whiteouts, name)
	return n, nil
}

// Link creates a new hard link newPath for the file at oldPath.
func (fs *FS) Link(oldPath, newPath string) error {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	n, err := fs.resolveLocked(oldPath)
	if err != nil {
		return err
	}
	if n.isDir {
		return ErrIsDir
	}
	parent, name, err := fs.resolveParentLocked(newPath)
	if err != nil {
		return err
	}
	if _, err := fs.childLocked(parent, name); err == nil {
		return ErrExist
	} else if err != ErrNotExist {
		return err
	}
	parent.children[name] = n
	delete(parent.whiteouts, name)
	n.nlink++
	return nil
}

// Unlink removes a name. The underlying object survives until its last
// name is gone and its last handle is closed.
func (fs *FS) Unlink(path string) error {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	parent, name, err := fs.resolveParentLocked(path)
	if err != nil {
		return err
	}
	child, err := fs.childLocked(parent, name)
	if err != nil {
		return err
	}
	if child.isDir {
		return ErrIsDir
	}
	delete(parent.children, name)
	whiteoutIfLower(parent, name)
	child.nlink--
	fs.maybeReclaimLocked(child)
	return nil
}

// Rename moves oldPath to newPath, moving whole directory subtrees by
// pointer. A lower name vacated by the move is whited out, so the old
// path stays gone. An existing file at newPath is replaced; replacing
// directories is rejected.
func (fs *FS) Rename(oldPath, newPath string) error {
	fs.mu.Lock()
	defer fs.mu.Unlock()

	oldParent, oldName, err := fs.resolveParentLocked(oldPath)
	if err != nil {
		return err
	}
	child, err := fs.childLocked(oldParent, oldName)
	if err != nil {
		return err
	}
	newParent, newName, err := fs.resolveParentLocked(newPath)
	if err != nil {
		return err
	}
	if child.isDir {
		for p := newParent; p != nil; p = p.parent {
			if p == child {
				return ErrInvalid // cannot move a directory into itself
			}
		}
	}
	if victim, err := fs.childLocked(newParent, newName); err == nil {
		if victim == child {
			return nil // rename onto itself
		}
		if victim.isDir || child.isDir {
			return ErrExist
		}
		delete(newParent.children, newName)
		whiteoutIfLower(newParent, newName)
		victim.nlink--
		fs.maybeReclaimLocked(victim)
	} else if err != ErrNotExist {
		return err
	}

	delete(oldParent.children, oldName)
	whiteoutIfLower(oldParent, oldName)
	newParent.children[newName] = child
	delete(newParent.whiteouts, newName)
	if child.isDir {
		child.parent = newParent
	}
	return nil
}

// Mkdir creates an empty upper directory.
func (fs *FS) Mkdir(path string) error {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	parent, name, err := fs.resolveParentLocked(path)
	if err != nil {
		return err
	}
	if _, err := fs.childLocked(parent, name); err == nil {
		return ErrExist
	} else if err != ErrNotExist {
		return err
	}
	parent.children[name] = &node{
		isDir:     true,
		parent:    parent,
		children:  make(map[string]*node),
		whiteouts: make(map[string]bool),
	}
	delete(parent.whiteouts, name)
	return nil
}

// ReadDir lists the merged, whiteout-filtered directory entries, sorted.
func (fs *FS) ReadDir(path string) ([]string, error) {
	fs.mu.Lock()
	defer fs.mu.Unlock()
	n, err := fs.resolveLocked(path)
	if err != nil {
		return nil, err
	}
	if !n.isDir {
		return nil, ErrNotDir
	}
	set := make(map[string]bool)
	for name := range n.children {
		set[name] = true
	}
	if n.ld != nil {
		for name := range n.ld.entries {
			if !n.whiteouts[name] {
				set[name] = true
			}
		}
	}
	out := make([]string, 0, len(set))
	for name := range set {
		out = append(out, name)
	}
	sort.Strings(out)
	return out, nil
}

// Info describes a resolved path.
type Info struct {
	IsDir bool
	Size  int
	Nlink int
}

// Stat resolves path without opening it.
func (fs *FS) Stat(path string) (Info, error) {
	fs.mu.Lock()
	n, err := fs.resolveLocked(path)
	fs.mu.Unlock()
	if err != nil {
		return Info{}, err
	}
	if n.isDir {
		return Info{IsDir: true, Nlink: 1}, nil
	}
	n.mu.Lock()
	size := n.size
	if n.lf != nil {
		size = len(n.lf.data)
	}
	n.mu.Unlock()
	fs.mu.Lock()
	nlink := n.nlink
	fs.mu.Unlock()
	return Info{Size: size, Nlink: nlink}, nil
}
