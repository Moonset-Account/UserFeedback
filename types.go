package barrier

import "time"

// Identifier types make call sites harder to swap accidentally.
type (
	RunID         string
	ParticipantID string
	GroupID       string
	BarrierID     string
	RelationID    string
	GrantID       string
	ReceiptID     string
	HandoverID    string
	ReleaseID     string
)

// ParticipantStatus is the current authorization state of a run participant.
type ParticipantStatus string

const (
	ParticipantActive     ParticipantStatus = "active"
	ParticipantRevoked    ParticipantStatus = "revoked"
	ParticipantHandedOver ParticipantStatus = "handed_over"
)

// ParticipantConfig declares one responsible party in a run.
type ParticipantConfig struct {
	ID     ParticipantID `json:"id"`
	Groups []GroupID     `json:"groups,omitempty"`
}

// GroupConfig declares a threshold group. Threshold is the number of distinct
// logical subjects required, not the raw number of receipts.
type GroupConfig struct {
	ID        GroupID         `json:"id"`
	Threshold int             `json:"threshold"`
	Members   []ParticipantID `json:"members,omitempty"`
}

// RunConfig is the complete initial participant and group set for one run.
type RunConfig struct {
	ID           RunID               `json:"id"`
	Participants []ParticipantConfig `json:"participants"`
	Groups       []GroupConfig       `json:"groups"`
}

// ParticipantInfo is a stable copy of participant state.
type ParticipantInfo struct {
	ID              ParticipantID     `json:"id"`
	Groups          []GroupID         `json:"groups,omitempty"`
	Status          ParticipantStatus `json:"status"`
	Successor       ParticipantID     `json:"successor,omitempty"`
	LastHandoverID  HandoverID        `json:"last_handover_id,omitempty"`
	RevokeReason    string            `json:"revoke_reason,omitempty"`
	StatusChangedAt time.Time         `json:"status_changed_at"`
}

// GroupInfo is a stable copy of group threshold and current membership.
type GroupInfo struct {
	ID        GroupID         `json:"id"`
	Threshold int             `json:"threshold"`
	Members   []ParticipantID `json:"members,omitempty"`
}

// RunInfo is a stable copy of a run.
type RunInfo struct {
	ID              RunID             `json:"id"`
	Participants    []ParticipantInfo `json:"participants"`
	Groups          []GroupInfo       `json:"groups"`
	TopologyVersion int64             `json:"topology_version"`
	CreatedAt       time.Time         `json:"created_at"`
}

// GroupRequirement overrides a run group's default threshold for one barrier.
type GroupRequirement struct {
	GroupID   GroupID `json:"group_id"`
	Threshold int     `json:"threshold,omitempty"`
}

// BarrierConfig declares a checkpoint barrier. An empty Groups list means all
// groups in the run must satisfy their configured thresholds.
type BarrierConfig struct {
	ID           BarrierID          `json:"id"`
	CheckpointID string             `json:"checkpoint_id,omitempty"`
	Groups       []GroupRequirement `json:"groups,omitempty"`
}

// Receipt is a prepare acknowledgement submitted by a participant.
//
// If SubjectID is empty or equal to SubmitterID, the receipt is direct.
// Otherwise GrantID may identify the proxy authorization; when empty the
// manager searches for a unique active matching grant.
type Receipt struct {
	RunID        RunID         `json:"run_id"`
	BarrierID    BarrierID     `json:"barrier_id"`
	ReceiptID    ReceiptID     `json:"receipt_id,omitempty"`
	CheckpointID string        `json:"checkpoint_id"`
	GroupID      GroupID       `json:"group_id"`
	SubmitterID  ParticipantID `json:"submitter_id"`
	SubjectID    ParticipantID `json:"subject_id,omitempty"`
	GrantID      GrantID       `json:"grant_id,omitempty"`
	Proof        string        `json:"proof,omitempty"`
}

// ReceiptRecord adds server-managed ordering and time to a Receipt.
type ReceiptRecord struct {
	Receipt
	Order      int64     `json:"order"`
	ReceivedAt time.Time `json:"received_at"`
}

// SubmitReceiptResult is returned from receipt submission.
type SubmitReceiptResult struct {
	Receipt    *ReceiptRecord `json:"receipt"`
	Duplicate  bool           `json:"duplicate"`
	Evaluation *Evaluation    `json:"evaluation,omitempty"`
}

// MirrorRelation records an active or revoked mirror edge. Mirrored direct
// receipts collapse to one logical subject for threshold counting.
type MirrorRelation struct {
	ID        RelationID    `json:"id"`
	RunID     RunID         `json:"run_id"`
	A         ParticipantID `json:"a"`
	B         ParticipantID `json:"b"`
	Active    bool          `json:"active"`
	CreatedAt time.Time     `json:"created_at"`
	RevokedAt *time.Time    `json:"revoked_at,omitempty"`
	Reason    string        `json:"reason,omitempty"`
}

// ProxyAuthorization authorizes ProxyID to submit for PrincipalID.
type ProxyAuthorization struct {
	GrantID     GrantID       `json:"grant_id,omitempty"`
	RunID       RunID         `json:"run_id"`
	ProxyID     ParticipantID `json:"proxy_id"`
	PrincipalID ParticipantID `json:"principal_id"`
	Groups      []GroupID     `json:"groups,omitempty"`
}

// Grant is a stored proxy authorization.
type Grant struct {
	ID          GrantID       `json:"id"`
	RunID       RunID         `json:"run_id"`
	ProxyID     ParticipantID `json:"proxy_id"`
	PrincipalID ParticipantID `json:"principal_id"`
	Groups      []GroupID     `json:"groups"`
	Active      bool          `json:"active"`
	CreatedAt   time.Time     `json:"created_at"`
	RevokedAt   *time.Time    `json:"revoked_at,omitempty"`
	Reason      string        `json:"reason,omitempty"`
}

// HandOverRequest retires From and replaces it by To in every current group.
// To may be a new participant ID; it then inherits From's group memberships.
type HandOverRequest struct {
	RunID RunID         `json:"run_id"`
	From  ParticipantID `json:"from"`
	To    ParticipantID `json:"to"`
}

// HandOverRecord is the provenance for a participant handoff.
type HandOverRecord struct {
	ID              HandoverID    `json:"id"`
	RunID           RunID         `json:"run_id"`
	From            ParticipantID `json:"from"`
	To              ParticipantID `json:"to"`
	Groups          []GroupID     `json:"groups"`
	TopologyVersion int64         `json:"topology_version"`
	At              time.Time     `json:"at"`
}

// Contribution is one distinct threshold vote and every currently valid
// receipt that proves it.
type Contribution struct {
	// SubjectID is the represented principal for a proxy receipt, otherwise
	// the direct submitter.
	SubjectID ParticipantID `json:"subject_id"`
	// LogicalID is the mirror equivalence identity used for deduplication.
	LogicalID ParticipantID   `json:"logical_id"`
	GroupID   GroupID         `json:"group_id"`
	Receipts  []ReceiptRecord `json:"receipts"`
}

// InvalidReceipt explains why a stored receipt is not currently counted.
type InvalidReceipt struct {
	Receipt ReceiptRecord `json:"receipt"`
	Code    string        `json:"code"`
	Reason  string        `json:"reason"`
}

// ProofRequest describes one logical identity that could supply a missing
// proof and the participant paths currently accepted for it.
type ProofRequest struct {
	RunID              RunID           `json:"run_id"`
	BarrierID          BarrierID       `json:"barrier_id"`
	CheckpointID       string          `json:"checkpoint_id"`
	GroupID            GroupID         `json:"group_id"`
	LogicalID          ParticipantID   `json:"logical_id"`
	Principals         []ParticipantID `json:"principals,omitempty"`
	DirectSubmitters   []ParticipantID `json:"direct_submitters,omitempty"`
	ProxySubmitters    []ParticipantID `json:"proxy_submitters,omitempty"`
	AcceptedSubmitters []ParticipantID `json:"accepted_submitters,omitempty"`
	Reason             string          `json:"reason"`
}

// MissingProof is the current gap for one group threshold.
type MissingProof struct {
	RunID              RunID           `json:"run_id"`
	BarrierID          BarrierID       `json:"barrier_id"`
	CheckpointID       string          `json:"checkpoint_id"`
	GroupID            GroupID         `json:"group_id"`
	Threshold          int             `json:"threshold"`
	ProofCount         int             `json:"proof_count"`
	MissingCount       int             `json:"missing_count"`
	Impossible         bool            `json:"impossible,omitempty"`
	EligibleLogicalIDs []ParticipantID `json:"eligible_logical_ids,omitempty"`
	AwaitingLogicalIDs []ParticipantID `json:"awaiting_logical_ids,omitempty"`
	Requests           []ProofRequest  `json:"requests,omitempty"`
	Reason             string          `json:"reason,omitempty"`
}

// GroupStatus is the recomputed state of one barrier group.
type GroupStatus struct {
	GroupID            GroupID             `json:"group_id"`
	Threshold          int                 `json:"threshold"`
	ProofCount         int                 `json:"proof_count"`
	CurrentlySatisfied bool                `json:"currently_satisfied"`
	Releasable         bool                `json:"releasable"`
	Published          bool                `json:"published"`
	Proofs             []Contribution      `json:"proofs,omitempty"`
	InvalidReceipts    []InvalidReceipt    `json:"invalid_receipts,omitempty"`
	PublishedRelease   *GroupReleaseRecord `json:"published_release,omitempty"`
	Missing            *MissingProof       `json:"missing,omitempty"`
}

// Evaluation is a read-only point-in-time calculation.
type Evaluation struct {
	RunID            RunID          `json:"run_id"`
	BarrierID        BarrierID      `json:"barrier_id"`
	CheckpointID     string         `json:"checkpoint_id"`
	Ready            bool           `json:"ready"`
	ReleasableGroups []GroupID      `json:"releasable_groups,omitempty"`
	Groups           []GroupStatus  `json:"groups"`
	Missing          []MissingProof `json:"missing,omitempty"`
	TopologyVersion  int64          `json:"topology_version"`
	EvaluatedAt      time.Time      `json:"evaluated_at"`
}

// GroupReleaseRecord is an immutable published group quorum certificate.
type GroupReleaseRecord struct {
	ReleaseID       ReleaseID       `json:"release_id"`
	RunID           RunID           `json:"run_id"`
	BarrierID       BarrierID       `json:"barrier_id"`
	GroupID         GroupID         `json:"group_id"`
	CheckpointID    string          `json:"checkpoint_id"`
	Threshold       int             `json:"threshold"`
	Proofs          []Contribution  `json:"proofs"`
	SourceReceipts  []ReceiptRecord `json:"source_receipts"`
	TopologyVersion int64           `json:"topology_version"`
	PublishedAt     time.Time       `json:"published_at"`
}

// ReleaseRecord is an immutable published barrier success.
type ReleaseRecord struct {
	ReleaseID       ReleaseID            `json:"release_id"`
	RunID           RunID                `json:"run_id"`
	BarrierID       BarrierID            `json:"barrier_id"`
	CheckpointID    string               `json:"checkpoint_id"`
	Groups          []GroupReleaseRecord `json:"groups"`
	SourceReceipts  []ReceiptRecord      `json:"source_receipts"`
	TopologyVersion int64                `json:"topology_version"`
	PublishedAt     time.Time            `json:"published_at"`
}

// PublishGroupOutcome reports either a new/idempotent group release or the
// current status that prevented publication.
type PublishGroupOutcome struct {
	Released         bool                `json:"released"`
	AlreadyPublished bool                `json:"already_published"`
	Release          *GroupReleaseRecord `json:"release,omitempty"`
	Status           *GroupStatus        `json:"status,omitempty"`
	Evaluation       *Evaluation         `json:"evaluation,omitempty"`
}

// ReleaseOutcome reports an atomic barrier release attempt.
type ReleaseOutcome struct {
	Released        bool           `json:"released"`
	AlreadyReleased bool           `json:"already_released"`
	Release         *ReleaseRecord `json:"release,omitempty"`
	Evaluation      *Evaluation    `json:"evaluation,omitempty"`
}

// BarrierView is one barrier in a PollResult.
type BarrierView struct {
	BarrierID    BarrierID      `json:"barrier_id"`
	CheckpointID string         `json:"checkpoint_id"`
	Released     bool           `json:"released"`
	Release      *ReleaseRecord `json:"release,omitempty"`
	Evaluation   *Evaluation    `json:"evaluation,omitempty"`
}

// PollResult returns all barriers in a run.
type PollResult struct {
	RunID           RunID         `json:"run_id"`
	TopologyVersion int64         `json:"topology_version"`
	Barriers        []BarrierView `json:"barriers"`
}
