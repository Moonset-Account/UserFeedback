// Package rootfs builds and queries a layered, in-memory root filesystem.
//
// A caller supplies one Layer per image layer. Each layer holds path records:
// regular files with content, directories, ordinary whiteout markers, and
// opaque-directory markers. Build merges the layers into an immutable image
// that keeps a separate view for every layer prefix, so a caller can ask what
// the merged filesystem looked like at layer N and still ask what it looked
// like at an earlier layer.
package rootfs

import (
	"fmt"
	"path"
	"sort"
	"strings"
)

// Image is an immutable merged view of all layers passed to Build. One view is
// retained per layer prefix. Queries are read-only and safe for concurrent use.
type Image struct {
	views []*node
}

// node is one item in a merged view. Directories have a non-nil children map;
// files carry data. layer is the zero-based layer that supplied the item; the
// synthetic root uses -1.
type node struct {
	isDir    bool
	data     []byte
	layer    int
	children map[string]*node
}

func newRoot() *node {
	return &node{isDir: true, layer: -1, children: make(map[string]*node)}
}

func cloneNode(n *node) *node {
	if n == nil {
		return nil
	}
	c := &node{isDir: n.isDir, layer: n.layer}
	if n.isDir {
		c.children = make(map[string]*node, len(n.children))
		for name, child := range n.children {
			c.children[name] = cloneNode(child)
		}
	} else {
		c.data = append([]byte(nil), n.data...)
	}
	return c
}

// Build merges layers in order and returns an immutable Image. Entries inside a
// layer may be supplied in any order. Build does not retain the caller's
// slices; content bytes are copied.
func Build(layers []Layer) (*Image, error) {
	img := &Image{views: make([]*node, 0, len(layers))}
	view := newRoot()

	for i := range layers {
		norm, err := normalizeLayer(i, layers[i])
		if err != nil {
			return nil, err
		}
		if i > 0 {
			view = cloneNode(view)
		}
		applyLayer(view, norm)
		img.views = append(img.views, view)
	}
	return img, nil
}

// NumLayers reports how many layer views are available. Valid layer indexes are
// [0, NumLayers()).
func (img *Image) NumLayers() int {
	if img == nil {
		return 0
	}
	return len(img.views)
}

// ReadFile returns the bytes of the regular file at p as visible at layer.
func (img *Image) ReadFile(layer int, p string) (FileContent, error) {
	segs, canon, err := canonicalPath(p)
	if err != nil {
		return FileContent{}, err
	}
	view, err := img.viewAt(layer)
	if err != nil {
		return FileContent{}, err
	}
	n, ok := find(view, segs)
	if !ok {
		return FileContent{}, fmt.Errorf("%w: layer %d path %q", ErrNotExist, layer, canon)
	}
	if n.isDir {
		return FileContent{}, fmt.Errorf("%w: layer %d path %q", ErrIsDir, layer, canon)
	}
	data := append([]byte(nil), n.data...)
	return FileContent{Path: canon, Data: data, Size: len(data), Layer: n.layer}, nil
}

// Stat reports whether p exists at layer and where it came from.
func (img *Image) Stat(layer int, p string) (FileInfo, error) {
	segs, canon, err := canonicalPath(p)
	if err != nil {
		return FileInfo{}, err
	}
	view, err := img.viewAt(layer)
	if err != nil {
		return FileInfo{}, err
	}
	if len(segs) == 0 {
		return FileInfo{Path: "/", IsDir: true, Layer: -1}, nil
	}
	n, ok := find(view, segs)
	if !ok {
		return FileInfo{}, fmt.Errorf("%w: layer %d path %q", ErrNotExist, layer, canon)
	}
	fi := FileInfo{Path: canon, IsDir: n.isDir, Layer: n.layer}
	if !n.isDir {
		fi.Size = len(n.data)
	}
	return fi, nil
}

// ListDir returns the direct, visible children of directory p at layer, sorted
// by name. The root path "/" may be listed.
func (img *Image) ListDir(layer int, p string) ([]DirEntry, error) {
	segs, canon, err := canonicalPath(p)
	if err != nil {
		return nil, err
	}
	view, err := img.viewAt(layer)
	if err != nil {
		return nil, err
	}
	n := view
	if len(segs) > 0 {
		var ok bool
		n, ok = find(view, segs)
		if !ok {
			return nil, fmt.Errorf("%w: layer %d path %q", ErrNotExist, layer, canon)
		}
	}
	if !n.isDir {
		return nil, fmt.Errorf("%w: layer %d path %q", ErrNotDir, layer, canon)
	}
	entries := make([]DirEntry, 0, len(n.children))
	for name, child := range n.children {
		e := DirEntry{Name: name, IsDir: child.isDir, Layer: child.layer}
		if !child.isDir {
			e.Size = len(child.data)
		}
		entries = append(entries, e)
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name < entries[j].Name })
	return entries, nil
}

func (img *Image) viewAt(layer int) (*node, error) {
	if img == nil || layer < 0 || layer >= len(img.views) {
		return nil, fmt.Errorf("%w: %d (have %d layers)", ErrLayerOutOfRange, layer, img.NumLayers())
	}
	return img.views[layer], nil
}

// canonicalPath validates and normalizes a layer-entry or query path. Leading
// slashes are optional, "." segments are dropped, and a path above the root is
// rejected rather than resolved. The returned segments are empty for "/".
func canonicalPath(p string) ([]string, string, error) {
	if p == "" || strings.ContainsRune(p, 0) {
		return nil, "", fmt.Errorf("%w: %q", ErrInvalidPath, p)
	}
	for _, part := range strings.Split(p, "/") {
		if part == ".." {
			return nil, "", fmt.Errorf("%w: %q contains \"..\"", ErrInvalidPath, p)
		}
	}
	canon := path.Clean("/" + p)
	if canon == "/" {
		return nil, "/", nil
	}
	return strings.Split(strings.TrimPrefix(canon, "/"), "/"), canon, nil
}

func find(root *node, segs []string) (*node, bool) {
	cur := root
	for _, s := range segs {
		if !cur.isDir {
			return nil, false
		}
		next, ok := cur.children[s]
		if !ok {
			return nil, false
		}
		cur = next
	}
	return cur, true
}

func remove(root *node, segs []string) {
	if len(segs) == 0 {
		return
	}
	parent, ok := find(root, segs[:len(segs)-1])
	if !ok || !parent.isDir {
		return
	}
	delete(parent.children, segs[len(segs)-1])
}

// ensureDir makes the path a directory. Missing ancestors are created with
// layer origin idx. An existing file in the way is replaced. If setOrigin is
// true the final directory's origin becomes idx (an explicit directory or
// opaque marker); inherited children are kept.
func ensureDir(root *node, segs []string, idx int, setOrigin bool) {
	cur := root
	for i, s := range segs {
		child, ok := cur.children[s]
		if !ok || !child.isDir {
			child = &node{isDir: true, layer: idx, children: make(map[string]*node)}
			cur.children[s] = child
		} else if i == len(segs)-1 && setOrigin {
			child.layer = idx
		}
		cur = child
	}
}

// addFile places a file at the path, replacing any existing item and, for a
// directory, its whole subtree. Missing or non-directory ancestors are turned
// into directories first.
func addFile(root *node, segs []string, idx int, data []byte) {
	parent := root
	if len(segs) > 1 {
		ensureDir(root, segs[:len(segs)-1], idx, false)
		parent, _ = find(root, segs[:len(segs)-1])
	}
	if parent == nil || !parent.isDir {
		return
	}
	parent.children[segs[len(segs)-1]] = &node{
		isDir: false,
		data:  append([]byte(nil), data...),
		layer: idx,
	}
}

// normEntry is a validated, canonicalized entry.
type normEntry struct {
	segs    []string
	path    string
	kind    Kind
	layer   int
	content []byte
}

// normLayer is a validated layer, with entries grouped and sorted so the result
// does not depend on input order.
type normLayer struct {
	whiteouts []normEntry
	opaques   []normEntry
	contents  []normEntry
}

func normalizeLayer(index int, layer Layer) (*normLayer, error) {
	out := &normLayer{}
	contentKinds := make(map[string]Kind)
	markerKinds := make(map[string]Kind)
	allPaths := make(map[string]struct{})

	for _, e := range layer.Entries {
		segs, canon, err := canonicalPath(e.Path)
		if err != nil {
			return nil, fmt.Errorf("layer %d: %w", index, err)
		}
		if len(segs) == 0 {
			return nil, fmt.Errorf("%w: layer %d path %q is the root", ErrInvalidEntry, index, canon)
		}
		switch e.Kind {
		case KindFile:
		case KindDir, KindWhiteout, KindOpaque:
			if len(e.Content) != 0 {
				return nil, fmt.Errorf("%w: layer %d path %q kind %s carries content", ErrInvalidEntry, index, canon, e.Kind)
			}
		default:
			return nil, fmt.Errorf("%w: layer %d path %q has unknown kind %v", ErrInvalidEntry, index, canon, e.Kind)
		}

		switch e.Kind {
		case KindFile, KindDir:
			if prev, ok := contentKinds[canon]; ok {
				if prev == e.Kind {
					return nil, fmt.Errorf("%w: layer %d path %q", ErrDuplicatePath, index, canon)
				}
				return nil, fmt.Errorf("%w: layer %d path %q is both %s and %s", ErrConflict, index, canon, prev, e.Kind)
			}
			// Removals target the inherited view only, so a whiteout may
			// coexist with same-layer content at the same path: the whiteout
			// hides the lower item and the new record replaces it. A directory
			// may also coexist with its own opaque marker. Everything else at
			// one path is contradictory.
			if prev, ok := markerKinds[canon]; ok {
				ok = prev == KindWhiteout || (e.Kind == KindDir && prev == KindOpaque)
				if !ok {
					return nil, fmt.Errorf("%w: layer %d path %q has both %s and %s entries", ErrConflict, index, canon, prev, e.Kind)
				}
			}
			contentKinds[canon] = e.Kind
			ne := normEntry{segs: segs, path: canon, kind: e.Kind, layer: index}
			if e.Kind == KindFile {
				ne.content = append([]byte(nil), e.Content...)
			}
			out.contents = append(out.contents, ne)
		case KindWhiteout, KindOpaque:
			// Whiteout-over-content is explicitly allowed above. An opaque
			// marker only makes sense on a directory, so pairing it with a file
			// at the same path is an error.
			if prev, ok := contentKinds[canon]; ok && !(e.Kind == KindOpaque && prev == KindDir) && e.Kind != KindWhiteout {
				return nil, fmt.Errorf("%w: layer %d path %q has both %s and %s entries", ErrConflict, index, canon, prev, e.Kind)
			}
			if prev, ok := markerKinds[canon]; ok {
				if prev == e.Kind {
					return nil, fmt.Errorf("%w: layer %d path %q", ErrDuplicatePath, index, canon)
				}
				return nil, fmt.Errorf("%w: layer %d path %q has both %s and %s markers", ErrConflict, index, canon, prev, e.Kind)
			}
			markerKinds[canon] = e.Kind
			ne := normEntry{segs: segs, path: canon, kind: e.Kind, layer: index}
			if e.Kind == KindWhiteout {
				out.whiteouts = append(out.whiteouts, ne)
			} else {
				out.opaques = append(out.opaques, ne)
			}
		}
		allPaths[canon] = struct{}{}
	}

	// An opaque marker makes its path a directory, so it cannot sit below a
	// file this same layer places. (Checked before the general ancestor rule so
	// the more specific message wins.)
	for _, o := range out.opaques {
		for i := 1; i < len(o.segs); i++ {
			ancestor := "/" + strings.Join(o.segs[:i], "/")
			if k, ok := contentKinds[ancestor]; ok && k == KindFile {
				return nil, fmt.Errorf("%w: layer %d has file %q and opaque marker %q below it", ErrConflict, index, ancestor, o.path)
			}
		}
	}

	// A regular file cannot be an ancestor of anything, so any content,
	// whiteout or opaque record below it is contradictory. This is a defect in
	// the layer description, not a merge to interpret.
	for p, k := range contentKinds {
		if k != KindFile {
			continue
		}
		prefix := p + "/"
		for q := range allPaths {
			if strings.HasPrefix(q, prefix) {
				return nil, fmt.Errorf("%w: layer %d has file %q and entry %q below it", ErrConflict, index, p, q)
			}
		}
	}

	// Sorting makes merging deterministic regardless of input order.
	sortEntries(out.contents)
	sortEntries(out.whiteouts)
	sortEntries(out.opaques)
	return out, nil
}

func sortEntries(es []normEntry) {
	sort.Slice(es, func(i, j int) bool {
		if len(es[i].segs) != len(es[j].segs) {
			return len(es[i].segs) < len(es[j].segs)
		}
		return es[i].path < es[j].path
	})
}

// applyLayer merges one validated layer into view. All removals are computed
// against the inherited view before this layer's own content is inserted, so
// content placed by the same layer always survives its whiteouts and opaque
// markers.
func applyLayer(view *node, l *normLayer) {
	for _, w := range l.whiteouts {
		remove(view, w.segs)
	}

	for _, o := range l.opaques {
		n, ok := find(view, o.segs)
		if !ok {
			continue
		}
		if n.isDir {
			// Only inherited children are hidden; the directory node itself
			// (and thus the path) stays.
			n.children = make(map[string]*node)
		} else {
			// A lower file cannot be the directory this marker declares.
			remove(view, o.segs)
		}
	}

	for _, c := range l.contents {
		if c.kind == KindDir {
			ensureDir(view, c.segs, c.layer, true)
		} else {
			addFile(view, c.segs, c.layer, c.content)
		}
	}

	for _, o := range l.opaques {
		ensureDir(view, o.segs, o.layer, true)
	}
}
