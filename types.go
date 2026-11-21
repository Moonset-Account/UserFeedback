package rootfs

import (
	"errors"
	"fmt"
)

// Kind identifies what a layer entry represents.
type Kind int

const (
	// KindFile is a regular file. Its content is carried by Entry.Content.
	KindFile Kind = iota

	// KindDir is a directory.
	KindDir

	// KindWhiteout is an ordinary deletion marker. The entry path names the
	// item to hide: the same path inherited from lower layers and its whole
	// subtree disappear from the merged view at this layer and above.
	KindWhiteout

	// KindOpaque is an opaque-directory marker. The entry path names a
	// directory whose children inherited from lower layers are hidden. The
	// directory itself stays, and children placed by the same layer stay.
	KindOpaque
)

// String returns a human-readable name for the kind.
func (k Kind) String() string {
	switch k {
	case KindFile:
		return "file"
	case KindDir:
		return "dir"
	case KindWhiteout:
		return "whiteout"
	case KindOpaque:
		return "opaque"
	default:
		return fmt.Sprintf("kind(%d)", int(k))
	}
}

// Entry is one record supplied for a layer.
//
// Path is a POSIX path inside the root filesystem. It may be absolute or
// relative; Build cleans it and roots it. Content is only used by KindFile and
// is copied by Build.
type Entry struct {
	Path    string
	Kind    Kind
	Content []byte
}

// File returns a regular-file entry. The content slice is copied.
func File(p string, content []byte) Entry {
	return Entry{Path: p, Kind: KindFile, Content: append([]byte(nil), content...)}
}

// Dir returns a directory entry.
func Dir(p string) Entry {
	return Entry{Path: p, Kind: KindDir}
}

// Whiteout returns an ordinary deletion marker for p.
func Whiteout(p string) Entry {
	return Entry{Path: p, Kind: KindWhiteout}
}

// Opaque returns an opaque-directory marker for p.
func Opaque(p string) Entry {
	return Entry{Path: p, Kind: KindOpaque}
}

// Layer is the input for one image layer. Entries may be supplied in any order;
// the merged result does not depend on that order.
type Layer struct {
	Entries []Entry
}

// FileContent is the result of Image.ReadFile.
type FileContent struct {
	// Path is the canonical absolute path that was read, for example
	// "/etc/app/app.yaml".
	Path string
	// Data is a private copy of the visible file bytes.
	Data []byte
	// Size is len(Data).
	Size int
	// Layer is the zero-based layer that supplied the visible file.
	Layer int
}

// FileInfo is the result of Image.Stat.
type FileInfo struct {
	Path  string
	IsDir bool
	// Size is the file size in bytes, or 0 for a directory.
	Size int
	// Layer is the zero-based layer that supplied the visible item. The
	// synthetic root reports -1.
	Layer int
}

// DirEntry describes one direct child returned by Image.ListDir.
type DirEntry struct {
	Name  string
	IsDir bool
	// Size is the file size in bytes, or 0 for a directory.
	Size int
	// Layer is the zero-based layer that supplied the visible child.
	Layer int
}

var (
	// ErrNotExist reports a path that is absent from the queried layer view.
	ErrNotExist = errors.New("rootfs: path does not exist")

	// ErrNotDir reports ListDir on a regular file.
	ErrNotDir = errors.New("rootfs: path is not a directory")

	// ErrIsDir reports ReadFile on a directory.
	ErrIsDir = errors.New("rootfs: path is a directory")

	// ErrLayerOutOfRange reports a layer index outside [0, NumLayers()).
	ErrLayerOutOfRange = errors.New("rootfs: layer index out of range")

	// ErrInvalidPath reports an entry or query path that is malformed or
	// escapes above the root.
	ErrInvalidPath = errors.New("rootfs: invalid path")

	// ErrInvalidEntry reports an unusable entry, for example a non-file that
	// carries content, an entry for the root itself, or an unknown Kind.
	ErrInvalidEntry = errors.New("rootfs: invalid layer entry")

	// ErrDuplicatePath reports two records of the same sort for one path
	// inside a single layer.
	ErrDuplicatePath = errors.New("rootfs: duplicate path in layer")

	// ErrConflict reports contradictory records inside one layer, for example
	// a file at "a" together with an entry at "a/b".
	ErrConflict = errors.New("rootfs: conflicting entries in layer")
)
