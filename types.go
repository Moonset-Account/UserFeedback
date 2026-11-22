// Package errfeedback implements an integer error-feedback (compensation)
// module for communication-efficient training.
//
// The model is deliberately small and deterministic:
//
//   - Every gradient and compensation value is an int64 associated with a
//     logical coordinate (Coord). Logical coordinates never depend on how
//     values are packed into communication buckets.
//   - A sender keeps a confirmed residual per logical coordinate. On Send it
//     adds that residual to the gradient, quantizes the result onto the
//     integer lattice prescribed for the step, selects a sparse subset and
//     keeps the difference (unsent values plus quantization error) as the
//     tentative new residual. The tentative residual is committed only when
//     the whole step is confirmed.
//   - A receiver accepts at most one message per (step, source, bucket) and
//     publishes the aggregate only after every expected group has been
//     accepted.
//   - A StepPlan may change its bucket layout between steps. Residuals are
//     keyed by logical coordinate, so a repartition migrates compensation to
//     the correct coordinate instead of reusing an old packet position.
//
// All state is guarded by a mutex, so Send/Confirm/Abort and
// Expect/Accept/Abort may be called concurrently. The module only uses the
// Go standard library.
package errfeedback

// SourceID identifies a logical gradient source.
type SourceID int

// StepID identifies one communication step. Step IDs are supplied by the
// caller and are never reused by that sender.
type StepID uint64

// BucketID identifies one communication bucket inside a StepPlan.
type BucketID int

// Coord is a logical coordinate of the gradient vector. It is stable across
// bucket layouts; this is what makes repartitioning safe.
type Coord int

// StepStatus is the settlement state of a step inside a Sender or Receiver.
type StepStatus int

const (
	// StepOpen means the step has pending packets and has not been settled.
	StepOpen StepStatus = iota
	// StepSettled means every bucket was confirmed and compensation was
	// committed exactly once.
	StepSettled
	// StepVoided means the step was abandoned; its residual was not updated
	// and late packets/confirmations are ignored.
	StepVoided
)

func (s StepStatus) String() string {
	switch s {
	case StepOpen:
		return "open"
	case StepSettled:
		return "settled"
	case StepVoided:
		return "voided"
	default:
		return "unknown"
	}
}
