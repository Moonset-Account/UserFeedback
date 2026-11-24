package rebalancer

import (
	"errors"
	"math/big"
	"testing"
	"time"
)

// ---- test fixtures -------------------------------------------------------

func makeSubs(m int, allowed ...int) []bool {
	s := make([]bool, m)
	for _, c := range allowed {
		s[c] = true
	}
	return s
}

func partition(id, weight int, family, domain string, origin int, subs []bool) Partition {
	return Partition{
		ID: id, Weight: weight, Family: family, Domain: domain,
		Origin: origin, SubsOK: subs,
	}
}

func consumers(specs ...[2]string) []Consumer {
	cs := make([]Consumer, len(specs))
	for i, s := range specs {
		cs[i] = Consumer{ID: i, Capacity: atoiSafe(s[1]), Domain: s[0]}
	}
	return cs
}

func atoiSafe(s string) int {
	n := 0
	for _, r := range s {
		n = n*10 + int(r-'0')
	}
	return n
}

// ---- 1. normal scenario: hand-computed optimum and minimum-round plan ----

func TestNormalOptimalAssignment(t *testing.T) {
	// Families F/G each have two mirrors; two domains, two consumers,
	// capacities (4, 4), all weights 2. Origins: p0,p2 -> c0; p1,p3 -> c1.
	p := &Problem{
		Partitions: []Partition{
			partition(0, 2, "F", "dX", 0, makeSubs(2, 0, 1)),
			partition(1, 2, "F", "dY", 1, makeSubs(2, 0, 1)),
			partition(2, 2, "G", "dX", 0, makeSubs(2, 0, 1)),
			partition(3, 2, "G", "dY", 1, makeSubs(2, 0, 1)),
		},
		Consumers:   consumers([2]string{"dX", "4"}, [2]string{"dY", "4"}),
		InFlightCap: 10,
	}
	got, err := p.Solve()
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	wantAssign := Assignment{0, 1, 0, 1}
	if !assignEqual(got.Assign, wantAssign) {
		t.Fatalf("assignment = %v, want %v", got.Assign, wantAssign)
	}
	// Loads (4,4), capacities (4,4): exact max normalized load = 1.
	if got.MaxLoad.Cmp(big.NewRat(1, 1)) != 0 {
		t.Fatalf("max load = %s, want 1/1", got.MaxLoad)
	}
	if got.Migration != 0 {
		t.Fatalf("migration = %d, want 0", got.Migration)
	}
	// Feasibility invariant: same family never shares a domain.
	assertFamilyDomains(t, p, got.Assign)

	plan, err := p.Schedule(got.Assign)
	if err != nil {
		t.Fatalf("Schedule: %v", err)
	}
	if len(plan.Rounds) != 0 {
		t.Fatalf("zero-migration plan must take 0 rounds, got %d", len(plan.Rounds))
	}
}

// ---- 2. normal scenario with migration: cap serializes the hand-off -------

func TestMigrationTwoRoundsAndReplay(t *testing.T) {
	// 3 partitions, weights 3,2,2; capacities (2,5). Origins all on c0.
	// F mirrors p0,p1 must split domains (p0 is c0-only); p2 is family H's
	// second mirror so it is safe to revoke (p3 keeps H consumable).
	// Candidate tables (p0 is fixed at c0):
	//   [0,0,0]: loads (7,0) -> max 7/2 = 3.5
	//   [0,1,0]: loads (5,2) -> max 5/2 = 2.5, migration 2
	//   [0,0,1]: loads (5,2) -> max 5/2 = 2.5, migration 2
	//   [0,1,1]: loads (3,4) -> max 3/2 = 1.5, migration 4
	// The exact-fraction optimum is [0,1,1] even though it moves more
	// weight: the primary objective dominates.
	p := &Problem{
		Partitions: []Partition{
			partition(0, 3, "F", "dX", 0, makeSubs(2, 0)), // c0 only
			partition(1, 2, "F", "dY", 0, makeSubs(2, 0, 1)),
			partition(2, 2, "H", "dY", 0, makeSubs(2, 0, 1)),
			partition(3, 1, "H", "dX", 0, makeSubs(2, 0)), // c0 only
		},
		Consumers:   consumers([2]string{"dX", "2"}, [2]string{"dY", "5"}),
		InFlightCap: 4,
	}
	got, err := p.Solve()
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if !assignEqual(got.Assign, Assignment{0, 1, 1, 0}) {
		t.Fatalf("assignment = %v, want [0 1 1 0]", got.Assign)
	}
	// Loads: c0 = (3+1)/2 = 2; c1 = 4/5 = 0.8. Exact maximum is 2.
	if got.MaxLoad.Cmp(big.NewRat(2, 1)) != 0 {
		t.Fatalf("max load = %s, want 2/1", got.MaxLoad)
	}
	if got.Migration != 4 {
		t.Fatalf("migration = %d, want 4", got.Migration)
	}

	plan, err := p.Schedule(got.Assign)
	if err != nil {
		t.Fatalf("Schedule: %v", err)
	}
	// Cap 4 admits both weight-2 hand-offs in one in-flight batch, so the
	// minimum is two rounds: one revoke round, one grant round.
	if len(plan.Rounds) != 2 {
		t.Fatalf("rounds = %d, want 2", len(plan.Rounds))
	}
	if len(plan.Rounds[0].Revokes) != 2 {
		t.Fatalf("round 1 must batch both revokes, got %+v", plan.Rounds[0])
	}
	if len(plan.Rounds[1].Grants) != 2 {
		t.Fatalf("round 2 must issue both grants, got %+v", plan.Rounds[1])
	}
	if plan.Rounds[1].Releases[0].Generation != plan.Rounds[0].Revokes[0].Generation {
		t.Fatalf("release receipt must carry the revoke generation")
	}

	// Replay both in-order and with shuffled receipts; boundary snapshots
	// must be identical and every family must keep one consumable mirror.
	base, err := p.Replay(plan, 0)
	if err != nil {
		t.Fatalf("Replay(seed 0): %v", err)
	}
	shuffled, err := p.Replay(plan, 42)
	if err != nil {
		t.Fatalf("Replay(seed 42): %v", err)
	}
	if len(base.Snapshots) != len(shuffled.Snapshots) {
		t.Fatalf("snapshot count differs under receipt shuffle")
	}
	for k := range base.Snapshots {
		a, b := base.Snapshots[k], shuffled.Snapshots[k]
		if !byteSliceEqual(a.Phase, b.Phase) || !intSliceEqual(a.Holder, b.Holder) {
			t.Fatalf("round %d snapshots differ under out-of-order receipts", k)
		}
		for fam, n := range a.ConsumableCopies {
			if n < 1 {
				t.Fatalf("round %d family %s has no consumable copy", k, fam)
			}
		}
	}
	// Round 1 is the in-flight point: both moving partitions are released
	// (weight 4 = cap), while each family keeps its other mirror consumable.
	mid := base.Snapshots[1]
	if mid.InFlightWeight != 4 || mid.ConsumableCopies["F"] != 1 ||
		mid.ConsumableCopies["H"] != 1 {
		t.Fatalf("mid-round state wrong: %+v", mid)
	}
	// Independently recomputed final load must equal the solver's fraction.
	if base.FinalMaxLoad.Cmp(got.MaxLoad) != 0 {
		t.Fatalf("replay final load %s != solver load %s",
			base.FinalMaxLoad, got.MaxLoad)
	}
	if !assignEqual(base.Final, got.Assign) {
		t.Fatalf("replay final %v != target %v", base.Final, got.Assign)
	}
}

// ---- 3. boundary: in-flight cap forces serialization (min rounds 3) -------

func TestInFlightCapSerializes(t *testing.T) {
	// Family G has two mirrors of weight 4 held by c0. The cap is 4, so
	// both fit one in-flight batch. RELEASED must span a round, giving the
	// minimum two rounds: revoke together, then grant together.
	p := &Problem{
		Partitions: []Partition{
			partition(0, 4, "G", "dX", 0, makeSubs(2, 0, 1)),
			partition(1, 4, "G", "dY", 0, makeSubs(2, 0, 1)),
		},
		Consumers:   consumers([2]string{"dX", "10"}, [2]string{"dY", "10"}),
		InFlightCap: 4,
	}
	got, err := p.Solve()
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if !assignEqual(got.Assign, Assignment{0, 1}) {
		t.Fatalf("assignment = %v, want [0 1]", got.Assign)
	}
	plan, err := p.Schedule(got.Assign)
	if err != nil {
		t.Fatalf("Schedule: %v", err)
	}
	// With cap 4 both two-round paths (one batched revoke or two sequential
	// revokes) are optimal; the total protocol work is fixed regardless.
	if len(plan.Rounds) != 2 {
		t.Fatalf("rounds = %d, want 2 (revoke round then grant round)", len(plan.Rounds))
	}
	moved := 0
	for _, pt := range p.Partitions {
		if pt.Origin != got.Assign[pt.ID] {
			moved++
		}
	}
	totalRevokes := len(plan.Rounds[0].Revokes) + len(plan.Rounds[1].Revokes)
	totalGrants := len(plan.Rounds[0].Grants) + len(plan.Rounds[1].Grants)
	if totalRevokes != moved || totalGrants != moved {
		t.Fatalf("expected %d revokes and grants each, got %+v", moved, plan.Rounds)
	}

	// With a cap that admits the whole batch, an explicit capacity-wide
	// problem batches the revokes into round 1. Each moving family keeps a
	// second mirror consumable throughout, so the batch is availability-safe.
	// Families G and H each contribute one moving mirror plus one pinned
	// mirror that stays on its origin consumer.
	batched := &Problem{
		Partitions: []Partition{
			partition(0, 2, "G", "dY", 0, makeSubs(2, 0, 1)), // moves to c1
			partition(1, 1, "G", "dX", 0, makeSubs(2, 0)),    // pinned c0
			partition(2, 2, "H", "dY", 0, makeSubs(2, 0, 1)), // moves to c1
			partition(3, 1, "H", "dX", 0, makeSubs(2, 0)),    // pinned c0
		},
		Consumers:   consumers([2]string{"dX", "10"}, [2]string{"dY", "10"}),
		InFlightCap: 4,
	}
	bp, err := batched.Schedule(Assignment{1, 0, 1, 0})
	if err != nil {
		t.Fatalf("batched Schedule: %v", err)
	}
	if len(bp.Rounds) != 2 || len(bp.Rounds[0].Revokes) != 2 {
		t.Fatalf("cap-wide batch must revoke both moving mirrors in round 1, got %+v",
			bp.Rounds)
	}
	res, err := p.Replay(plan, 0)
	if err != nil {
		t.Fatalf("Replay: %v", err)
	}
	for _, s := range res.Snapshots {
		if s.InFlightWeight > 4 {
			t.Fatalf("cap violated: %d", s.InFlightWeight)
		}
		if s.ConsumableCopies["G"] < 1 {
			t.Fatalf("family G lost all consumable copies")
		}
	}

	// Tighten the cap below one partition's weight: no hand-off is possible
	// at all, so the target is correctly reported unreachable.
	tight := *p
	tight.InFlightCap = 3
	if _, err := tight.Schedule(got.Assign); !errors.Is(err, ErrNoReachableAssignment) {
		t.Fatalf("overweight hand-off: want ErrNoReachableAssignment, got %v", err)
	}

	// Cap 4 with two weight-4 moving partitions, each from a different
	// family and each backed by a weight-1 pinned mirror so availability is
	// never at risk. Only one moving partition fits the in-flight batch, so
	// the cap serializes them: revoke | grant+revoke | grant (3 rounds).
	serial := &Problem{
		Partitions: []Partition{
			partition(0, 4, "K", "dY", 0, makeSubs(2, 0, 1)), // moves c0->c1
			partition(1, 1, "K", "dX", 0, makeSubs(2, 0)),    // pinned c0
			partition(2, 4, "L", "dY", 0, makeSubs(2, 0, 1)), // moves c0->c1
			partition(3, 1, "L", "dX", 0, makeSubs(2, 0)),    // pinned c0
		},
		Consumers:   consumers([2]string{"dX", "10"}, [2]string{"dY", "10"}),
		InFlightCap: 4,
	}
	serialTarget := Assignment{1, 0, 1, 0}
	sp, err := serial.Schedule(serialTarget)
	if err != nil {
		t.Fatalf("serial Schedule: %v", err)
	}
	if len(sp.Rounds) != 3 {
		t.Fatalf("serialized rounds = %d, want 3", len(sp.Rounds))
	}
	// The middle round must interleave a grant (receipt of the first
	// hand-off) with a revoke (start of the second) in one round.
	if len(sp.Rounds[1].Grants) != 1 || len(sp.Rounds[1].Revokes) != 1 {
		t.Fatalf("middle round must interleave one grant and one revoke: %+v",
			sp.Rounds[1])
	}
	if sres, err := serial.Replay(sp, 3); err != nil {
		t.Fatalf("serial replay: %v", err)
	} else if !assignEqual(sres.Final, serialTarget) {
		t.Fatalf("serial replay final = %v", sres.Final)
	}
}

// ---- 14. scale: the maximum 16 partitions solve and schedule quickly ------

func TestScaleSixteenPartitions(t *testing.T) {
	// 16 partitions over 4 families (4 mirrors each) and 4 consumers in 4
	// fault domains, so every family can spread one mirror per domain.
	const n = MaxPartitions
	subs := make([]bool, 4)
	for c := range subs {
		subs[c] = true
	}
	ps := make([]Partition, n)
	for i := range ps {
		ps[i] = partition(i, (i%3)+1,
			[]string{"F0", "F1", "F2", "F3"}[i%4],
			[]string{"d0", "d1", "d2", "d3"}[i%4],
			0, append([]bool(nil), subs...))
	}
	cs := make([]Consumer, 4)
	for i := range cs {
		cs[i] = Consumer{ID: i, Capacity: 10, Domain: "d" + string(rune('0'+i))}
	}
	p := &Problem{Partitions: ps, Consumers: cs, InFlightCap: 6}

	start := time.Now()
	got, err := p.Solve()
	if err != nil {
		t.Fatalf("Solve at 16 partitions: %v", err)
	}
	assertFamilyDomains(t, p, got.Assign)

	plan, err := p.Schedule(got.Assign)
	if err != nil {
		t.Fatalf("Schedule at 16 partitions: %v", err)
	}
	// Independently verify every round boundary against the two invariants.
	phases := p.StartPhases(got.Assign)
	if !p.safePhases(phases) {
		t.Fatalf("initial phases violate a safety invariant")
	}
	res, err := p.Replay(plan, 99)
	if err != nil {
		t.Fatalf("Replay at 16 partitions: %v", err)
	}
	for _, s := range res.Snapshots {
		if !p.safePhases(s.Phase) {
			t.Fatalf("round %d phases violate a safety invariant: %v", s.Round, s.Phase)
		}
	}
	if !assignEqual(res.Final, got.Assign) {
		t.Fatalf("scale replay final mismatch")
	}
	if time.Since(start) > 2*time.Second {
		t.Fatalf("16-partition solve+schedule took too long: %v", time.Since(start))
	}
	t.Logf("16 partitions: maxLoad=%s migration=%d rounds=%d elapsed=%v",
		got.MaxLoad, got.Migration, len(plan.Rounds), time.Since(start))
}

// ---- 4. boundary: optimal terminal unsafe -> next reachable target --------

func TestFallbackToNextReachable(t *testing.T) {
	// Family K has one single mirror p0. Optimal normalized load would put
	// it on c1, but moving it leaves zero consumable mirrors mid-flight;
	// additionally the in-flight cap (2) is below its weight (4), so it can
	// never be revoked at all. The only safely reachable table keeps it at
	// its origin c0.
	p := &Problem{
		Partitions: []Partition{
			partition(0, 4, "K", "dX", 0, makeSubs(2, 0, 1)),
			partition(1, 1, "L", "dY", -1, makeSubs(2, 0, 1)),
		},
		Consumers:   consumers([2]string{"dX", "2"}, [2]string{"dY", "5"}),
		InFlightCap: 2,
	}
	// Sanity: the unconstrained optimum wants p0 on c1.
	opt, err := p.Solve()
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if opt.Assign[0] != 1 {
		t.Fatalf("unconstrained optimum should prefer c1, got %v", opt.Assign)
	}
	if _, schedErr := p.Schedule(opt.Assign); !errors.Is(schedErr, ErrNoReachableAssignment) {
		t.Fatalf("optimal table must be unreachable, got err=%v", schedErr)
	}

	target, plan, err := p.SolveReachable()
	if err != nil {
		t.Fatalf("SolveReachable: %v", err)
	}
	if target.Rank == 0 {
		t.Fatalf("expected fallback to a worse-ranked table")
	}
	// The reachable table must be strictly worse than the unreachable optimum
	// under the exact lexicographic objective.
	if compareTargets(&target, &opt) <= 0 {
		t.Fatalf("reachable table %v must be worse than optimum %v",
			target.Assign, opt.Assign)
	}
	if target.Assign[0] != 0 {
		t.Fatalf("reachable table must pin single mirror at origin, got %v", target.Assign)
	}
	if len(plan.Rounds) != 1 {
		t.Fatalf("only the unowned p1 needs a grant: 1 round, got %d", len(plan.Rounds))
	}
	res, err := p.Replay(plan, 7)
	if err != nil {
		t.Fatalf("Replay: %v", err)
	}
	if !assignEqual(res.Final, target.Assign) {
		t.Fatalf("replay final %v != reachable target %v", res.Final, target.Assign)
	}
}

// ---- 5. boundary: no-move instance, zero rounds ---------------------------

func TestZeroRoundsWhenAlreadyOptimal(t *testing.T) {
	p := &Problem{
		Partitions: []Partition{
			partition(0, 1, "F", "dX", 0, makeSubs(1, 0)),
		},
		Consumers:   consumers([2]string{"dX", "1"}),
		InFlightCap: 1,
	}
	got, err := p.Solve()
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	plan, err := p.Schedule(got.Assign)
	if err != nil {
		t.Fatalf("Schedule: %v", err)
	}
	if len(plan.Rounds) != 0 {
		t.Fatalf("want 0 rounds, got %d", len(plan.Rounds))
	}
	res, err := p.Replay(plan, 0)
	if err != nil {
		t.Fatalf("Replay: %v", err)
	}
	if len(res.Snapshots) != 1 || res.Snapshots[0].ConsumableCopies["F"] != 1 {
		t.Fatalf("empty-plan snapshot wrong: %+v", res.Snapshots)
	}
}

// ---- 6. exact fractions: unequal capacities, compare exactly --------------

func TestExactFractionComparison(t *testing.T) {
	// Weights 1,1,1, capacities (2,3), all subscribable, distinct families
	// so domain constraints never bind. Balanced tables give loads
	// (1,2): max = max(1/2, 2/3) = 2/3 exactly.
	p := &Problem{
		Partitions: []Partition{
			partition(0, 1, "F0", "dX", -1, makeSubs(2, 0, 1)),
			partition(1, 1, "F1", "dY", -1, makeSubs(2, 0, 1)),
			partition(2, 1, "F2", "dX", -1, makeSubs(2, 0, 1)),
		},
		Consumers:   consumers([2]string{"dX", "2"}, [2]string{"dY", "3"}),
		InFlightCap: 10,
	}
	got, err := p.Solve()
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if got.MaxLoad.Cmp(big.NewRat(2, 3)) != 0 {
		t.Fatalf("max load = %s, want exactly 2/3", got.MaxLoad)
	}
	if got.Migration != 0 { // no origins -> no migration cost
		t.Fatalf("migration = %d, want 0 for unowned partitions", got.Migration)
	}
}

// ---- 7. lexicographic tie break on equal objective pairs -----------------

func TestLexicographicTieBreak(t *testing.T) {
	// Two weight-1 partitions, equal capacities, distinct single-mirror
	// families, both subscribable everywhere and no origins. Every table
	// that splits them ties on load (1/2) and migration (0); the
	// lexicographically smallest table is [0,1] vs [1,0].
	p := &Problem{
		Partitions: []Partition{
			partition(0, 1, "F0", "dX", -1, makeSubs(2, 0, 1)),
			partition(1, 1, "F1", "dY", -1, makeSubs(2, 0, 1)),
		},
		Consumers:   consumers([2]string{"dX", "1"}, [2]string{"dY", "1"}),
		InFlightCap: 10,
	}
	got, err := p.Solve()
	if err != nil {
		t.Fatalf("Solve: %v", err)
	}
	if !assignEqual(got.Assign, Assignment{0, 1}) {
		t.Fatalf("lex tie break = %v, want [0 1]", got.Assign)
	}

	// Determinism: repeated solves produce identical tables and fractions.
	got2, err := p.Solve()
	if err != nil {
		t.Fatalf("second Solve: %v", err)
	}
	if !assignEqual(got.Assign, got2.Assign) || got.MaxLoad.Cmp(got2.MaxLoad) != 0 {
		t.Fatalf("solver is not deterministic")
	}
}

// ---- 8. validation failures ----------------------------------------------

func TestValidation(t *testing.T) {
	cases := []struct {
		name string
		p    *Problem
	}{
		{"no partitions", &Problem{Consumers: consumers([2]string{"dX", "1"}), InFlightCap: 1}},
		{"too many partitions", sixteenPlusProblem()},
		{"zero weight", &Problem{
			Partitions: []Partition{
				partition(0, 0, "F", "dX", 0, makeSubs(1, 0)),
			},
			Consumers:   consumers([2]string{"dX", "1"}),
			InFlightCap: 1,
		}},
		{"no subscriber", &Problem{
			Partitions: []Partition{
				partition(0, 1, "F", "dX", 0, makeSubs(1)),
			},
			Consumers:   consumers([2]string{"dX", "1"}),
			InFlightCap: 1,
		}},
	}
	for _, tc := range cases {
		if err := tc.p.Validate(); !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("%s: want ErrInvalidInput, got %v", tc.name, err)
		}
	}

	// Family mirror collision: two F mirrors in the only domain.
	bad := &Problem{
		Partitions: []Partition{
			partition(0, 1, "F", "dX", 0, makeSubs(1, 0)),
			partition(1, 1, "F", "dX", 0, makeSubs(1, 0)),
		},
		Consumers:   consumers([2]string{"dX", "1"}),
		InFlightCap: 1,
	}
	if _, err := bad.Solve(); !errors.Is(err, ErrInfeasible) {
		t.Fatalf("mirror collision: want ErrInfeasible, got %v", err)
	}
}

func sixteenPlusProblem() *Problem {
	ps := make([]Partition, MaxPartitions+1)
	for i := range ps {
		ps[i] = partition(i, 1, "F", "dX", 0, makeSubs(1, 0))
	}
	return &Problem{
		Partitions:  ps,
		Consumers:   consumers([2]string{"dX", "100"}),
		InFlightCap: 1,
	}
}

// ---- helpers -------------------------------------------------------------

func assignEqual(a, b Assignment) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func byteSliceEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func intSliceEqual(a, b []int) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func assertFamilyDomains(t *testing.T, p *Problem, a Assignment) {
	t.Helper()
	seen := map[string]map[string]bool{}
	for i, c := range a {
		fam := p.Partitions[i].Family
		dom := p.Consumers[c].Domain
		if seen[fam] == nil {
			seen[fam] = map[string]bool{}
		}
		if seen[fam][dom] {
			t.Fatalf("family %s assigned twice into domain %s", fam, dom)
		}
		seen[fam][dom] = true
	}
}
