package rebalancer

import (
	"fmt"
	"math/big"
	"math/rand"
	"sort"
)

// RoundSnapshot is a replayable, independently recomputed view of the system
// at a round boundary. Index 0 is the state before round 1 executes.
type RoundSnapshot struct {
	Round int
	// Phase per partition (phaseStableSource / phaseReleased / phaseStableTarget).
	Phase []byte
	// Holder per partition: consumer index, or -1 while released.
	Holder []int
	// ConsumableCopies counts consumable (phase 0/2) mirrors per family.
	ConsumableCopies map[string]int
	// InFlightWeight is the total weight currently in hand-off.
	InFlightWeight int
	// Loads recomputed from the current holder map (exact-fraction inputs).
	Loads []int
}

// ReplayResult bundles every round-boundary snapshot and the final load.
type ReplayResult struct {
	Snapshots    []RoundSnapshot
	Final        Assignment
	FinalMaxLoad *big.Rat
}

// Replay executes the action list as a message-by-message simulation and
// recomputes every round's consumable mirrors, in-flight weight and loads.
//
// Receipt order is derived from seed: seed 0 delivers in canonical order,
// any other seed shuffles each round's release receipts deterministically
// (out-of-order delivery). The coordinator grants a partition only after its
// matching generation-fenced release receipt has actually been observed, so
// shuffling receipts must not change any round-boundary result.
func (p *Problem) Replay(plan *Plan, seed int64) (*ReplayResult, error) {
	return p.ReplayFrom(plan, p.StartPhases(plan.Target), seed)
}

// ReplayFrom executes the action list from an explicit starting phase vector.
// It is used for failover continuations: partitions already RELEASED when the
// new leader takes over have their generation-fenced receipts in the durable
// history, so the continuation's action list correctly contains no repeated
// receipt for them.
func (p *Problem) ReplayFrom(plan *Plan, start []byte, seed int64) (*ReplayResult, error) {
	n := len(p.Partitions)
	if len(start) != n {
		return nil, fmt.Errorf("%w: replay start vector length %d != %d",
			ErrInvalidInput, len(start), n)
	}

	// Build the holder map purely from the problem inputs and the supplied
	// start vector, never from the plan, so the replay is an independent
	// recomputation.
	holder := make([]int, n)
	phases := append([]byte(nil), start...)
	initiallyReleased := map[int]bool{}
	for i := range p.Partitions {
		switch phases[i] {
		case phaseReleased:
			holder[i] = -1
			initiallyReleased[i] = true
		case phaseStableTarget:
			holder[i] = plan.Target[i]
		default:
			holder[i] = p.Partitions[i].Origin
		}
	}

	snap := func(round int) RoundSnapshot {
		return p.snapshot(round, phases, holder)
	}
	res := &ReplayResult{Snapshots: []RoundSnapshot{snap(0)}}
	rng := rand.New(rand.NewSource(seed))

	for _, rd := range plan.Rounds {
		// --- 1. revokes: the old holder must currently own the partition. ---
		for _, g := range rd.Revokes {
			if phases[g.Partition] != phaseStableSource {
				return nil, fmt.Errorf("round %d: revoke of partition %d in wrong phase",
					rd.Round, g.Partition)
			}
			if holder[g.Partition] != g.Consumer {
				return nil, fmt.Errorf("round %d: revoke sent to consumer %d but "+
					"partition %d is held by %d (double-consume risk)",
					rd.Round, g.Consumer, g.Partition, holder[g.Partition])
			}
			holder[g.Partition] = -1
			phases[g.Partition] = phaseReleased
		}

		// --- 2. release receipts, possibly out of order. ---
		receipts := append([]Grant(nil), rd.Releases...)
		if seed != 0 {
			rng.Shuffle(len(receipts), func(i, j int) {
				receipts[i], receipts[j] = receipts[j], receipts[i]
			})
		}
		released := make(map[int]bool, len(receipts))
		generations := make(map[int]int64, len(receipts))
		for _, g := range receipts {
			if phases[g.Partition] != phaseReleased {
				return nil, fmt.Errorf("round %d: unexpected release for partition %d",
					rd.Round, g.Partition)
			}
			released[g.Partition] = true
			generations[g.Partition] = g.Generation
		}

		// --- 3. grants: only after the matching receipt is observed. ---
		grants := append([]Grant(nil), rd.Grants...)
		sort.Slice(grants, func(i, j int) bool { return grants[i].Partition < grants[j].Partition })
		for _, g := range grants {
			// A release receipt is unnecessary when there was no old holder
			// (coordinator-side initial state) or the partition was already
			// RELEASED when this replay's continuation started (its receipt
			// belongs to the durable history before the continuation).
			needsReceipt := p.Partitions[g.Partition].Origin != -1 &&
				!initiallyReleased[g.Partition]
			if needsReceipt {
				if !released[g.Partition] {
					return nil, fmt.Errorf("round %d: grant for partition %d issued "+
						"before its release receipt (would double-consume)",
						rd.Round, g.Partition)
				}
				if generations[g.Partition] != g.Generation {
					return nil, fmt.Errorf("round %d: grant generation %d for partition %d "+
						"does not fence receipt generation %d",
						rd.Round, g.Generation, g.Partition, generations[g.Partition])
				}
			}
			if holder[g.Partition] != -1 {
				return nil, fmt.Errorf("round %d: partition %d granted while still held",
					rd.Round, g.Partition)
			}
			if !p.Partitions[g.Partition].SubsOK[g.Consumer] {
				return nil, fmt.Errorf("round %d: partition %d granted to non-subscriber %d",
					rd.Round, g.Partition, g.Consumer)
			}
			holder[g.Partition] = g.Consumer
			phases[g.Partition] = phaseStableTarget
		}

		s := snap(rd.Round)
		if s.InFlightWeight > p.InFlightCap {
			return nil, fmt.Errorf("round %d: in-flight weight %d exceeds cap %d",
				rd.Round, s.InFlightWeight, p.InFlightCap)
		}
		for fam, n := range s.ConsumableCopies {
			if n < 1 {
				return nil, fmt.Errorf("round %d: family %s has no consumable copy",
					rd.Round, fam)
			}
		}
		res.Snapshots = append(res.Snapshots, s)
	}

	final := make(Assignment, n)
	copy(final, holder)
	for i := range final {
		if final[i] != plan.Target[i] {
			return nil, fmt.Errorf("replay ended with partition %d at consumer %d, target %d",
				i, final[i], plan.Target[i])
		}
	}
	res.Final = final
	res.FinalMaxLoad = maxLoad(p.loads(final), p.capacities())
	return res, nil
}

func (p *Problem) snapshot(round int, phases []byte, holder []int) RoundSnapshot {
	n := len(p.Partitions)
	s := RoundSnapshot{
		Round:            round,
		Phase:            append([]byte(nil), phases...),
		Holder:           append([]int(nil), holder...),
		ConsumableCopies: map[string]int{},
		Loads:            make([]int, len(p.Consumers)),
	}
	for i := 0; i < n; i++ {
		fam := p.Partitions[i].Family
		if phases[i] != phaseReleased {
			s.ConsumableCopies[fam]++
			s.Loads[holder[i]] += p.Partitions[i].Weight
		} else {
			if _, ok := s.ConsumableCopies[fam]; !ok {
				s.ConsumableCopies[fam] = 0
			}
			s.InFlightWeight += p.Partitions[i].Weight
		}
	}
	return s
}
