// Package reinject implements the dead-letter re-injection module.
//
// A bill message that lands in the dead-letter queue blocks consumer groups
// that are still positioned before it, while other groups may already have
// taken an irrevocable decision to skip past it. When a repaired version of
// the message becomes available, this module decides, per consumer group:
//
//   - a group that has not crossed the message position resumes in place by
//     re-injecting the repaired message at its original position;
//   - a group that has irrevocably skipped the position sends the repair to
//     the pending-review source, while the progress it already acknowledged
//     is retained (the module only reads progress, it never rewinds it);
//   - messages belonging to other customers keep flowing through the normal
//     delivery path and are not touched by the repair.
//
// A re-injection request is identified by a business repair ID. Retrying the
// same repair returns the previously taken decision without repeating any
// side effect.
package reinject

import (
	"context"
	"errors"
)

// MessageID is the unique identifier of a queued bill message.
type MessageID string

// Group is the name of a consumer group.
type Group string

// DeadLetter describes the poisoned message and its original queue position.
type DeadLetter struct {
	// MessageID identifies the poisoned bill message.
	MessageID MessageID
	// Key is the partition/shard key (for example the customer key) the
	// in-key sequence number is scoped to.
	Key string
	// SeqInKey is the zero-based ordinal of the message inside its key.
	SeqInKey uint64
	// CustomerID owns the message; repairs must never move messages across
	// customers.
	CustomerID string
	// OriginalPayload is the payload that failed processing.
	OriginalPayload []byte
}

// Repair carries the corrected content for a dead-lettered message.
type Repair struct {
	// RepairID identifies one business repair. Retries carrying the same
	// RepairID are the same business decision and must be idempotent.
	RepairID string
	// MessageID must match the dead letter the repair is meant for.
	MessageID MessageID
	// CustomerID must match the dead letter's customer.
	CustomerID string
	// RepairedPayload replaces the original payload when the message is
	// resumed in place or forwarded for manual review.
	RepairedPayload []byte
}

// ActionKind is the decision taken for one consumer group.
type ActionKind int

const (
	// ResumeInPlace re-injects the repaired message at its original
	// in-key position for a group that has not crossed that position.
	ResumeInPlace ActionKind = iota + 1
	// RouteToReview sends the repaired content to the pending-review
	// source on behalf of a group that already skipped the position
	// irrevocably; acknowledged progress is left untouched.
	RouteToReview
)

// String returns a stable, human-readable action name.
func (a ActionKind) String() string {
	switch a {
	case ResumeInPlace:
		return "resume_in_place"
	case RouteToReview:
		return "route_to_review"
	default:
		return "unknown"
	}
}

// GroupAction is the per-group decision together with the data the engine
// handed to the corresponding side effect.
type GroupAction struct {
	Group      Group
	Action     ActionKind
	MessageID  MessageID
	Key        string
	SeqInKey   uint64
	CustomerID string
	Payload    []byte
}

// Decision is the full, durable result of processing one repair request.
type Decision struct {
	RepairID   string
	DeadLetter DeadLetter
	Actions    []GroupAction
}

// ActionFor returns the action decided for g, or false when the group was not
// part of the repair request.
func (d *Decision) ActionFor(g Group) (GroupAction, bool) {
	for _, a := range d.Actions {
		if a.Group == g {
			return a, true
		}
	}
	return GroupAction{}, false
}

// Validation errors returned before any side effect is attempted.
var (
	ErrEmptyRepairID    = errors.New("reinject: repair id must not be empty")
	ErrMessageMismatch  = errors.New("reinject: repair message id does not match the dead letter")
	ErrCustomerMismatch = errors.New("reinject: repair customer does not match the dead letter")
	ErrEmptyDeadLetter  = errors.New("reinject: dead letter message id must not be empty")
	ErrNilProgress      = errors.New("reinject: progress reader is required")
	ErrNilBroker        = errors.New("reinject: broker is required")
	ErrNilReview        = errors.New("reinject: review source is required")
)

// AckProgress is a read model of one consumer group's consumption position.
type AckProgress struct {
	Group Group
	// Key the progress applies to.
	Key string
	// CommittedSeq is the highest in-key sequence number that has been
	// acknowledged. Set HasCommitted to false when the group has never
	// acknowledged anything on the key, so that sequence zero is not
	// mistaken for progress.
	CommittedSeq uint64
	// HasCommitted reports whether CommittedSeq is meaningful.
	HasCommitted bool
	// SkippedSeq marks an in-key position the group has irrevocably
	// decided to skip (for example a dead-letter acknowledgement with a
	// skip policy). Once recorded, the group cannot be resumed in place
	// before that position.
	SkippedSeq map[uint64]bool
}

// HasIrrevocableSkip reports whether the group has skipped the given in-key
// position and therefore cannot resume the message there.
func (p AckProgress) HasIrrevocableSkip(seq uint64) bool {
	return p.SkippedSeq[seq]
}

// BeforePosition reports whether the group is still strictly before the given
// position, i.e. it has not acknowledged the position or anything beyond it.
func (p AckProgress) BeforePosition(seq uint64) bool {
	if !p.HasCommitted {
		return true
	}
	return p.CommittedSeq < seq
}

// ProgressReader looks up the acknowledged progress of a consumer group on a
// key. Implementations must be safe for concurrent use.
type ProgressReader interface {
	Progress(ctx context.Context, group Group, key string) (AckProgress, error)
}

// Broker is the queue side effect port. Implementations must be safe for
// concurrent use.
type Broker interface {
	// ReinstateAt re-injects payload for the given group at the original
	// in-key position of the dead letter so the group resumes from there.
	ReinstateAt(ctx context.Context, group Group, id MessageID, key string, seq uint64, payload []byte) error
	// Deliver sends a normal (non-dead-letter) message through the regular
	// delivery path. The repair module uses it to prove other customers'
	// messages continue to be delivered while a repair is in flight.
	Deliver(ctx context.Context, id MessageID, key string, seq uint64, customerID string, payload []byte) error
}

// ReviewSource receives repaired content that can no longer be resumed in
// place because the group already skipped the original position. The content
// is queued for manual/secondary review. Implementations must be safe for
// concurrent use.
type ReviewSource interface {
	SendForReview(ctx context.Context, group Group, id MessageID, key string, seq uint64, customerID string, repairedPayload []byte) error
}
