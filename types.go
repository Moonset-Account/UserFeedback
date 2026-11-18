// Package bootmeasure implements a measured-boot decision module for
// protected workloads.
//
// The module consumes measurement records produced by an attestation layer,
// compares them against the complete approved component sequences (normal
// boot and post-upgrade recovery boot) and emits an explicit status for every
// record. A record is only accepted when one single approved baseline matches
// as a whole: partial matches from different baselines can never be spliced
// together into an accept decision.
package bootmeasure

// Status is the final verdict produced by the decision module.
type Status string

const (
	// StatusAccepted means the measurement record matched exactly one
	// complete approved baseline at the claimed firmware generation.
	StatusAccepted Status = "accepted"

	// StatusMissingMeasurement means the measured stream followed an
	// approved baseline up to a point, but a required component was never
	// measured. Optional steps may be absent; required steps may not.
	StatusMissingMeasurement Status = "missing_measurement"

	// StatusOrderMismatch means a measured component appeared before a
	// required component that precedes it in the approved baseline.
	StatusOrderMismatch Status = "order_mismatch"

	// StatusDigestMismatch means a measured component's digest does not
	// equal the digest recorded in the approved baseline.
	StatusDigestMismatch Status = "digest_mismatch"

	// StatusNoBaselineMatch means no approved baseline can explain the
	// record, including attempts to splice partial matches of the normal
	// and recovery sequences (or of different firmware generations).
	StatusNoBaselineMatch Status = "no_baseline_match"

	// StatusRollbackBlocked means the node had already been confirmed at a
	// newer firmware generation and is presenting an older baseline. An
	// older sequence may remain approved for other nodes; rollback is
	// tracked per node, not globally.
	StatusRollbackBlocked Status = "rollback_blocked"

	// StatusGenerationMismatch means the measured sequence matches a
	// baseline whose firmware generation differs from the generation
	// claimed in the record.
	StatusGenerationMismatch Status = "generation_mismatch"

	// StatusAmbiguousBaseline means more than one baseline matches the
	// record completely, so no single baseline version can be attested.
	StatusAmbiguousBaseline Status = "ambiguous_baseline"

	// StatusInvalidInput means the record itself is malformed (empty node
	// identifier, no measurements, empty component name or digest).
	StatusInvalidInput Status = "invalid_input"

	// StatusInternalError means the node-state storage failed. No state is
	// committed and the caller must retry rather than trust the verdict.
	StatusInternalError Status = "internal_error"
)

// Step describes one component position inside an approved boot sequence.
type Step struct {
	// Component is the identifier of the measured component (for example a
	// bootloader or kernel stage).
	Component string

	// ExpectedDigest is the value the attestation layer must have measured
	// for this component at this position.
	ExpectedDigest []byte

	// Optional marks steps that a declaration is allowed to omit. Optional
	// steps that are present must still appear in baseline order and carry
	// the expected digest.
	Optional bool
}

// Baseline is one fully approved boot sequence at a firmware generation.
// Two sequence kinds are expected per generation: normal boot and recovery
// boot after an upgrade.
type Baseline struct {
	// Version uniquely identifies this baseline revision. Every usable
	// decision carries the version of the baseline it is grounded in.
	Version string

	// FirmwareGeneration is the generation this baseline belongs to.
	Generation uint64

	// SequenceType distinguishes the approved sequence kinds, e.g.
	// "normal" and "recovery".
	SequenceType string

	// Steps is the ordered, complete list of approved component steps.
	Steps []Step
}

// Measurement is one attested component record coming from the
// attestation layer.
type Measurement struct {
	// Component identifies the measured component.
	Component string

	// Digest is the value measured for the component on this boot.
	Digest []byte
}

// Record is the input evaluated by the decision module.
type Record struct {
	// NodeID identifies the node that produced the record. Rollback
	// protection is tracked per node.
	NodeID string

	// FirmwareGeneration is the generation the node claims to be running.
	FirmwareGeneration uint64

	// Measurements is the ordered measurement log from the attestation
	// layer.
	Measurements []Measurement
}

// Decision is the verdict for one record.
type Decision struct {
	// Status is the explicit outcome; see the Status constants.
	Status Status

	// BaselineVersion is the version of the baseline grounding a usable
	// conclusion. It is populated for accepted, generation-mismatch and
	// rollback-blocked decisions, where a unique baseline was identified,
	// and left empty otherwise.
	BaselineVersion string

	// Reason is a human-readable diagnostic. It never contains secrets and
	// is meant for operator logs and tests, not for security parsing.
	Reason string
}
