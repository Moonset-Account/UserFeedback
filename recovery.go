package rebalancer

import "fmt"

// Recovery model
// --------------
// The coordinator durably persists each round before/writing its messages.
// A leader can crash after sending a round's revokes but before the write is
// acknowledged. The new leader therefore only trusts:
//
//   - durableRounds: the rounds whose writes are known to be durable;
//   - reports:       the highest generation each surviving consumer later
//                    reports observing.
//
// For a partition whose revoke is not confirmed by any report the old holder
// and the revoked state are indistinguishable: the holder may still serve it.
// The new leader keeps such a partition at its old holder (phase 0) and simply
// revokes again under a fresh generation. It never grants a partition while a
// possible old holder exists, which is the condition that prevents double
// consumption (see fence.go for the executable counter-example).

// GenerationReport is a consumer's later report of the highest generation it
// has observed (a release receipt it durably acknowledged).
type GenerationReport struct {
	Consumer   int
	Generation int64
}

// DurableRound is one coordinator round present in the durable log.
type DurableRound struct {
	Round       int
	Revocations []Grant
}

// RecoveryPlan is the executable policy a new leader runs.
type RecoveryPlan struct {
	StartPhases []byte
	// Ambiguous lists partitions whose revoke is not confirmed by any report.
	// They are conservatively treated as still held by the old holder.
	Ambiguous   []int
	Plan        *Plan
	ExtraRounds int
}

// Recover reconstructs a safe phase vector from the durable log plus consumer
// generation reports and schedules the minimum-round continuation to target.
func (p *Problem) Recover(durable []DurableRound,
	reports []GenerationReport, target Assignment) (*RecoveryPlan, error) {

	n := len(p.Partitions)
	if err := p.Validate(); err != nil {
		return nil, err
	}
	observed := make(map[int]int64, len(reports)) // consumer -> highest generation
	for _, r := range reports {
		if r.Consumer < 0 || r.Consumer >= len(p.Consumers) {
			return nil, fmt.Errorf("%w: report from unknown consumer %d",
				ErrInvalidInput, r.Consumer)
		}
		if g, ok := observed[r.Consumer]; !ok || r.Generation > g {
			observed[r.Consumer] = r.Generation
		}
	}

	// Revocation generation per partition, taken from the durable log.
	revGen := make(map[int]int64)
	revHolder := make(map[int]int)
	for _, dr := range durable {
		for _, g := range dr.Revocations {
			revGen[g.Partition] = g.Generation
			revHolder[g.Partition] = g.Consumer
		}
	}

	phases := make([]byte, n)
	var ambiguous []int
	for i := range p.Partitions {
		origin := p.Partitions[i].Origin
		switch {
		case origin == -1:
			// Never had an owner; granting it is always safe.
			phases[i] = phaseReleased
		case origin == target[i]:
			phases[i] = phaseStableTarget
		default:
			gen, wasRevoked := revGen[i]
			// Generations are per hand-off: a report confirms a partition's
			// revoke only when the reported high-water mark reaches the exact
			// generation carried by that partition's revoke.
			confirmed := wasRevoked && observed[revHolder[i]] >= gen
			switch {
			case wasRevoked && confirmed:
				// The old holder durably observed the revoke and reported its
				// generation; the partition is safely released.
				phases[i] = phaseReleased
			case wasRevoked && !confirmed:
				// Indistinguishable: the write may be lost and the old holder
				// may still serve. Treat as still held; re-revoke later.
				phases[i] = phaseStableSource
				ambiguous = append(ambiguous, i)
			default:
				// Never present in the durable log: definitely still held.
				phases[i] = phaseStableSource
			}
		}
	}

	plan, err := p.ScheduleFrom(target, phases)
	if err != nil {
		return nil, err
	}
	return &RecoveryPlan{
		StartPhases: phases,
		Ambiguous:   ambiguous,
		Plan:        plan,
		ExtraRounds: len(plan.Rounds),
	}, nil
}

// MembershipOutcome describes one allowed membership-change branch.
type MembershipOutcome struct {
	Kind        string // "leave" or "rejoin"
	Member      int
	Feasible    bool
	Target      Assignment
	Plan        *Plan
	ExtraRounds int
	// StaleClaims (rejoin only) counts partitions the returning member still
	// claims although it had already released them; each must be re-fenced.
	StaleClaims int
}

// EvaluateLeave handles a consumer leaving at a round boundary. Completed
// hand-offs are inherited as-is (pinned). Partitions the departed member was
// about to receive become RELEASED and are re-assigned; partitions it still
// holds at the boundary are revoked once before re-assignment.
//
// currentHolder gives the holder of every phase-0/phase-2 partition in the
// pre-leave view (phase-1 partitions carry -1). remaining maps old consumer
// indices to surviving old indices; the surviving problem view uses the same
// partition indexing.
func (p *Problem) EvaluateLeave(leaving int, phases []byte,
	currentHolder []int, remaining []int) (*MembershipOutcome, error) {

	n := len(p.Partitions)
	if leaving < 0 || leaving >= len(p.Consumers) {
		return nil, fmt.Errorf("%w: leaving consumer %d out of range",
			ErrInvalidInput, leaving)
	}
	if len(phases) != n || len(currentHolder) != n {
		return nil, fmt.Errorf("%w: phase/holder vector length mismatch",
			ErrInvalidInput)
	}
	sub, err := p.subProblem(remaining)
	if err != nil {
		return nil, err
	}

	fixed := map[int]int{}
	start := make([]byte, n)
	for i := range p.Partitions {
		holder := currentHolder[i]
		switch phases[i] {
		case phaseStableTarget:
			if holder == leaving {
				// Hand-off completed onto the member that has now left: the
				// partition is orphaned and must be re-assigned from released.
				start[i] = phaseReleased
				continue
			}
			c := remainingIndex(remaining, holder)
			if c < 0 {
				return nil, fmt.Errorf("%w: surviving holder mapping missing for %d",
					ErrInvalidInput, holder)
			}
			fixed[i] = c
			start[i] = phaseStableTarget
		case phaseReleased:
			start[i] = phaseReleased
		default: // still at its source
			if holder == leaving {
				// The departed member still holds it: one revoke is required.
				// Model it as an item held by no surviving consumer; the
				// sub-problem gives it origin -1 so it starts released.
				start[i] = phaseReleased
			} else {
				start[i] = phaseStableSource
			}
		}
	}

	// Rebuild origins in the sub-problem to match the start phases.
	for i := range sub.Partitions {
		switch start[i] {
		case phaseReleased:
			sub.Partitions[i].Origin = -1
		case phaseStableTarget:
			sub.Partitions[i].Origin = fixed[i]
		default:
			sub.Partitions[i].Origin = remainingIndex(remaining, currentHolder[i])
		}
	}

	t, err := sub.SolveFixed(fixed)
	if err != nil {
		return &MembershipOutcome{Kind: "leave", Member: leaving, Feasible: false}, nil
	}
	// Normalize no-op partitions explicitly: a source-stable partition whose
	// surviving holder is already the target has nothing to revoke and is
	// equivalent to a target-stable one.
	for i := range start {
		if start[i] == phaseStableSource && sub.Partitions[i].Origin == t.Assign[i] {
			start[i] = phaseStableTarget
		}
	}
	plan, err := sub.scheduleOpts(t.Assign, start, false)
	if err != nil {
		return &MembershipOutcome{Kind: "leave", Member: leaving, Feasible: false}, nil
	}
	return &MembershipOutcome{
		Kind:        "leave",
		Member:      leaving,
		Feasible:    true,
		Target:      t.Assign,
		Plan:        plan,
		ExtraRounds: len(plan.Rounds),
	}, nil
}

func remainingIndex(remaining []int, oldIdx int) int {
	for newIdx, old := range remaining {
		if old == oldIdx {
			return newIdx
		}
	}
	return -1
}

// subProblem builds the problem view after a member leaves, reindexing
// consumers and subscription sets. Partition weights/families/domains and
// origins are remapped by the caller through EvaluateLeave.
func (p *Problem) subProblem(remaining []int) (*Problem, error) {
	nc := len(remaining)
	if nc == 0 {
		return nil, fmt.Errorf("%w: no surviving consumers", ErrInvalidInput)
	}
	for _, idx := range remaining {
		if idx < 0 || idx >= len(p.Consumers) {
			return nil, fmt.Errorf("%w: remaining index %d out of range",
				ErrInvalidInput, idx)
		}
	}
	ps := make([]Partition, len(p.Partitions))
	for i := range p.Partitions {
		pt := p.Partitions[i]
		subs := make([]bool, nc)
		for newC, oldC := range remaining {
			subs[newC] = pt.SubsOK[oldC]
		}
		ps[i] = Partition{
			ID:     pt.ID,
			Weight: pt.Weight,
			Family: pt.Family,
			Domain: pt.Domain,
			Origin: pt.Origin,
			SubsOK: subs,
		}
	}
	cs := make([]Consumer, nc)
	for newC, oldC := range remaining {
		cs[newC] = Consumer{
			ID:       newC,
			Capacity: p.Consumers[oldC].Capacity,
			Domain:   p.Consumers[oldC].Domain,
		}
	}
	return &Problem{Partitions: ps, Consumers: cs, InFlightCap: p.InFlightCap}, nil
}

// EvaluateRejoin handles a member returning at a round boundary. Hand-offs
// already completed before the rejoin are inherited as-is through fixed
// holders; the returned member only participates in the remaining decisions.
// Partitions the member still claims but had already released must be
// re-fenced, which costs one extra round covering all current stale claims.
func (p *Problem) EvaluateRejoin(returning int, phases []byte,
	currentHolder []int, staleClaims []int) (*MembershipOutcome, error) {

	n := len(p.Partitions)
	if returning < 0 || returning >= len(p.Consumers) {
		return nil, fmt.Errorf("%w: returning consumer %d out of range",
			ErrInvalidInput, returning)
	}
	if len(phases) != n || len(currentHolder) != n {
		return nil, fmt.Errorf("%w: phase/holder vector length mismatch",
			ErrInvalidInput)
	}
	staleSet := map[int]int{}
	for _, id := range staleClaims {
		if id < 0 || id >= n {
			return nil, fmt.Errorf("%w: stale claim %d out of range",
				ErrInvalidInput, id)
		}
		staleSet[id] = returning
	}

	// Re-fencing: the coordinator cannot safely grant anything a stale member
	// may still serve until that member observes a newer generation. One
	// extra round covers all currently stale claims together.
	fenceRounds := 0
	if len(staleSet) > 0 {
		fenceRounds = 1
	}

	// Inherit every hand-off that reached STABLE-AT-TARGET before the rejoin.
	fixed := map[int]int{}
	for i, ph := range phases {
		if ph == phaseStableTarget {
			if currentHolder[i] < 0 || currentHolder[i] >= len(p.Consumers) {
				return nil, fmt.Errorf("%w: stable partition %d has invalid holder %d",
					ErrInvalidInput, i, currentHolder[i])
			}
			fixed[i] = currentHolder[i]
		}
	}
	t, err := p.SolveFixed(fixed)
	if err != nil {
		return &MembershipOutcome{
			Kind: "rejoin", Member: returning, Feasible: false,
			StaleClaims: len(staleSet),
		}, nil
	}
	plan, err := p.ScheduleFrom(t.Assign, phases)
	if err != nil {
		return &MembershipOutcome{
			Kind: "rejoin", Member: returning, Feasible: false,
			StaleClaims: len(staleSet),
		}, nil
	}
	return &MembershipOutcome{
		Kind:        "rejoin",
		Member:      returning,
		Feasible:    true,
		Target:      t.Assign,
		Plan:        plan,
		ExtraRounds: len(plan.Rounds) + fenceRounds,
		StaleClaims: len(staleSet),
	}, nil
}

// WorstCaseExtraRounds compares every allowed membership-change branch and
// returns the branch with the largest worst-case extra-round cost.
// Already-completed hand-offs are inherited through the phase vectors.
func WorstCaseExtraRounds(branches []*MembershipOutcome) (*MembershipOutcome, error) {
	if len(branches) == 0 {
		return nil, fmt.Errorf("%w: no membership branches to compare", ErrInvalidInput)
	}
	var worst *MembershipOutcome
	for _, b := range branches {
		if !b.Feasible {
			// An infeasible branch has no finite completion: surface it as the
			// worst case so callers must handle it explicitly.
			return b, nil
		}
		if worst == nil || b.ExtraRounds > worst.ExtraRounds ||
			(b.ExtraRounds == worst.ExtraRounds && b.Kind < worst.Kind) {
			worst = b
		}
	}
	return worst, nil
}
