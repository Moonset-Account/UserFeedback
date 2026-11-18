// Package cloneplan implements clone planning and publication for sparse disk
// snapshot chains in the disaster-recovery platform.
//
// It takes fixed-version parent-child snapshots, block-cover ranges, data
// classifications and per-range sanitization receipts, decides the final
// visible source of every segment according to the snapshot layers, and only
// publishes ranges the destination tenant owns or holds valid sanitization
// receipts for. The final parent-chain version verification and the visibility
// swap happen in one atomic commit action; a failed draft never becomes
// visible.
package cloneplan

import "errors"

// Range is a half-open block-cover interval [Start, End) expressed in virtual
// disk block numbers.
type Range struct {
	Start uint64
	End   uint64
}

// Valid reports whether the range is well-formed and non-empty.
func (r Range) Valid() bool { return r.End > r.Start }

func (r Range) overlaps(o Range) bool {
	return r.Start < o.End && o.Start < r.End
}

// intersect returns the overlap of r and o.
func (r Range) intersect(o Range) (Range, bool) {
	s := r.Start
	if o.Start > s {
		s = o.Start
	}
	e := r.End
	if o.End < e {
		e = o.End
	}
	if s >= e {
		return Range{}, false
	}
	return Range{Start: s, End: e}, true
}

func rangeContained(inner, outer Range) bool {
	return inner.Start >= outer.Start && inner.End <= outer.End
}

func minU64(a, b uint64) uint64 {
	if a < b {
		return a
	}
	return b
}

func maxU64(a, b uint64) uint64 {
	if a > b {
		return a
	}
	return b
}

// Classification of the data carried by a block version.
type Classification string

const (
	ClassPublic    Classification = "public"
	ClassInternal  Classification = "internal"
	ClassSensitive Classification = "sensitive"
)

// BlockRecord is one layer's fixed version of a block range.
type BlockRecord struct {
	Range          Range
	BlockVersion   string
	Classification Classification
	OwnerTenant    string
	// Zero marks an explicit zero-write layer. Explicit zero ranges keep
	// their zero semantics and shadow every ancestor version beneath them.
	Zero bool
	// Discardable marks sensitive data the source tenant permits to be
	// discarded (zeroed) instead of being cloned. Only sensitive blocks may
	// carry this marker; non-discardable sensitive data without a receipt
	// blocks publication.
	Discardable bool
}

// SnapshotRef identifies an immutable, fixed-version snapshot layer.
type SnapshotRef struct {
	SnapshotID string
	Version    string
}

// String renders the fixed version as "snapshotID@version".
func (r SnapshotRef) String() string { return r.SnapshotID + "@" + r.Version }

// Snapshot is an immutable parent-child snapshot layer. A nil Parent marks a
// root snapshot.
type Snapshot struct {
	Ref     SnapshotRef
	Parent  *SnapshotRef
	Records []BlockRecord
}

// SanitizationReceipt is a per-range purge receipt issued for a destination
// tenant. A receipt only applies when BlockVersion matches the block version
// that actually becomes effective over its covered range: a receipt for a
// stale, shadowed version is invalid.
type SanitizationReceipt struct {
	ReceiptID    string
	TargetTenant string
	Range        Range
	BlockVersion string
}

// Decision enumerates what the planner may do with a resolved segment.
type Decision string

const (
	// DecisionPublish exposes the resolved block version as-is.
	DecisionPublish Decision = "publish"
	// DecisionZero replaces the segment content with explicit zero bytes.
	DecisionZero Decision = "zero"
	// DecisionBlocked prevents the whole publication.
	DecisionBlocked Decision = "blocked"
)

// PlanRequest is the input of a clone planning run.
type PlanRequest struct {
	TargetTenant string
	Leaf         SnapshotRef
	CloneRange   Range
	Receipts     []SanitizationReceipt
}

// ResolvedSegment is the per-segment output of parent-chain resolution,
// before the publication policy is applied.
type ResolvedSegment struct {
	Range Range
	// Origin is the layer that supplies the content; nil for a sparse hole
	// that terminates in zero.
	Origin           *SnapshotRef
	EffectiveVersion string
	// Zero is true for both explicit zero-write layers and sparse holes.
	Zero bool
	// ExplicitZero distinguishes an explicit zero-write layer from a sparse
	// hole; both publish as zero content but carry different semantics.
	ExplicitZero   bool
	Classification Classification
	OwnerTenant    string
	// Discardable carries the source layer's discard marker for sensitive
	// data; only meaningful when Classification is sensitive.
	Discardable bool
	Reason      string
}

// PlannedSegment is the final per-segment publication decision.
type PlannedSegment struct {
	Range          Range
	Decision       Decision
	Origin         *SnapshotRef
	BlockVersion   string
	Classification Classification
	OwnerTenant    string
	Discardable    bool
	Zero           bool
	ExplicitZero   bool
	Reason         string
}

// ClonePlan is an immutable planning result.
type ClonePlan struct {
	PlanID         string
	TargetTenant   string
	Leaf           SnapshotRef
	CloneRange     Range
	PinnedChain    []SnapshotRef
	Resolved       []ResolvedSegment
	Segments       []PlannedSegment
	Receipts       []SanitizationReceipt
	BlockedReasons []string
}

// Blocked reports whether the plan cannot be published.
func (p *ClonePlan) Blocked() bool {
	for _, seg := range p.Segments {
		if seg.Decision == DecisionBlocked {
			return true
		}
	}
	return false
}

// PublishedClone is a visible, committed clone.
type PublishedClone struct {
	PlanID       string
	Sequence     int64
	TargetTenant string
	Leaf         SnapshotRef
	CloneRange   Range
	ParentChain  []SnapshotRef
	Segments     []PlannedSegment
}

// Resolution reasons.
const (
	ReasonResolved     = "resolved"
	ReasonExplicitZero = "explicit-zero"
	ReasonSparseHole   = "sparse-hole-zero"
)

// Blocked-policy reasons.
const (
	BlockedMissingReceipt = "missing-sanitization-receipt"
)

// Sentinel errors; callers are expected to use errors.Is against them.
var (
	ErrInvalidRange          = errors.New("invalid block range")
	ErrEmptyTenant           = errors.New("target tenant must not be empty")
	ErrSnapshotNotFound      = errors.New("snapshot not found")
	ErrDuplicateSnapshot     = errors.New("snapshot already registered")
	ErrParentNotFound        = errors.New("parent snapshot not found")
	ErrSnapshotCycle         = errors.New("snapshot parent chain contains a cycle")
	ErrOverlappingRecords    = errors.New("snapshot records overlap")
	ErrDuplicateReceiptID    = errors.New("duplicate sanitization receipt id")
	ErrReceiptTenantMismatch = errors.New("receipt target tenant does not match plan target tenant")
	ErrReceiptOutOfRange     = errors.New("receipt range is not contained in the clone range")
	ErrReceiptStaleVersion   = errors.New("receipt block version does not match the effective block version")
	ErrReceiptCoversZero     = errors.New("receipt covers a non-data explicit-zero or sparse-hole range")
	ErrPublicationBlocked    = errors.New("publication blocked: segment lacks ownership or sanitization")
	ErrPlanNotFound          = errors.New("clone plan not found")
	ErrPlanBlocked           = errors.New("clone plan is blocked and cannot be staged")
	ErrAlreadyStaged         = errors.New("clone plan is already staged or committed")
	ErrChainMismatch         = errors.New("parent chain verification failed at commit")
)
