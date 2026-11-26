package overlay

// LowerImage is a read-only base filesystem: a tree of directories and
// files, where files may be hard-linked under several names. It is built
// once and never mutated afterwards; the overlay only reads from it.
type LowerImage struct {
	root *lowerDir
}

type lowerEntry interface{ lowerEntry() }

type lowerFile struct {
	data  []byte // immutable content
	nlink int    // total number of names in the lower image
}

func (*lowerFile) lowerEntry() {}

type lowerDir struct {
	entries map[string]lowerEntry
}

func (*lowerDir) lowerEntry() {}

// NewLowerImage returns an empty image containing only the root directory.
func NewLowerImage() *LowerImage {
	return &LowerImage{root: &lowerDir{entries: make(map[string]lowerEntry)}}
}

// Mkdir creates path and any missing parents (mkdir -p semantics).
func (l *LowerImage) Mkdir(path string) error {
	comps, err := splitPath(path)
	if err != nil {
		return err
	}
	d := l.root
	for _, c := range comps {
		e, ok := d.entries[c]
		if !ok {
			nd := &lowerDir{entries: make(map[string]lowerEntry)}
			d.entries[c] = nd
			d = nd
			continue
		}
		nd, ok := e.(*lowerDir)
		if !ok {
			return ErrNotDir
		}
		d = nd
	}
	return nil
}

// AddFile creates a file with the given content, making parents as needed.
func (l *LowerImage) AddFile(path string, data []byte) error {
	comps, err := splitPath(path)
	if err != nil {
		return err
	}
	if len(comps) == 0 {
		return ErrInvalid
	}
	d := l.root
	for _, c := range comps[:len(comps)-1] {
		e, ok := d.entries[c]
		if !ok {
			nd := &lowerDir{entries: make(map[string]lowerEntry)}
			d.entries[c] = nd
			d = nd
			continue
		}
		nd, ok := e.(*lowerDir)
		if !ok {
			return ErrNotDir
		}
		d = nd
	}
	name := comps[len(comps)-1]
	if _, ok := d.entries[name]; ok {
		return ErrExist
	}
	d.entries[name] = &lowerFile{data: append([]byte(nil), data...), nlink: 1}
	return nil
}

// Link adds a hard link: linkPath becomes another name for the file at
// existing. Both paths must live in the lower image.
func (l *LowerImage) Link(existing, linkPath string) error {
	e, err := l.lookup(existing)
	if err != nil {
		return err
	}
	lf, ok := e.(*lowerFile)
	if !ok {
		return ErrIsDir
	}
	comps, err := splitPath(linkPath)
	if err != nil {
		return err
	}
	if len(comps) == 0 {
		return ErrInvalid
	}
	d := l.root
	for _, c := range comps[:len(comps)-1] {
		e, ok := d.entries[c]
		if !ok {
			return ErrNotExist
		}
		nd, ok := e.(*lowerDir)
		if !ok {
			return ErrNotDir
		}
		d = nd
	}
	name := comps[len(comps)-1]
	if _, ok := d.entries[name]; ok {
		return ErrExist
	}
	lf.nlink++
	d.entries[name] = lf
	return nil
}

func (l *LowerImage) lookup(path string) (lowerEntry, error) {
	comps, err := splitPath(path)
	if err != nil {
		return nil, err
	}
	if len(comps) == 0 {
		return l.root, nil
	}
	d := l.root
	for i, c := range comps {
		e, ok := d.entries[c]
		if !ok {
			return nil, ErrNotExist
		}
		if i == len(comps)-1 {
			return e, nil
		}
		d, ok = e.(*lowerDir)
		if !ok {
			return nil, ErrNotDir
		}
	}
	return nil, ErrNotExist
}
