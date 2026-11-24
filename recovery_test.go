package rebalancer

import (
	"errors"
	"testing"
)

// failoverProblem: two 2-mirror families, two consumers, cap 2 so both
// weight-1 hand-offs fit one in-flight batch. Target moves p1 and p3 from
// c0 to c1.
func failoverProblem() *Problem {
	return &Problem{
		Partitions: []Partition{
			partition(0, 1, "F", "dX", 0, makeSubs(2, 0)),
			partition(1, 1, "F", "dY", 0, makeSubs(2, 0, 1)),
			partition(2, 1, "G", "dX", 0, makeSubs(2, 0)),
			partition(3, 1, "G", "dY", 0, makeSubs(2, 0, 1)),
		},
		Consumers:   consumers([2]string{"dX", "10"}, [2]string{"dY", "10"}),
		InFlightCap: 2,
	}
}

func failoverTarget() Assignment { return Assignment{0, 1, 0, 1} }

// ---- 9. leader swap after a round's revokes: ambiguous old holder --------

func TestRecoverAfterLeaderSwap(t *testing.T) {
	p := failoverProblem()
	target := failoverTarget()
	plan, err := p.Schedule(target)
	if err != nil {
		t.Fatalf("Schedule: %v", err)
	}
	// Both weight-1 hand-offs fit the cap-2 batch: revoke together in
	// round 1, grant together in round 2.
	if len(plan.Rounds) != 2 {
		t.Fatalf("batched plan wants 2 rounds, got %d", len(plan.Rounds))
	}

	// Crash after round 1's revokes were sent but before the write was
	// acknowledged. Two durable-log views are possible.
	durableRound1 := []DurableRound{{Round: 1, Revocations: plan.Rounds[0].Revokes}}

	// Case A: no consumer reports the generation -> both old holders are
	// indistinguishable from the released state. The new leader treats both
	// partitions as still held, re-revokes, then grants: two extra rounds.
	rp, err := p.Recover(durableRound1, nil, target)
	if err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if len(rp.Ambiguous) != 2 {
		t.Fatalf("ambiguous = %v, want both p1 and p3", rp.Ambiguous)
	}
	if rp.StartPhases[1] != phaseStableSource || rp.StartPhases[3] != phaseStableSource {
		t.Fatalf("ambiguous partitions must be treated as still held: %v", rp.StartPhases)
	}
	if rp.StartPhases[0] != phaseStableTarget || rp.StartPhases[2] != phaseStableTarget {
		t.Fatalf("pinned partitions must start stable at their targets")
	}
	if rp.ExtraRounds != 2 {
		t.Fatalf("extra rounds = %d, want 2 (re-revoke then grant)", rp.ExtraRounds)
	}
	if res, rerr := p.ReplayFrom(rp.Plan, rp.StartPhases, 11); rerr != nil {
		t.Fatalf("recovery plan replay failed: %v", rerr)
	} else if !assignEqual(res.Final, target) {
		t.Fatalf("recovery reaches %v, want %v", res.Final, target)
	}

	// Case B: c0 later reports the high-water generation covering both
	// revokes -> both partitions are provably released; one grant round
	// remains.
	hiGen := int64(0)
	for _, g := range plan.Rounds[0].Revokes {
		if g.Generation > hiGen {
			hiGen = g.Generation
		}
	}
	rp2, err := p.Recover(durableRound1,
		[]GenerationReport{{Consumer: 0, Generation: hiGen}}, target)
	if err != nil {
		t.Fatalf("Recover(report): %v", err)
	}
	if len(rp2.Ambiguous) != 0 {
		t.Fatalf("reported revokes must not be ambiguous, got %v", rp2.Ambiguous)
	}
	if rp2.StartPhases[1] != phaseReleased || rp2.StartPhases[3] != phaseReleased {
		t.Fatalf("confirmed partitions must start released: %v", rp2.StartPhases)
	}
	if rp2.ExtraRounds != 1 {
		t.Fatalf("extra rounds = %d, want 1", rp2.ExtraRounds)
	}
	// The continuation starts from RELEASED partitions whose receipts are
	// already in durable history; ReplayFrom must accept a grant-only round.
	res2, rerr := p.ReplayFrom(rp2.Plan, rp2.StartPhases, 13)
	if rerr != nil {
		t.Fatalf("confirmed-release continuation replay failed: %v", rerr)
	}
	if !assignEqual(res2.Final, target) {
		t.Fatalf("confirmed recovery reaches %v, want %v", res2.Final, target)
	}
	if res2.Snapshots[0].InFlightWeight != 2 {
		t.Fatalf("continuation must start with both partitions in flight: %+v",
			res2.Snapshots[0])
	}

	// Case C: the durable log is empty: the round-1 write is lost entirely,
	// so the new leader has no knowledge of the revokes and again waits
	// safely at the source state.
	rp3, err := p.Recover(nil, nil, target)
	if err != nil {
		t.Fatalf("Recover(empty log): %v", err)
	}
	if rp3.StartPhases[1] != phaseStableSource || rp3.StartPhases[3] != phaseStableSource {
		t.Fatalf("empty-log recovery must keep old holders: %v", rp3.StartPhases)
	}
	if rp3.ExtraRounds != 2 {
		t.Fatalf("empty-log extra rounds = %d, want 2", rp3.ExtraRounds)
	}
}

// ---- 10. member leave: infeasible and feasible branches ------------------

func TestMemberLeaveBranches(t *testing.T) {
	p := failoverProblem()
	// Boundary state before any round executes: every partition is still
	// stable at its source c0, so the departing c1 holds nothing.
	phases := make([]byte, len(p.Partitions))
	holders := make([]int, len(p.Partitions))
	for i := range phases {
		holders[i] = p.Partitions[i].Origin
		if p.Partitions[i].Origin == failoverTarget()[i] {
			phases[i] = phaseStableTarget
		} else {
			phases[i] = phaseStableSource
		}
	}

	// Leaving c1 leaves only c0, which does not subscribe to p1/p3.
	out, err := p.EvaluateLeave(1, phases, holders, []int{0})
	if err != nil {
		t.Fatalf("EvaluateLeave: %v", err)
	}
	if out.Feasible {
		t.Fatalf("leave must be infeasible when no surviving subscriber exists")
	}

	// Feasible branch: one surviving consumer c0 must be able to take every
	// partition, so the variant uses distinct single-mirror families (the
	// domain constraint only binds mirrors within one family).
	p2 := failoverProblem()
	for i := range p2.Partitions {
		p2.Partitions[i].Family = []string{"F0", "F1", "F2", "F3"}[i]
		p2.Partitions[i].SubsOK = makeSubs(2, 0, 1)
	}
	phases2 := make([]byte, len(p2.Partitions))
	holders2 := make([]int, len(p2.Partitions))
	for i := range phases2 {
		phases2[i] = phaseStableSource
		holders2[i] = p2.Partitions[i].Origin
	}
	out2, err := p2.EvaluateLeave(1, phases2, holders2, []int{0})
	if err != nil {
		t.Fatalf("EvaluateLeave(feasible): %v", err)
	}
	if !out2.Feasible {
		t.Fatalf("leave branch should be feasible")
	}
	if len(out2.Target) != len(p2.Partitions) {
		t.Fatalf("target length mismatch")
	}
	for _, c := range out2.Target {
		if c != 0 {
			t.Fatalf("survivor view must reindex the only consumer as 0, got %d", c)
		}
	}
	// The survivor is already the original holder of every partition, so
	// the lexicographic optimizer keeps all of them and inherits the state
	// as-is: zero extra rounds is the correct optimal outcome.
	if out2.ExtraRounds != 0 || len(out2.Plan.Rounds) != 0 {
		t.Fatalf("inheritance wants 0 rounds, got %d (%+v)",
			out2.ExtraRounds, out2.Plan.Rounds)
	}

	// Force actual work after the leave: pin one partition onto a target the
	// survivor must now take over via a fresh revoke/grant hand-off.
	p3 := failoverProblem()
	for i := range p3.Partitions {
		p3.Partitions[i].Family = []string{"F0", "F1", "F2", "F3"}[i]
		p3.Partitions[i].SubsOK = makeSubs(2, 0, 1)
	}
	phases3 := make([]byte, len(p3.Partitions))
	holders3 := make([]int, len(p3.Partitions))
	for i := range phases3 {
		phases3[i] = phaseStableSource
		holders3[i] = p3.Partitions[i].Origin
	}
	// Simulate one hand-off already completed onto c1 before it leaves:
	// the orphaned partition must be re-assigned from RELEASED.
	phases3[1] = phaseStableTarget
	holders3[1] = 1
	out3, err := p3.EvaluateLeave(1, phases3, holders3, []int{0})
	if err != nil {
		t.Fatalf("EvaluateLeave(orphan): %v", err)
	}
	if !out3.Feasible {
		t.Fatalf("orphan re-assignment should be feasible")
	}
	if out3.ExtraRounds < 1 {
		t.Fatalf("an orphaned partition needs at least a grant round, got %d",
			out3.ExtraRounds)
	}
}

// ---- 11. member rejoin: stale-claim fence cost and branch comparison ------

func TestMemberRejoinAndWorstCase(t *testing.T) {
	p := failoverProblem()
	target := failoverTarget()
	plan, err := p.Schedule(target)
	if err != nil {
		t.Fatalf("Schedule: %v", err)
	}
	// State after round 1: p1 stable at c1, p3 still at c0.
	res, err := p.Replay(plan, 0)
	if err != nil {
		t.Fatalf("Replay: %v", err)
	}
	phases := res.Snapshots[1].Phase
	holders := res.Snapshots[1].Holder

	// c1 rejoins with a stale claim on p1: re-fencing adds one round.
	stale, err := p.EvaluateRejoin(1, phases, holders, []int{1})
	if err != nil {
		t.Fatalf("EvaluateRejoin: %v", err)
	}
	if !stale.Feasible || stale.StaleClaims != 1 {
		t.Fatalf("stale rejoin outcome wrong: %+v", stale)
	}
	if stale.ExtraRounds < 1 {
		t.Fatalf("stale claims must add a fence round")
	}
	// The hand-off already completed onto c1 must be inherited, not moved.
	if stale.Target[1] != 1 {
		t.Fatalf("completed hand-off must survive rejoin, target=%v", stale.Target)
	}

	// Same rejoin with no stale claim: no fence round.
	fresh, err := p.EvaluateRejoin(1, phases, holders, nil)
	if err != nil {
		t.Fatalf("EvaluateRejoin(fresh): %v", err)
	}
	if fresh.ExtraRounds >= stale.ExtraRounds {
		t.Fatalf("fresh rejoin (%d) must be cheaper than stale (%d)",
			fresh.ExtraRounds, stale.ExtraRounds)
	}

	// Leave branch at the same boundary, then compare worst cases.
	leave, err := p.EvaluateLeave(1, phases, holders, []int{0})
	if err != nil {
		t.Fatalf("EvaluateLeave: %v", err)
	}
	worst, err := WorstCaseExtraRounds([]*MembershipOutcome{fresh, stale, leave})
	if err != nil {
		t.Fatalf("WorstCaseExtraRounds: %v", err)
	}
	// The leave branch is infeasible (c0 cannot subscribe to p1/p3) and an
	// infeasible branch must surface as the worst case for explicit handling.
	if worst.Feasible || worst.Kind != "leave" {
		t.Fatalf("worst branch must be the infeasible leave, got %+v", worst)
	}

	// All-feasible comparison picks the numerically largest extra cost.
	worst2, err := WorstCaseExtraRounds([]*MembershipOutcome{fresh, stale})
	if err != nil {
		t.Fatalf("WorstCaseExtraRounds(feasible): %v", err)
	}
	if worst2.Kind != "rejoin" || worst2.ExtraRounds != stale.ExtraRounds {
		t.Fatalf("worst feasible branch = %+v, want stale rejoin", worst2)
	}
}

// ---- 12. recovery rejects unknown consumer reports -----------------------

func TestRecoverValidation(t *testing.T) {
	p := failoverProblem()
	_, err := p.Recover(nil,
		[]GenerationReport{{Consumer: 9, Generation: 1}}, failoverTarget())
	if !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("want ErrInvalidInput, got %v", err)
	}
}
