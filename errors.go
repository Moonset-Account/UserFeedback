package errfeedback

import (
	"errors"
	"fmt"
)

// Sentinel errors. Confirmation and delivery are network-facing operations, so
// stale or duplicate traffic is reported as a non-fatal outcome where possible
// (see ConfirmOutcome and ErrDuplicateMessage) and as a sentinel only when the
// caller needs to notice a programming or protocol error.
var (
	// ErrInvalidSpec is returned when a StepSpec or StepPlan is malformed.
	ErrInvalidSpec = errors.New("errfeedback: invalid step spec")
	// ErrInvalidPlan is returned when a StepPlan is malformed.
	ErrInvalidPlan = errors.New("errfeedback: invalid plan")
	// ErrStepInProgress is returned when Begin is called while another step is
	// still open on the same sender.
	ErrStepInProgress = errors.New("errfeedback: step already in progress")
	// ErrStepSettled is returned when an operation targets an already settled step.
	ErrStepSettled = errors.New("errfeedback: step already settled")
	// ErrStepVoided is returned when an operation targets an abandoned step.
	ErrStepVoided = errors.New("errfeedback: step voided")
	// ErrUnknownStep is returned when a step was never registered.
	ErrUnknownStep = errors.New("errfeedback: unknown step")
	// ErrUnknownGroup is returned when a bucket is not part of the step plan.
	ErrUnknownGroup = errors.New("errfeedback: unknown bucket group")
	// ErrUnknownSource is returned when a source is not expected by a receiver.
	ErrUnknownSource = errors.New("errfeedback: unknown source")
	// ErrWrongSource is returned when a confirmation targets a foreign source.
	ErrWrongSource = errors.New("errfeedback: confirmation for foreign source")
	// ErrStaleAttempt is returned when a packet or spec has an outdated attempt.
	ErrStaleAttempt = errors.New("errfeedback: stale attempt")
	// ErrStaleLayout is returned when a packet was built for a retired layout.
	ErrStaleLayout = errors.New("errfeedback: stale bucket layout")
	// ErrDuplicateMessage is returned when a group already accepted a message.
	// The duplicate is dropped and never aggregated.
	ErrDuplicateMessage = errors.New("errfeedback: duplicate message for group")
	// ErrIntegrity is returned when a packet digest does not match its content.
	ErrIntegrity = errors.New("errfeedback: packet integrity check failed")
	// ErrJournalInconsistent is returned when journal records cannot be
	// replayed (tampered, reordered or built from a different chain).
	ErrJournalInconsistent = errors.New("errfeedback: inconsistent journal")
	// ErrEmptySources is returned when a receiver expectation has no sources.
	ErrEmptySources = errors.New("errfeedback: no sources")
)

// ConfirmOutcome describes the effect of one confirmation on the sender.
type ConfirmOutcome int

const (
	// ConfirmRecorded means the bucket was newly recorded but the step is not
	// yet complete. The residual is not touched.
	ConfirmRecorded ConfirmOutcome = iota
	// ConfirmSettled means this confirmation completed the step and committed
	// the new residual exactly once.
	ConfirmSettled
	// ConfirmDuplicate means the bucket was already confirmed, or the whole
	// step was already settled. Nothing changed.
	ConfirmDuplicate
	// ConfirmIgnored means the confirmation was dropped as stale: an unknown
	// step/attempt, a retired layout, or a voided step. Nothing changed.
	ConfirmIgnored
)

func (o ConfirmOutcome) String() string {
	switch o {
	case ConfirmRecorded:
		return "recorded"
	case ConfirmSettled:
		return "settled"
	case ConfirmDuplicate:
		return "duplicate"
	case ConfirmIgnored:
		return "ignored"
	default:
		return fmt.Sprintf("ConfirmOutcome(%d)", int(o))
	}
}
