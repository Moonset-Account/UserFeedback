package rebalancer

import (
	"errors"
	"fmt"
	"math/big"
	"sort"
)

// ErrNoReachableAssignment means no feasible terminal table can be handed over
// safely under the in-flight / family-availability constraints.
var ErrNoReachableAssignment = errors.New("rebalancer: no safely reachable assignment")

// Target is a fully evaluated terminal ownership table.
type Target struct {
	Assign    Assignment
	MaxLoad   *big.Rat // exact max_c load[c]/capacity[c]
	Migration int      // total weight that must move away from original holders
	Rank      int      // ordinal in the strict lexicographic objective order (0 = best)
}

func targetFor(p *Problem, a Assignment) Target {
	return Target{
		Assign:    append(Assignment(nil), a...),
		MaxLoad:   p.MaxNormalizedLoad(a),
		Migration: p.MigrationWeight(a),
	}
}

// compareTargets implements the exact lexicographic objective:
//
//  1. smaller maximum normalized load (exact fractions)
//  2. smaller total migrated weight
//  3. lexicographically smaller ownership table by partition number
//
// Returns -1 if a is better, +1 if b is better, 0 on full tie.
func compareTargets(a, b *Target) int {
	if c := a.MaxLoad.Cmp(b.MaxLoad); c != 0 {
		return c
	}
	if a.Migration != b.Migration {
		if a.Migration < b.Migration {
			return -1
		}
		return 1
	}
	for i := range a.Assign {
		if a.Assign[i] != b.Assign[i] {
			if a.Assign[i] < b.Assign[i] {
				return -1
			}
			return 1
		}
	}
	return 0
}

// solver runs depth-first branch and bound over ownership tables.
type solver struct {
	p        *Problem
	n        int
	nc       int
	assign   Assignment
	loads    []int
	famUsed  []map[string]bool // family index -> domains in use
	famOf    []int             // search position -> family index
	famNames []string
	migrated int

	// order maps each search position to the partition number placed there.
	// Partitions are searched heaviest-first so loads differentiate early and
	// branch-and-bound prunes aggressively; lexicographic tie resolution is
	// independent of search order because it is applied on complete tables.
	order []int
	// posOf is the inverse of order.
	posOf []int

	// allowedRemaining[pos][c] is the total weight of partitions at search
	// positions >= pos that consumer c subscribes to.
	allowedRemaining [][]int

	// fixed forces certain partitions to given consumers (recovery / member
	// leave: hand-overs already completed are inherited as-is).
	fixed map[int]int

	// anchor, when non-nil, makes the search return only targets that are
	// strictly worse than it (used to enumerate the next reachable target).
	anchor *Target

	// loadCap, when non-nil, restricts the search to tables whose maximum
	// normalized load does not exceed it.
	loadCap *big.Rat

	best    Assignment
	bestMax *big.Rat
	bestMig int
	found   bool
}

func (s *solver) familyIndex(name string) int {
	for i, n := range s.famNames {
		if n == name {
			return i
		}
	}
	s.famNames = append(s.famNames, name)
	s.famUsed = append(s.famUsed, map[string]bool{})
	return len(s.famNames) - 1
}

func (s *solver) canPick(partID, c int) bool {
	pt := &s.p.Partitions[partID]
	if !pt.SubsOK[c] {
		return false
	}
	dom := s.p.Consumers[c].Domain
	return !s.famUsed[s.famOf[s.posOf[partID]]][dom]
}

func (s *solver) betterThanBest(t *Target) bool {
	if !s.found {
		return true
	}
	cur := Target{Assign: s.best, MaxLoad: s.bestMax, Migration: s.bestMig}
	return compareTargets(t, &cur) < 0
}

func (s *solver) place(partID, c int) {
	pt := &s.p.Partitions[partID]
	s.assign[partID] = c
	s.loads[c] += pt.Weight
	s.famUsed[s.famOf[s.posOf[partID]]][s.p.Consumers[c].Domain] = true
	if pt.Origin >= 0 && c != pt.Origin {
		s.migrated += pt.Weight
	}
}

func (s *solver) unplace(partID, c int) {
	pt := &s.p.Partitions[partID]
	s.assign[partID] = -1
	s.loads[c] -= pt.Weight
	dom := s.p.Consumers[c].Domain
	// A domain is only marked by one mirror of a family at any time, so
	// deleting the marker on unplace is exact.
	delete(s.famUsed[s.famOf[s.posOf[partID]]], dom)
	if pt.Origin >= 0 && c != pt.Origin {
		s.migrated -= pt.Weight
	}
}

// dfs is the full-objective branch-and-bound search (used once the optimal
// maximum load is known). It minimizes migration weight and then resolves
// lexicographic ties among tables that respect the load cap.
func (s *solver) dfs(pos int) {
	if pos == s.n {
		t := targetFor(s.p, s.assign)
		if s.anchor != nil && compareTargets(&t, s.anchor) <= 0 {
			return
		}
		if s.betterThanBest(&t) {
			s.best = append(Assignment(nil), s.assign...)
			s.bestMax = new(big.Rat).Set(t.MaxLoad)
			s.bestMig = t.Migration
			s.found = true
		}
		return
	}

	curMax := maxLoad(s.loads, s.p.capacities())
	if s.loadCap != nil && curMax.Cmp(s.loadCap) > 0 {
		return
	}
	if s.found && curMax.Cmp(s.bestMax) > 0 {
		return
	}
	// Admissible relaxation lower bound on the eventual maximum load:
	//   (a) per-consumer (load[c] + reachable suffix weight) / cap[c];
	//   (b) global total weight / total capacity, which the maximum cannot
	//       be below because it is the capacity-weighted average.
	if s.found {
		remaining := 0
		for k := pos; k < s.n; k++ {
			remaining += s.p.Partitions[s.order[k]].Weight
		}
		lb := intRat(0)
		var totalCap int64
		for c := range s.loads {
			r := loadFrac(s.loads[c]+s.allowedRemaining[pos][c],
				s.p.Consumers[c].Capacity)
			if r.Cmp(lb) > 0 {
				lb = r
			}
			totalCap += int64(s.p.Consumers[c].Capacity)
		}
		var placedTotal int64
		for k := 0; k < pos; k++ {
			placedTotal += int64(s.p.Partitions[s.order[k]].Weight)
		}
		global := new(big.Rat).SetFrac(
			big.NewInt(placedTotal+int64(remaining)), big.NewInt(totalCap))
		if global.Cmp(lb) > 0 {
			lb = global
		}
		if lb.Cmp(s.bestMax) > 0 {
			return
		}
	}
	if s.found && s.migrated > s.bestMig && curMax.Cmp(s.bestMax) >= 0 {
		return
	}

	partID := s.order[pos]
	if c, ok := s.fixed[partID]; ok {
		if !s.canPick(partID, c) {
			return
		}
		s.place(partID, c)
		s.dfs(pos + 1)
		s.unplace(partID, c)
		return
	}

	// Feasible consumers ordered by resulting normalized load, smallest
	// first, to reach a strong incumbent quickly.
	type cand struct {
		c int
		r *big.Rat
	}
	cands := make([]cand, 0, s.nc)
	for c := 0; c < s.nc; c++ {
		if !s.canPick(partID, c) {
			continue
		}
		cands = append(cands, cand{
			c: c,
			r: loadFrac(s.loads[c]+s.p.Partitions[partID].Weight,
				s.p.Consumers[c].Capacity),
		})
	}
	sort.SliceStable(cands, func(i, j int) bool {
		if cmp := cands[i].r.Cmp(cands[j].r); cmp != 0 {
			return cmp < 0
		}
		return cands[i].c < cands[j].c
	})
	for _, cd := range cands {
		s.place(partID, cd.c)
		s.dfs(pos + 1)
		s.unplace(partID, cd.c)
	}
}

// feasibleUnderCap reports whether any feasible table (respecting fixed
// holders) keeps every normalized load at or below cap_.
func (p *Problem) feasibleUnderCap(cap_ *big.Rat, fixed map[int]int) (Assignment, bool) {
	s := p.newSolver(fixed, nil, cap_)
	if s.feasDFS(0) {
		return append(Assignment(nil), s.assign...), true
	}
	return nil, false
}

// feasDFS is the capped decision-procedure search. On success it leaves the
// complete assignment in s.assign; on failure it backtracks every placement.
func (s *solver) feasDFS(pos int) bool {
	if pos == s.n {
		return true
	}
	partID := s.order[pos]
	try := func(c int) bool {
		if !s.canPick(partID, c) {
			return false
		}
		r := loadFrac(s.loads[c]+s.p.Partitions[partID].Weight,
			s.p.Consumers[c].Capacity)
		if r.Cmp(s.loadCap) > 0 {
			return false
		}
		s.place(partID, c)
		if s.feasDFS(pos + 1) {
			return true // keep the placement: it belongs to the witness table
		}
		s.unplace(partID, c)
		return false
	}
	if c, ok := s.fixed[partID]; ok {
		return try(c)
	}
	for c := 0; c < s.nc; c++ {
		if try(c) {
			return true
		}
	}
	return false
}

// greedyIncumbent builds a feasible table by placing each partition on the
// subscribing consumer whose resulting normalized load stays smallest. It
// only seeds branch-and-bound; the exact searches still prove optimality.
func (p *Problem) greedyIncumbent(fixed map[int]int) (Assignment, bool) {
	n := len(p.Partitions)
	a := make(Assignment, n)
	for i := range a {
		a[i] = -1
	}
	loads := make([]int, len(p.Consumers))
	famUsed := map[string]map[string]bool{}
	for i := range p.Partitions {
		pt := &p.Partitions[i]
		if famUsed[pt.Family] == nil {
			famUsed[pt.Family] = map[string]bool{}
		}
		pick := -1
		if c, ok := fixed[i]; ok {
			pick = c
		} else {
			var best *big.Rat
			for c := range p.Consumers {
				if !pt.SubsOK[c] || famUsed[pt.Family][p.Consumers[c].Domain] {
					continue
				}
				r := loadFrac(loads[c]+pt.Weight, p.Consumers[c].Capacity)
				if best == nil || r.Cmp(best) < 0 {
					best, pick = r, c
				}
			}
		}
		if pick < 0 {
			return nil, false
		}
		a[i] = pick
		loads[pick] += pt.Weight
		famUsed[pt.Family][p.Consumers[pick].Domain] = true
	}
	return a, true
}

// candidateLoads enumerates the distinct normalized loads any consumer could
// realize: achievable subset sums of its subscribable weights over its
// capacity. The optimal maximum normalized load is one of these fractions.
func (p *Problem) candidateLoads(fixed map[int]int) []*big.Rat {
	seen := map[string]*big.Rat{}
	for c := range p.Consumers {
		mandatory := 0
		for i := range p.Partitions {
			if fc, ok := fixed[i]; ok && fc == c {
				mandatory += p.Partitions[i].Weight
			}
		}
		sums := map[int]bool{0: true}
		for i := range p.Partitions {
			if !p.Partitions[i].SubsOK[c] {
				continue
			}
			if fc, ok := fixed[i]; ok && fc != c {
				continue
			}
			w := p.Partitions[i].Weight
			for s := range sums {
				sums[s+w] = true
			}
		}
		for s := range sums {
			if s == 0 && mandatory == 0 {
				continue
			}
			r := loadFrac(s+mandatory, p.Consumers[c].Capacity)
			seen[r.RatString()] = r
		}
	}
	out := make([]*big.Rat, 0, len(seen))
	for _, r := range seen {
		out = append(out, r)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Cmp(out[j]) < 0 })
	return out
}

// newSolver builds the reordered branch-and-bound solver state.
func (p *Problem) newSolver(fixed map[int]int, anchor *Target,
	loadCap *big.Rat) *solver {
	n := len(p.Partitions)
	s := &solver{
		p:       p,
		n:       n,
		nc:      len(p.Consumers),
		assign:  make(Assignment, n),
		loads:   make([]int, len(p.Consumers)),
		famOf:   make([]int, n),
		order:   make([]int, n),
		posOf:   make([]int, n),
		fixed:   fixed,
		anchor:  anchor,
		loadCap: loadCap,
		bestMax: intRat(0),
	}
	for i := range s.order {
		s.order[i] = i
	}
	sort.SliceStable(s.order, func(i, j int) bool {
		pi, pj := &p.Partitions[s.order[i]], &p.Partitions[s.order[j]]
		if pi.Weight != pj.Weight {
			return pi.Weight > pj.Weight
		}
		return pi.ID < pj.ID
	})
	for pos, partID := range s.order {
		s.posOf[partID] = pos
	}
	for i := range s.assign {
		s.assign[i] = -1
	}
	for pos, partID := range s.order {
		s.famOf[pos] = s.familyIndex(p.Partitions[partID].Family)
	}
	s.allowedRemaining = make([][]int, n+1)
	s.allowedRemaining[n] = make([]int, s.nc)
	for pos := n - 1; pos >= 0; pos-- {
		s.allowedRemaining[pos] = append([]int(nil), s.allowedRemaining[pos+1]...)
		partID := s.order[pos]
		for c := 0; c < s.nc; c++ {
			if p.Partitions[partID].SubsOK[c] {
				s.allowedRemaining[pos][c] += p.Partitions[partID].Weight
			}
		}
	}
	return s
}

// solveWithBound runs one search. anchor == nil returns the globally optimal
// table; anchor != nil returns the best table strictly worse than the anchor.
func (p *Problem) solveWithBound(anchor *Target, fixed map[int]int) (Target, error) {
	if err := p.Validate(); err != nil {
		return Target{}, err
	}

	if anchor == nil {
		// Phase 1: smallest achievable maximum normalized load, found by
		// testing sorted candidate fractions with the capped decision search.
		var minCap *big.Rat
		var witness Assignment
		for _, cand := range p.candidateLoads(fixed) {
			if a, ok := p.feasibleUnderCap(cand, fixed); ok {
				minCap, witness = cand, a
				break
			}
		}
		if minCap == nil {
			return Target{}, ErrInfeasible
		}
		// Phase 2: within minCap, minimize migration then lexicographic order.
		s := p.newSolver(fixed, nil, minCap)
		st := targetFor(p, witness)
		s.best = append(Assignment(nil), witness...)
		s.bestMax = st.MaxLoad
		s.bestMig = st.Migration
		s.found = true
		s.dfs(0)
		return Target{
			Assign:    s.best,
			MaxLoad:   s.bestMax,
			Migration: s.bestMig,
		}, nil
	}

	// Anchored enumeration: only tables strictly worse than the anchor.
	s := p.newSolver(fixed, anchor, nil)
	if seed, ok := p.greedyIncumbent(fixed); ok {
		st := targetFor(p, seed)
		if p.feasible(seed) && compareTargets(&st, anchor) > 0 {
			s.best = seed
			s.bestMax = st.MaxLoad
			s.bestMig = st.Migration
			s.found = true
		}
	}
	s.dfs(0)
	if !s.found {
		return Target{}, ErrNoReachableAssignment
	}
	return Target{
		Assign:    s.best,
		MaxLoad:   s.bestMax,
		Migration: s.bestMig,
	}, nil
}

// Solve returns the globally optimal terminal ownership table under the exact
// three-level lexicographic objective.
func (p *Problem) Solve() (Target, error) {
	return p.solveWithBound(nil, nil)
}

// SolveFixed returns the optimal table with the given partitions pinned to
// their current consumers.
func (p *Problem) SolveFixed(fixed map[int]int) (Target, error) {
	return p.solveWithBound(nil, fixed)
}

// maxEnumerations caps the search over "next worse" tables.
const maxEnumerations = 1_000_000

// SolveReachable finds the best terminal table (in objective order) that can
// actually be handed over safely. If the globally optimal terminal table has
// no safe move schedule, the next worse reachable table is selected.
func (p *Problem) SolveReachable() (Target, *Plan, error) {
	best, err := p.Solve()
	if err != nil {
		return Target{}, nil, err
	}
	rank := 0
	for {
		if rank > maxEnumerations {
			return Target{}, nil, fmt.Errorf("%w: enumeration limit exceeded",
				ErrNoReachableAssignment)
		}
		plan, schedErr := p.Schedule(best.Assign)
		if schedErr == nil {
			best.Rank = rank
			return best, plan, nil
		}
		next, err := p.solveWithBound(&best, nil)
		if err != nil {
			return Target{}, nil, err
		}
		best = next
		rank++
	}
}
