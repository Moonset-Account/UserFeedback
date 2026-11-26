// Package sparseblockvol implements a versioned logical-to-physical mapping
// layer for sparse block volumes.
//
// A Store owns a set of Volumes. Every volume has its own logical block size
// (alignment), so volumes created before and after an alignment-rule upgrade
// keep their historical block numbers and can coexist.
//
// Physical blocks are content addressed: the block id is the hex SHA-256 of
// the block contents. Snapshots and even distinct volumes that hold identical
// bytes therefore share one physical block. Reference counts are not trusted
// on disk; they are recomputed from every reachable version map on open and
// after every commit.
//
// Writes are grouped in transactions whose payload is staged before an atomic
// metadata switch. A merge between two (possibly differently aligned) volumes
// is journaled so that an interrupted merge resumes deterministically while
// every previously published snapshot stays readable.
package sparseblockvol

import (
	"errors"
	"time"
)

// Errors returned by the package. They are wrapped with context where useful,
// so callers should use errors.Is rather than direct comparison.
var (
	ErrClosed        = errors.New("sparseblockvol: store is closed")
	ErrNotFound      = errors.New("sparseblockvol: volume or version not found")
	ErrExists        = errors.New("sparseblockvol: volume already exists")
	ErrBlockSize     = errors.New("sparseblockvol: block size must be a positive power of two")
	ErrBadArgument   = errors.New("sparseblockvol: invalid argument")
	ErrTxClosed      = errors.New("sparseblockvol: transaction already committed or aborted")
	ErrSnapshotName  = errors.New("sparseblockvol: snapshot name conflict")
	ErrPinnedVersion = errors.New("sparseblockvol: version is pinned by a named snapshot")
)

// EntryKind discriminates the three mapping states a logical block can be in.
type EntryKind string

const (
	// KindMapped means the logical block points at a physical content block.
	KindMapped EntryKind = "mapped"
	// KindZero means the block is a deliberately written, known zero segment.
	// It consumes no physical block.
	KindZero EntryKind = "zero"
	// KindHole means the block was never written or has been punched out.
	// Reads return zero bytes but the block is not considered allocated.
	KindHole EntryKind = "hole"
)

// Entry is the mapping of one logical block within a volume version.
type Entry struct {
	Kind    EntryKind
	BlockID string // only meaningful when Kind == KindMapped
}

// Extent is a contiguous, type-homogeneous byte range of a volume.
// Offset and Length are byte offsets; Length is always a multiple of the
// owning volume's block size.
type Extent struct {
	Kind   EntryKind
	Offset int64
	Length int64
}

// Version describes one published, immutable version of a volume.
type Version struct {
	ID        int64
	ParentID  int64
	CreatedAt time.Time
	Snapshots []string // named snapshots pointing at this version
	Size      int64    // volume size in bytes when the version was published
}

// RecoveryReport summarizes what Open found and repaired on disk.
type RecoveryReport struct {
	// AbortedTxn lists transactions that were staged but never committed;
	// their staged payloads were removed and no mapping change was applied.
	AbortedTxn []int64
	// ResumedMerge lists volume ids whose merge journal was replayed.
	ResumedMerge []string
	// MergeCommitted is true when a journal was already committed just
	// before the crash; replay only finished its cleanup.
	MergeCommitted bool
	// OrphanedBlocks are physical blocks that existed on disk but were not
	// referenced by any committed mapping after repair.
	OrphanedBlocks []string
	// MissingBlocks are physical blocks referenced by a mapping but absent
	// from the block directory. In normal operation this stays empty.
	MissingBlocks []string
}
