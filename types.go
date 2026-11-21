// Package expertparallel implements a single-round dispatch/reclaim component for
// expert-parallel token routing.
//
// A round runs in three phases:
//
//  1. Collecting: every participating rank submits exactly one batch of tokens.
//     A token carries a globally unique ID, its original position inside the
//     rank's batch, and at most two (expert, positive-weight) candidates.
//  2. Dispatch (only after every rank's batch has been collected): for each
//     expert, all candidates from every rank are sorted by weight descending
//     and then token ID ascending, and the first expert-capacity entries are
//     accepted. The accepted entries become work items grouped into per-expert
//     work packages.
//  3. Reclaim: return packets, each addressed by work-item identity and
//     carrying an equal-length numeric vector, may arrive in any order and may
//     be re-sent. Re-sent packets are deduplicated and can never make a token
//     "complete" ahead of schedule. When every distinct accepted work item has
//     a vector, the round can be finalized.
//
// On finalization, each token merges the vectors of the experts that actually
// accepted it, re-normalizing over the *accepted* weights (not the original
// candidate weights). A token accepted by no expert gets an all-zero vector and
// is flagged as overflow. Results are restored to every rank's original batch
// positions.
package expertparallel

import "errors"

// Errors returned by the component. They are sentinel errors so callers can use
// errors.Is.
var (
	// ErrNoRanks is returned when a round is created without participating ranks.
	ErrNoRanks = errors.New("expertparallel: at least one participating rank is required")
	// ErrNoExperts is returned when a round is created without experts.
	ErrNoExperts = errors.New("expertparallel: at least one expert is required")
	// ErrInvalidExpert is returned for duplicate/unknown expert configuration or
	// candidate references.
	ErrInvalidExpert = errors.New("expertparallel: invalid expert specification")
	// ErrInvalidCapacity is returned when an expert has a negative capacity.
	ErrInvalidCapacity = errors.New("expertparallel: expert capacity must be non-negative")
	// ErrInvalidVectorLength is returned when the configured vector length is not positive.
	ErrInvalidVectorLength = errors.New("expertparallel: vector length must be positive")
	// ErrInvalidRank is returned for unknown or duplicate rank configuration.
	ErrInvalidRank = errors.New("expertparallel: invalid rank specification")
	// ErrNotCollecting is returned when a batch is submitted before construction
	// finishes validation, or after dispatch has already happened.
	ErrNotCollecting = errors.New("expertparallel: round is not accepting batches")
	// ErrDuplicateRank is returned when a rank submits more than one batch.
	ErrDuplicateRank = errors.New("expertparallel: rank already submitted a batch")
	// ErrUnknownToken is returned when a batch contains a token whose global ID
	// was already seen in this round.
	ErrUnknownToken = errors.New("expertparallel: duplicate token global ID")
	// ErrInvalidPosition is returned when a token's batch position is out of range
	// or collides with another token in the same rank batch.
	ErrInvalidPosition = errors.New("expertparallel: invalid or duplicated token batch position")
	// ErrInvalidCandidate is returned when a token has too many candidates,
	// references an unknown expert, or carries a non-positive weight.
	ErrInvalidCandidate = errors.New("expertparallel: invalid candidate")
	// ErrDuplicateCandidate is returned when both candidates of a token name the
	// same expert: a token can be a candidate for an expert at most once.
	ErrDuplicateCandidate = errors.New("expertparallel: duplicate expert on a token")
	// ErrNotDispatched is returned when returns are submitted or results are
	// requested before Dispatch has run.
	ErrNotDispatched = errors.New("expertparallel: round has not dispatched yet")
	// ErrUnknownWorkItem is returned when a packet references a work item that
	// was never produced by dispatch.
	ErrUnknownWorkItem = errors.New("expertparallel: unknown work item")
	// ErrVectorLengthMismatch is returned when a return vector does not have the
	// configured length.
	ErrVectorLengthMismatch = errors.New("expertparallel: return vector length mismatch")
	// ErrIncomplete is returned when results are requested before every distinct
	// accepted work item has a return vector.
	ErrIncomplete = errors.New("expertparallel: not all work items have returned")
)

// Candidate is one expert nomination carried by a token. A token carries at most
// two of them. Weight must be a positive integer.
type Candidate struct {
	ExpertID string
	Weight   int
}

// Token is one input token. TokenID is globally unique within the round,
// BatchPosition is the token's position in the submitting rank's original
// batch, and Candidates holds at most two expert nominations.
type Token struct {
	TokenID       string
	BatchPosition int
	Candidates    []Candidate
}

// ExpertSpec configures one expert: its ID and fixed per-round capacity.
type ExpertSpec struct {
	ExpertID string
	Capacity int
}

// WorkItem identifies one unit of work sent to an expert: the pair
// (expert, token) is the work-item identity used when reclaiming returns.
type WorkItem struct {
	ExpertID string
	TokenID  string
	// Weight is the token's nomination weight for this expert; it is carried in
	// the work package so workers do not need the original submissions.
	Weight int
}

// WorkPackage is everything an expert needs to process its share of the round.
type WorkPackage struct {
	ExpertID  string
	WorkItems []WorkItem
}

// ReturnPacket is one reclaimed work result. Identity is the (expert, token)
// pair; Vector must have the round's configured vector length.
type ReturnPacket struct {
	ExpertID string
	TokenID  string
	Vector   []float64
}

// TokenResult is the finalized result for one token, restored at its original
// batch position.
type TokenResult struct {
	TokenID       string
	BatchPosition int
	// Vector is the weight-merged result. It is all zeros for overflow tokens.
	Vector []float64
	// Overflow is true when none of the token's candidates were accepted.
	Overflow bool
	// AcceptedExperts lists the experts whose work items were actually accepted,
	// in expert-ID order. Empty for overflow tokens.
	AcceptedExperts []string
}

// RankResult holds the finalized tokens of one rank, indexed by the original
// batch positions.
type RankResult struct {
	RankID string
	// Tokens is indexed by BatchPosition; positions without a token are nil.
	Tokens []*TokenResult
}
