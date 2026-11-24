package rebalancer

import "sort"

// Hand-off protocol for one partition:
//
//	phase 0  STABLE-AT-SOURCE   old holder serves it
//	         -- revoke (coordinator -> old holder, fenced by generation)
//	phase 1  RELEASED           no one may consume; in hand-off
//	         -- release receipt with generation (old holder -> coordinator),
//	            then grant (coordinator -> new holder)
//	phase 2  STABLE-AT-TARGET   new holder serves it
//
// A round can issue multiple messages. Release receipts may arrive out of
// order; the coordinator only grants once the matching receipt is observed.
//
// At every round boundary two invariants hold:
//   - the total weight of phase-1 partitions (the in-hand-off batch) is at
//     most Problem.InFlightCap;
//   - each data family has at least one partition in phase 0 or 2, i.e. at
//     least one mirror stays consumable.

const (
	phaseStableSource = 0
	phaseReleased     = 1
	phaseStableTarget = 2
)

// Grant is one coordinator message targeting one partition.
type Grant struct {
	Partition  int
	Consumer   int
	Generation int64
}

// Round is the replayable action list of one coordinator round.
type Round struct {
	Round int
	// Revokes are sent at the start of the round to the current old holders.
	Revokes []Grant
	// Releases are the generation-fenced receipts that become observable
	// during this round, immediately before the matching grants are issued.
	// A partition's revoke therefore always appears in an earlier round than
	// its release and grant.
	Releases []Grant
	// Grants are issued only after the matching release receipt is observed
	// (partitions without an original holder have no receipt).
	Grants []Grant
}

// Plan is a minimum-round safe hand-off schedule with full replay data.
type Plan struct {
	Target Assignment
	Rounds []Round
}

// StartPhases derives the initial protocol phases for reaching target.
//
//	partition without an original holder -> RELEASED (granted in round 1)
//	already at target                    -> STABLE-AT-TARGET
//	otherwise                            -> STABLE-AT-SOURCE
func (p *Problem) StartPhases(target Assignment) []byte {
	phases := make([]byte, len(p.Partitions))
	for i := range p.Partitions {
		switch {
		case p.Partitions[i].Origin == -1:
			phases[i] = phaseReleased
		case p.Partitions[i].Origin == target[i]:
			phases[i] = phaseStableTarget
		default:
			phases[i] = phaseStableSource
		}
	}
	return phases
}

type bfsNode struct {
	phases  []byte
	parent  *bfsNode
	revokes []int // partitions revoked on the edge into this node
	grants  []int // partitions granted on the edge into this node
	depth   int
}

// safePhases verifies the two round-boundary invariants: the in-flight weight
// stays within the cap and every family keeps a consumable mirror. It is used
// by tests as an independent check of the BFS's incremental pruning.
func (p *Problem) safePhases(phases []byte) bool {
	inFlight := 0
	famConsumable := map[string]bool{}
	famExists := map[string]bool{}
	for i, ph := range phases {
		pt := &p.Partitions[i]
		famExists[pt.Family] = true
		if ph == phaseReleased {
			inFlight += pt.Weight
			if inFlight > p.InFlightCap {
				return false
			}
		} else {
			famConsumable[pt.Family] = true
		}
	}
	for fam := range famExists {
		if !famConsumable[fam] {
			return false
		}
	}
	return true
}

func (p *Problem) schedule(target Assignment, start []byte) (*Plan, error) {
	return p.scheduleOpts(target, start, true)
}

func (p *Problem) scheduleOpts(target Assignment, start []byte,
	normalizeStableSource bool) (*Plan, error) {
	n := len(p.Partitions)
	if !p.feasible(target) {
		return nil, ErrInfeasible
	}
	// Normalize: a partition marked STABLE-AT-SOURCE whose holder already is
	// the target consumer is indistinguishable from STABLE-AT-TARGET for
	// scheduling purposes (there is nothing to revoke). Recovery and member
	// change callers build phase vectors from holder maps, so without this
	// normalization no-op partitions would make reachable goals look stuck.
	// Callers that already encoded RELEASED explicitly (e.g. orphaned
	// partitions after a member leave) opt out.
	root := &bfsNode{phases: append([]byte(nil), start...)}
	if normalizeStableSource {
		for i := range root.phases {
			if root.phases[i] == phaseStableSource &&
				p.Partitions[i].Origin == target[i] {
				root.phases[i] = phaseStableTarget
			}
		}
	}

	goal := make([]byte, n)
	for i := range goal {
		goal[i] = phaseStableTarget
	}
	if string(root.phases) == string(goal) {
		return &Plan{Target: append(Assignment(nil), target...), Rounds: nil}, nil
	}

	visited := map[string]*bfsNode{string(root.phases): root}
	frontier := []*bfsNode{root}

	var found *bfsNode
	for len(frontier) > 0 && found == nil {
		nextFrontier := make([]*bfsNode, 0)
		for _, cur := range frontier {
			var revocable []int
			var releasable []int // phase-1 items whose receipt is now observable
			for i, ph := range cur.phases {
				switch ph {
				case phaseStableSource:
					revocable = append(revocable, i)
				case phaseReleased:
					releasable = append(releasable, i)
				}
			}

			// One round = grant every already-released partition (its receipt
			// is observable), then revoke a subset of source-stable ones.
			// Granting all releasable items is complete for minimum-round
			// reachability: delaying an available grant never shortens the
			// schedule. A partition stays RELEASED for at least one whole
			// round, so its own revoke never shares a round with its grant.
			grants := append([]int(nil), releasable...)
			base := append([]byte(nil), cur.phases...)
			for _, id := range grants {
				base[id] = phaseStableTarget
			}

			// Consumable mirror count per family in the post-grant base state.
			// A revoke is legal only if its family still has another
			// consumable mirror; this prunes every availability-violating
			// subset incrementally instead of rescanning all partitions.
			famConsumable := map[string]int{}
			for i, ph := range base {
				if ph != phaseReleased {
					famConsumable[p.Partitions[i].Family]++
				}
			}
			// A family whose only mirror is about to be revoked can never be
			// touched; partition-level eligibility below encodes that.
			eligible := make([]int, 0, len(revocable))
			for _, id := range revocable {
				pt := &p.Partitions[id]
				if pt.Weight > p.InFlightCap {
					continue // exceeds the whole in-flight budget
				}
				if famConsumable[pt.Family] <= 1 {
					continue // revoking it would leave zero consumable mirrors
				}
				eligible = append(eligible, id)
			}

			subset := make([]int, 0, len(eligible))
			consumable := make(map[string]int, len(famConsumable))
			for k, v := range famConsumable {
				consumable[k] = v
			}
			emitState := func(nph []byte, revokes, grantList []int) bool {
				if len(revokes) == 0 && len(grantList) == 0 {
					return false // self loop
				}
				key := string(nph)
				if _, seen := visited[key]; seen {
					return false
				}
				child := &bfsNode{
					phases:  nph,
					parent:  cur,
					revokes: append([]int(nil), revokes...),
					grants:  append([]int(nil), grantList...),
					depth:   cur.depth + 1,
				}
				visited[key] = child
				nextFrontier = append(nextFrontier, child)
				if key == string(goal) {
					found = child
					return true
				}
				return false
			}
			var enum func(idx, used int) bool
			enum = func(idx, used int) bool {
				// Emit the current subset (including the empty one when grants
				// make the edge non-trivial).
				nph := append([]byte(nil), base...)
				for _, id := range subset {
					nph[id] = phaseReleased
				}
				if stop := emitState(nph, subset, grants); stop {
					return true
				}
				for j := idx; j < len(eligible); j++ {
					id := eligible[j]
					pt := &p.Partitions[id]
					if used+pt.Weight > p.InFlightCap {
						continue
					}
					// Another consumable mirror of this family must remain.
					if consumable[pt.Family]-1 < 1 {
						continue
					}
					subset = append(subset, id)
					consumable[pt.Family]--
					if enum(j+1, used+pt.Weight) {
						return true
					}
					consumable[pt.Family]++
					subset = subset[:len(subset)-1]
				}
				return false
			}
			if enum(0, 0) {
				break
			}
		}
		frontier = nextFrontier
	}
	if found == nil {
		return nil, ErrNoReachableAssignment
	}

	// Reconstruct rounds from the goal back to the root.
	type edge struct{ revokes, grants []int }
	var edges []edge
	for n := found; n.parent != nil; n = n.parent {
		edges = append(edges, edge{n.revokes, n.grants})
	}
	// Each partition uses one generation on its revoke round; its later
	// grant round reuses that same generation, because the generation fences
	// the whole hand-off and the release receipt carries it.
	revokeGeneration := make(map[int]int64)
	nextGen := int64(1)
	for k := len(edges) - 1; k >= 0; k-- {
		for _, id := range edges[k].revokes {
			revokeGeneration[id] = nextGen
			nextGen++
		}
	}
	rounds := make([]Round, 0, len(edges))
	for k := len(edges) - 1; k >= 0; k-- {
		e := edges[k]
		r := Round{Round: len(rounds) + 1}
		ids := append([]int(nil), e.revokes...)
		sort.Ints(ids)
		for _, id := range ids {
			gen := revokeGeneration[id]
			r.Revokes = append(r.Revokes, Grant{
				Partition: id, Consumer: p.Partitions[id].Origin, Generation: gen,
			})
		}
		gids := append([]int(nil), e.grants...)
		sort.Ints(gids)
		for _, id := range gids {
			gen, ok := revokeGeneration[id]
			if !ok {
				// Partition without an original holder: no revoke/receipt
				// precedes the grant; mint its generation at grant time.
				gen = nextGen
				nextGen++
				revokeGeneration[id] = gen
			} else {
				// The generation-fenced release receipt becomes observable on
				// the grant round, immediately before the grant is issued.
				r.Releases = append(r.Releases, Grant{
					Partition: id, Consumer: p.Partitions[id].Origin,
					Generation: gen,
				})
			}
			r.Grants = append(r.Grants, Grant{
				Partition: id, Consumer: target[id], Generation: gen,
			})
		}
		rounds = append(rounds, r)
	}
	return &Plan{Target: append(Assignment(nil), target...), Rounds: rounds}, nil
}

// Schedule computes the minimum-round safe plan that reaches target from the
// default initial phases. It returns ErrNoReachableAssignment if no sequence
// of legal rounds can reach target.
func (p *Problem) Schedule(target Assignment) (*Plan, error) {
	if err := p.Validate(); err != nil {
		return nil, err
	}
	return p.schedule(target, p.StartPhases(target))
}

// ScheduleFrom computes the minimum-round plan from an explicit phase vector.
// Used by failover recovery, where the new leader reconstructs phases from the
// durable log and consumer-reported generations.
func (p *Problem) ScheduleFrom(target Assignment, start []byte) (*Plan, error) {
	if err := p.Validate(); err != nil {
		return nil, err
	}
	if len(start) != len(p.Partitions) {
		return nil, ErrInvalidInput
	}
	return p.schedule(target, append([]byte(nil), start...))
}
