// Package slotcheck is an offline policy checker for externally visible
// lengths in fixed transmission slots.
//
// The checker correlates real bytes, padding, backlog, clock generations,
// padding allocations, delayed chunks and make-up receipts.  It never uses
// the network and has no third-party dependencies.
package slotcheck

import "strconv"

// Status is the per-slot verdict.
type Status string

const (
	// StatusProvenCompliant means all available evidence is consistent and
	// the verified visible length is allowed by the direction's policy.
	StatusProvenCompliant Status = "proven_compliant" // 已证明符合

	// StatusExceededVisibilityTier means a definitive length, padding,
	// backlog or timing-policy error was found.  Finding codes identify the
	// exact failure; the verdict is the required "outside/exceeded visible
	// tier" class.
	StatusExceededVisibilityTier Status = "exceeded_visibility_tier" // 超出可见档/长度策略失败

	// StatusInsufficientEvidence means no definitive length violation was
	// proved, but a receipt, record, clock identity or external observation
	// is missing or inconsistent.
	StatusInsufficientEvidence Status = "insufficient_evidence" // 证据不足
)

// LabelZH returns the Chinese verdict label from the problem statement.
func (s Status) LabelZH() string {
	switch s {
	case StatusProvenCompliant:
		return "已证明符合"
	case StatusExceededVisibilityTier:
		return "超出可见档"
	case StatusInsufficientEvidence:
		return "证据不足"
	default:
		return string(s)
	}
}

// EmptyPolicy controls generated slots that have no SlotRecord.
type EmptyPolicy string

const (
	// EmptyAsZero treats an absent record as an observed empty slot with
	// visible length zero, then checks zero against the direction policy.
	// This is the default and allows independent directions to accept or
	// reject silence through AllowedLengths.
	EmptyAsZero EmptyPolicy = "check_zero"

	// EmptyRequireRecord requires explicit evidence for every slot.  An
	// absent record produces an evidence gap instead of being assumed zero.
	EmptyRequireRecord EmptyPolicy = "require_record"
)

// Finding codes. They are stable strings for use in tests and JSON pipelines.
const (
	CodeVisibleLengthMismatch      = "visible_length_mismatch"
	CodeTierNotAllowed             = "outside_allowed_tier"
	CodeAboveMaximumVisible        = "above_maximum_visible_length"
	CodeEmptyLengthNotAllowed      = "empty_slot_length_not_allowed"
	CodeSlotOutsideTemplate        = "slot_outside_template"
	CodePaddingSlotBudget          = "padding_budget_per_slot_exceeded"
	CodePaddingEpochBudget         = "padding_budget_per_generation_exceeded"
	CodePaddingDuplicate           = "padding_double_counted"
	CodePaddingResyncDuplicate     = "padding_double_counted_at_resync"
	CodePaddingAllocationConflict  = "padding_allocation_conflict"
	CodePaddingIdentityMissing     = "padding_identity_missing_at_resync"
	CodeBacklogMismatch            = "backlog_equation_mismatch"
	CodeUnknownGeneration          = "unknown_clock_generation"
	CodeMissingExternalObservation = "missing_external_length_observation"
	CodeMissingSlotRecord          = "missing_slot_record"
	CodeReissueRecordMissing       = "reissue_slot_record_missing"
	CodeReceiptMissing             = "makeup_receipt_missing"
	CodeClaimedReceiptMissing      = "claimed_receipt_missing"
	CodeReceiptChunkMismatch       = "receipt_chunk_mismatch"
	CodeReceiptBytesMismatch       = "receipt_bytes_mismatch"
	CodeReceiptOriginMismatch      = "receipt_origin_slot_mismatch"
	CodeReceiptReissueMismatch     = "receipt_reissue_slot_mismatch"
	CodeReceiptReissueMissing      = "receipt_reissue_slot_missing"
	CodeReceiptNotCrossSlot        = "receipt_not_cross_slot"
	CodeReissueNotRecorded         = "reissued_chunk_not_recorded"
	CodeUnknownReissuedChunk       = "reissued_unknown_chunk"
	CodeBacklogReceiptMissing      = "backlog_makeup_receipt_missing"
	CodeBacklogReceiptMismatch     = "backlog_receipt_total_mismatch"
	CodeDuplicateReceiptID         = "duplicate_receipt_id"
	CodeOrphanReceipt              = "orphan_receipt"
	CodeAmbiguousReceipt           = "ambiguous_receipt_for_chunk"
	CodeDuplicateDelayedChunk      = "duplicate_delayed_chunk"
)

// SlotRef is the unambiguous identity of one direction's slot.  Generation
// is part of the key so that a slot index reused after clock resynchronisation
// is not merged with the pre-resync slot.
type SlotRef struct {
	Generation int64  `json:"generation"`
	Slot       int    `json:"slot"`
	Direction  string `json:"direction"`
}

// String returns a compact deterministic coordinate.
func (r SlotRef) String() string {
	return "g" + strconv.FormatInt(r.Generation, 10) + "/s" + strconv.Itoa(r.Slot) + "/" + r.Direction
}

// Epoch describes a clock generation.  Resync marks a generation that began
// with clock resynchronisation.
type Epoch struct {
	Generation int64  `json:"generation"`
	Resync     bool   `json:"resync,omitempty"`
	Note       string `json:"note,omitempty"`
}

// SlotTemplate generates the fixed slot indexes checked for every generation
// and every configured direction.  Either SlotIndexes or FirstSlot/SlotCount
// may be supplied.
type SlotTemplate struct {
	FirstSlot   int   `json:"first_slot,omitempty"`
	SlotCount   int   `json:"slot_count,omitempty"`
	SlotIndexes []int `json:"slot_indexes,omitempty"`
}

func (t SlotTemplate) indexes() []int {
	if len(t.SlotIndexes) > 0 {
		out := append([]int(nil), t.SlotIndexes...)
		sortInts(out)
		return out
	}
	if t.SlotCount <= 0 {
		return nil
	}
	out := make([]int, t.SlotCount)
	for i := 0; i < t.SlotCount; i++ {
		out[i] = t.FirstSlot + i
	}
	return out
}

// LengthTier names an exact externally visible length.
type LengthTier struct {
	Name   string `json:"name"`
	Length int    `json:"length"`
}

// DirectionPolicy is independent for each direction.  A direction may have
// different tiers, padding budgets and empty-slot behaviour.
type DirectionPolicy struct {
	Direction string `json:"direction"`

	// AllowedLengths is the set of permitted exact visible lengths.
	// Include 0 if an empty/silent slot is an allowed exposure tier.
	AllowedLengths []int `json:"allowed_lengths,omitempty"`

	// AllowedTiers is an optional named form of AllowedLengths.
	AllowedTiers []LengthTier `json:"allowed_tiers,omitempty"`

	// MaxVisibleBytes optionally imposes a numeric ceiling in addition to
	// the exact tier set.
	MaxVisibleBytes *int `json:"max_visible_bytes,omitempty"`

	// MaxPaddingBytes bounds padding in one slot.  Nil means unlimited.
	MaxPaddingBytes *int `json:"max_padding_bytes,omitempty"`

	// EpochPaddingBudgetBytes bounds the sum of SlotRecord.PaddingBytes for
	// one direction and clock generation.  Nil means no generation budget.
	EpochPaddingBudgetBytes *int `json:"epoch_padding_budget_bytes,omitempty"`

	// EmptyPolicy controls missing slot records.
	EmptyPolicy EmptyPolicy `json:"empty_policy,omitempty"`

	// RequireExternalObservation makes a missing ObservedVisibleBytes value
	// an evidence gap.  When false, RealBytes+PaddingBytes is used as the
	// accounted visible length.
	RequireExternalObservation bool `json:"require_external_observation,omitempty"`

	// RequirePaddingIDsAtResync requires positive-padding records on either
	// side of a resync boundary to carry allocation IDs.  The nil default is
	// true; set to false explicitly to allow unverified uniqueness.
	RequirePaddingIDsAtResync *bool `json:"require_padding_ids_at_resync,omitempty"`

	// RequireMakeupReceipts defaults to true when delayed chunks exist.
	RequireMakeupReceipts *bool `json:"require_makeup_receipts,omitempty"`
}

// PaddingAllocation identifies one padding grant.  The ID is how the checker
// proves that the same grant is not charged to two slots.
type PaddingAllocation struct {
	ID    string `json:"id"`
	Bytes *int   `json:"bytes,omitempty"`
}

// SlotRecord is the evidence for one generation/slot/direction.
type SlotRecord struct {
	// Promoted generation, slot and direction JSON fields.
	SlotRef

	RecordID string `json:"record_id,omitempty"`

	// ObservedVisibleBytes is the externally observed wire length.  Nil means
	// that no independent observation was supplied.
	ObservedVisibleBytes *int `json:"observed_visible_bytes,omitempty"`

	// RealBytes is the number of real/user bytes actually sent in this slot.
	// It includes backlog bytes sent now; PriorBacklogBytes is therefore not
	// added to RealBytes again.
	RealBytes int `json:"real_bytes"`

	// PaddingBytes is the padding charged to this slot.
	PaddingBytes int `json:"padding_bytes"`

	// Backlog evidence.  When both NewRealBytes and NextBacklogBytes are
	// supplied, the checker verifies:
	//   RealBytes = PriorBacklogBytes + *NewRealBytes - *NextBacklogBytes.
	PriorBacklogBytes int  `json:"prior_backlog_bytes,omitempty"`
	NewRealBytes      *int `json:"new_real_bytes,omitempty"`
	NextBacklogBytes  *int `json:"next_backlog_bytes,omitempty"`

	// PaddingAllocations identify padding grants used by this slot.
	PaddingAllocations []PaddingAllocation `json:"padding_allocations,omitempty"`

	// ReissuedChunkIDs records delayed chunks that were reissued here.
	ReissuedChunkIDs []string `json:"reissued_chunk_ids,omitempty"`

	// BacklogReceiptIDs optionally identifies receipts covering prior
	// backlog bytes sent in this slot.  When omitted, receipts whose
	// Reissue coordinate equals this slot are correlated automatically.
	BacklogReceiptIDs []string `json:"backlog_receipt_ids,omitempty"`
}

// DelayedChunk describes data that did not fit in its origin slot and was
// delayed to another slot.
type DelayedChunk struct {
	ChunkID string   `json:"chunk_id"`
	Bytes   int      `json:"bytes"`
	Origin  SlotRef  `json:"origin"`
	Reissue *SlotRef `json:"reissue,omitempty"`

	// ClaimedReceiptID optionally binds the delay to a specific receipt.
	ClaimedReceiptID string `json:"claimed_receipt_id,omitempty"`
}

// Receipt is a make-up/reissue acknowledgement.
type Receipt struct {
	ID      string   `json:"id"`
	ChunkID string   `json:"chunk_id"`
	Bytes   int      `json:"bytes"`
	Origin  SlotRef  `json:"origin"`
	Reissue *SlotRef `json:"reissue,omitempty"`

	// ObservedAt optionally locates where the receipt itself was logged.
	ObservedAt *SlotRef `json:"observed_at,omitempty"`
}

// Input is the complete offline policy-check request.
type Input struct {
	Generations   []Epoch           `json:"generations,omitempty"`
	Template      SlotTemplate      `json:"template"`
	Policies      []DirectionPolicy `json:"policies"`
	Records       []SlotRecord      `json:"records,omitempty"`
	DelayedChunks []DelayedChunk    `json:"delayed_chunks,omitempty"`
	Receipts      []Receipt         `json:"receipts,omitempty"`
}

// Finding locates one length, receipt or evidence problem.
type Finding struct {
	Code      string   `json:"code"`
	Kind      string   `json:"kind"`
	Message   string   `json:"message"`
	Slot      *SlotRef `json:"slot,omitempty"`
	OtherSlot *SlotRef `json:"other_slot,omitempty"`

	ChunkID   string `json:"chunk_id,omitempty"`
	ReceiptID string `json:"receipt_id,omitempty"`
	PaddingID string `json:"padding_id,omitempty"`

	Expected int `json:"expected,omitempty"`
	Actual   int `json:"actual,omitempty"`
}

// SlotReport is the verdict and located evidence for one slot/direction.
type SlotReport struct {
	Slot          SlotRef `json:"slot"`
	Status        Status  `json:"status"`
	StatusLabelZH string  `json:"status_label_zh"`

	// Empty is true when no SlotRecord was supplied for this coordinate.
	Empty bool `json:"empty,omitempty"`

	ObservedVisibleBytes  *int        `json:"observed_visible_bytes,omitempty"`
	AccountedVisibleBytes int         `json:"accounted_visible_bytes"`
	EffectiveVisibleBytes int         `json:"effective_visible_bytes"`
	SelectedTier          *LengthTier `json:"selected_tier,omitempty"`

	RealBytes           int `json:"real_bytes,omitempty"`
	PaddingBytes        int `json:"padding_bytes,omitempty"`
	PriorBacklogBytes   int `json:"prior_backlog_bytes,omitempty"`
	ClaimedPaddingBytes int `json:"claimed_padding_bytes,omitempty"`

	LengthFindings   []Finding `json:"length_findings,omitempty"`
	ConfirmationGaps []Finding `json:"confirmation_gaps,omitempty"`
	EvidenceGaps     []Finding `json:"evidence_gaps,omitempty"`
}

// Summary contains deterministic counts.
type Summary struct {
	Slots                  int `json:"slots"`
	ProvenCompliant        int `json:"proven_compliant"`
	ExceededVisibilityTier int `json:"exceeded_visibility_tier"`
	InsufficientEvidence   int `json:"insufficient_evidence"`
	LengthFindings         int `json:"length_findings"`
	ConfirmationGaps       int `json:"confirmation_gaps"`
	EvidenceGaps           int `json:"evidence_gaps"`
	GlobalFindings         int `json:"global_findings"`
}

// Report is returned by Check.
type Report struct {
	Slots          []SlotReport `json:"slots"`
	GlobalFindings []Finding    `json:"global_findings,omitempty"`
	Summary        Summary      `json:"summary"`
}

// Ptr is a small helper for optional scalar/struct fields in tests and API
// callers.
func Ptr[T any](v T) *T { return &v }
