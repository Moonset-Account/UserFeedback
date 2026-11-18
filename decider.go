package bootmeasure

import (
	"crypto/subtle"
	"fmt"
)

// evaluation ranks how close a single baseline came to a full match. A
// smaller rank is a stronger match; depth is the number of baseline steps
// consumed before failure and is used to pick the most informative verdict
// when every baseline fails.
const (
	rankOrder   = 0
	rankMissing = 1
	rankDigest  = 2
	rankUnknown = 3
)

type evaluation struct {
	matched bool
	rank    int
	depth   int
	reason  string
}

// Decider evaluates measurement records against the approved component
// sequences and enforces per-node firmware-generation rollback protection.
type Decider struct {
	baselines []Baseline
	store     NodeStateStore
}

// NewDecider builds a Decider. The baselines are copied by value and must
// each carry a unique non-empty version, a non-empty sequence type and at
// least one step. A nil store is rejected because rollback protection is a
// mandatory part of every decision.
func NewDecider(baselines []Baseline, store NodeStateStore) (*Decider, error) {
	if store == nil {
		return nil, fmt.Errorf("bootmeasure: node state store is required")
	}
	if len(baselines) == 0 {
		return nil, fmt.Errorf("bootmeasure: at least one approved baseline is required")
	}
	versions := make(map[string]struct{}, len(baselines))
	for i, b := range baselines {
		if b.Version == "" {
			return nil, fmt.Errorf("bootmeasure: baseline %d has no version", i)
		}
		if _, dup := versions[b.Version]; dup {
			return nil, fmt.Errorf("bootmeasure: duplicate baseline version %q", b.Version)
		}
		versions[b.Version] = struct{}{}
		if b.SequenceType == "" {
			return nil, fmt.Errorf("bootmeasure: baseline %q has no sequence type", b.Version)
		}
		if len(b.Steps) == 0 {
			return nil, fmt.Errorf("bootmeasure: baseline %q has no steps", b.Version)
		}
		for j, step := range b.Steps {
			if step.Component == "" {
				return nil, fmt.Errorf("bootmeasure: baseline %q step %d has no component", b.Version, j)
			}
			if len(step.ExpectedDigest) == 0 {
				return nil, fmt.Errorf("bootmeasure: baseline %q step %d has no expected digest", b.Version, j)
			}
		}
	}
	copied := make([]Baseline, len(baselines))
	copy(copied, baselines)
	return &Decider{baselines: copied, store: store}, nil
}

// Evaluate applies the full decision pipeline to one attestation record:
//
//  1. validate the record,
//  2. try to match the ordered measurements against every approved baseline
//     as a whole (optional steps may be omitted; partial matches from
//     different baselines can never be combined),
//  3. verify the claimed firmware generation against the matched baseline,
//  4. enforce per-node rollback protection using the confirmed-generation
//     store,
//  5. on success, confirm the node at the baseline generation.
func (d *Decider) Evaluate(rec Record) Decision {
	if err := validateRecord(rec); err != nil {
		return Decision{Status: StatusInvalidInput, Reason: err.Error()}
	}

	var matches []Baseline
	best := evaluation{rank: rankUnknown, reason: "no baseline was evaluated"}
	for _, b := range d.baselines {
		ev := matchBaseline(b, rec.Measurements)
		if ev.matched {
			matches = append(matches, b)
			continue
		}
		if ev.depth > best.depth || (ev.depth == best.depth && ev.rank < best.rank) {
			best = ev
		}
	}

	switch len(matches) {
	case 0:
		return Decision{Status: failureStatus(best), Reason: best.reason}
	case 1:
		// continue to generation and rollback checks
	default:
		return Decision{
			Status: StatusAmbiguousBaseline,
			Reason: fmt.Sprintf("record matches %d complete baselines; a single baseline version is required", len(matches)),
		}
	}

	baseline := matches[0]
	decision := Decision{BaselineVersion: baseline.Version}

	if baseline.Generation != rec.FirmwareGeneration {
		decision.Status = StatusGenerationMismatch
		decision.Reason = fmt.Sprintf(
			"record claims generation %d but matched baseline %q belongs to generation %d",
			rec.FirmwareGeneration, baseline.Version, baseline.Generation)
		return decision
	}

	highest, err := d.store.HighestGeneration(rec.NodeID)
	if err != nil {
		return Decision{
			Status: StatusInternalError,
			Reason: fmt.Sprintf("cannot read confirmed generation for node %q: %v", rec.NodeID, err),
		}
	}
	if highest > baseline.Generation {
		decision.Status = StatusRollbackBlocked
		decision.Reason = fmt.Sprintf(
			"node %q was already confirmed at generation %d; baseline %q at generation %d is a rollback",
			rec.NodeID, highest, baseline.Version, baseline.Generation)
		return decision
	}

	if err := d.store.CommitGeneration(rec.NodeID, baseline.Generation); err != nil {
		return Decision{
			Status: StatusInternalError,
			Reason: fmt.Sprintf("cannot confirm generation for node %q: %v", rec.NodeID, err),
		}
	}

	decision.Status = StatusAccepted
	decision.Reason = fmt.Sprintf("record matches baseline %q (%s, generation %d) in full",
		baseline.Version, baseline.SequenceType, baseline.Generation)
	return decision
}

// validateRecord rejects records that cannot represent a real measurement
// stream.
func validateRecord(rec Record) error {
	if rec.NodeID == "" {
		return fmt.Errorf("record has no node identifier")
	}
	if len(rec.Measurements) == 0 {
		return fmt.Errorf("record for node %q contains no measurements", rec.NodeID)
	}
	for i, m := range rec.Measurements {
		if m.Component == "" {
			return fmt.Errorf("measurement %d has no component", i)
		}
		if len(m.Digest) == 0 {
			return fmt.Errorf("measurement %d (%s) has an empty digest", i, m.Component)
		}
	}
	return nil
}

// matchBaseline walks one baseline and the ordered measurements in lock
// step. Every measurement must consume a baseline step; optional baseline
// steps may be skipped, but skipping a required step to reach a later
// component is an order mismatch. A component that does not occur anywhere
// in the baseline is reported as unknown rather than as an order violation.
func matchBaseline(b Baseline, measurements []Measurement) evaluation {
	known := make(map[string]struct{}, len(b.Steps))
	for _, step := range b.Steps {
		known[step.Component] = struct{}{}
	}

	stepIdx := 0
	consumed := 0

	for ; consumed < len(measurements); consumed++ {
		m := measurements[consumed]

		if _, ok := known[m.Component]; !ok {
			return evaluation{
				rank:   rankUnknown,
				depth:  stepIdx,
				reason: fmt.Sprintf("component %q is not part of baseline %q", m.Component, b.Version),
			}
		}

		// Skip optional steps that the declaration chose not to take.
		for stepIdx < len(b.Steps) &&
			b.Steps[stepIdx].Optional &&
			b.Steps[stepIdx].Component != m.Component {
			stepIdx++
		}

		switch {
		case stepIdx >= len(b.Steps):
			// The component exists earlier in the baseline but has no
			// remaining position: it was declared too late (or twice).
			return evaluation{
				rank:   rankOrder,
				depth:  stepIdx,
				reason: fmt.Sprintf("component %q has no remaining position in baseline %q", m.Component, b.Version),
			}
		case b.Steps[stepIdx].Component != m.Component:
			return evaluation{
				rank:  rankOrder,
				depth: stepIdx,
				reason: fmt.Sprintf("expected component %q at position %d of baseline %q, measured %q",
					b.Steps[stepIdx].Component, stepIdx, b.Version, m.Component),
			}
		case !digestsEqual(b.Steps[stepIdx].ExpectedDigest, m.Digest):
			return evaluation{
				rank:   rankDigest,
				depth:  stepIdx,
				reason: fmt.Sprintf("digest mismatch for component %q in baseline %q", m.Component, b.Version),
			}
		}
		stepIdx++
	}

	// Optional tail steps are allowed to remain unmeasured; required tail
	// steps mean the measurement stream is incomplete.
	for stepIdx < len(b.Steps) {
		if !b.Steps[stepIdx].Optional {
			return evaluation{
				rank:  rankMissing,
				depth: stepIdx,
				reason: fmt.Sprintf("required component %q at position %d of baseline %q was never measured",
					b.Steps[stepIdx].Component, stepIdx, b.Version),
			}
		}
		stepIdx++
	}

	return evaluation{matched: true, depth: len(b.Steps)}
}

func failureStatus(ev evaluation) Status {
	switch ev.rank {
	case rankMissing:
		return StatusMissingMeasurement
	case rankOrder:
		return StatusOrderMismatch
	case rankDigest:
		return StatusDigestMismatch
	default:
		return StatusNoBaselineMatch
	}
}

// digestsEqual compares digests without leaking length/prefix information
// through timing.
func digestsEqual(expected, actual []byte) bool {
	if len(expected) != len(actual) {
		return false
	}
	return subtle.ConstantTimeCompare(expected, actual) == 1
}
