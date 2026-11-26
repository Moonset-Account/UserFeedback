package barrier

import (
	"errors"
	"fmt"
)

// Stable sentinel errors allow callers to use errors.Is.
var (
	ErrInvalidConfig       = errors.New("barrier: invalid configuration")
	ErrInvalidRequest      = errors.New("barrier: invalid request")
	ErrConflict            = errors.New("barrier: conflicting state")
	ErrRunNotFound         = errors.New("barrier: run not found")
	ErrBarrierNotFound     = errors.New("barrier: barrier not found")
	ErrParticipantNotFound = errors.New("barrier: participant not found")
	ErrGroupNotFound       = errors.New("barrier: group not found")
	ErrGrantNotFound       = errors.New("barrier: proxy authorization not found")
	ErrRelationNotFound    = errors.New("barrier: relation not found")
	ErrReceiptNotFound     = errors.New("barrier: receipt not found")

	ErrParticipantExists = errors.New("barrier: participant already exists")
	ErrGroupExists       = errors.New("barrier: group already exists")
	ErrRunExists         = errors.New("barrier: run already exists")
	ErrBarrierExists     = errors.New("barrier: barrier already exists")

	ErrUnauthorized        = errors.New("barrier: submitter is not authorized for proof")
	ErrRevoked             = errors.New("barrier: authorization has been revoked")
	ErrParticipantInactive = errors.New("barrier: participant is not active")
	ErrCheckpointMismatch  = errors.New("barrier: receipt does not match declared checkpoint")
	ErrGroupNotRequired    = errors.New("barrier: group is not part of this barrier")
	ErrNotMember           = errors.New("barrier: subject is not a current member of group")
	ErrInvalidProof        = errors.New("barrier: proof material is invalid")
	ErrBarrierReleased     = errors.New("barrier: barrier has already been released")
	ErrBarrierNotReady     = errors.New("barrier: barrier does not have enough valid proofs")
)

// NotReadyError carries the current, recomputed evaluation when an explicit
// release attempt loses the race with a handoff, revocation, or relation
// change.
type NotReadyError struct {
	Evaluation *Evaluation
}

func (e *NotReadyError) Error() string {
	if e == nil || e.Evaluation == nil {
		return ErrBarrierNotReady.Error()
	}
	return fmt.Sprintf("%s: missing %d proof group(s)", ErrBarrierNotReady, len(e.Evaluation.Missing))
}

func (e *NotReadyError) Unwrap() error { return ErrBarrierNotReady }
