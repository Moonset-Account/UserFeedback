package forwarder

import (
	"errors"
	"fmt"
	"reflect"
	"sort"
	"sync"
	"testing"
)

const (
	alpha   = "alpha"
	bravo   = "bravo"
	charlie = "charlie"
)

func mustNew(t *testing.T, cfg Config, nodes []Node) *Ring {
	t.Helper()
	r, err := New(cfg, nodes)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return r
}

// pickID finds an unused connection ID whose preferred bucket is want.  It
// keeps FNV details out of the tests while still exercising the real hash.
func pickID(t *testing.T, r *Ring, used map[string]bool, want int) string {
	t.Helper()
	for i := 0; i < 5_000_000; i++ {
		id := fmt.Sprintf("conn-%06d", i)
		if used[id] {
			continue
		}
		if r.BucketFor(id) == want {
			used[id] = true
			return id
		}
	}
	t.Fatalf("no connection id hashes to bucket %d", want)
	return ""
}

func mustConnectAt(t *testing.T, r *Ring, id string, wantBucket int) *Conn {
	t.Helper()
	c, err := r.Connect(id)
	if err != nil {
		t.Fatalf("Connect(%q): %v", id, err)
	}
	if c.Bucket() != wantBucket {
		t.Fatalf("Connect(%q) landed on bucket %d, want %d", id, c.Bucket(), wantBucket)
	}
	return c
}

func statOf(t *testing.T, round Round, id string) NodeStat {
	t.Helper()
	for _, s := range round.Nodes {
		if s.ID == id {
			return s
		}
	}
	t.Fatalf("node %q missing from round stats", id)
	return NodeStat{}
}

func assertMoves(t *testing.T, got, want []Move) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("moves mismatch\n got: %#v\nwant: %#v", got, want)
	}
}

func assertBuckets(t *testing.T, got, want []string) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("bucket map mismatch\n got: %v\nwant: %v", got, want)
	}
}

func connCounts(s Snapshot) map[string]int {
	m := map[string]int{}
	for _, n := range s.Nodes {
		m[n.ID] = n.Connections
	}
	return m
}

// TestInitialQuotas checks the integer quota maths, including the
// largest-remainder step and its ID tie-break.
func TestInitialQuotas(t *testing.T) {
	r := mustNew(t, Config{Buckets: 12, MaxMovesPerRound: 2}, []Node{
		{ID: alpha, Weight: 6, Health: Ready},
		{ID: bravo, Weight: 3, Health: Ready},
		{ID: charlie, Weight: 3, Health: Ready},
	})
	assertBuckets(t, r.Snapshot().Buckets, []string{
		alpha, alpha, alpha, alpha, alpha, alpha,
		bravo, bravo, bravo,
		charlie, charlie, charlie,
	})

	// 4/3 leaves one bucket over; it goes to the smallest ID.
	r = mustNew(t, Config{Buckets: 4, MaxMovesPerRound: 1}, []Node{
		{ID: "a", Weight: 1, Health: Ready},
		{ID: "b", Weight: 1, Health: Ready},
		{ID: "c", Weight: 1, Health: Ready},
	})
	assertBuckets(t, r.Snapshot().Buckets, []string{"a", "a", "b", "c"})

	// Exact division leaves no remainder.
	r = mustNew(t, Config{Buckets: 9, MaxMovesPerRound: 1}, []Node{
		{ID: "a", Weight: 2, Health: Ready},
		{ID: "b", Weight: 3, Health: Ready},
		{ID: "c", Weight: 4, Health: Ready},
	})
	assertBuckets(t, r.Snapshot().Buckets, []string{
		"a", "a", "b", "b", "b", "c", "c", "c", "c",
	})
	for _, s := range r.Snapshot().Nodes {
		if s.Buckets != s.Quota {
			t.Fatalf("node %s buckets=%d quota=%d after bootstrap", s.ID, s.Buckets, s.Quota)
		}
	}
}

// TestDestinationTieBreakByID pins the documented tie-break: when two
// destinations are equally under quota, the lexicographically smaller ID
// wins, and the lowest over-quota bucket is the source.
func TestDestinationTieBreakByID(t *testing.T) {
	r := mustNew(t, Config{Buckets: 9, MaxMovesPerRound: 1}, []Node{
		{ID: "a", Weight: 2, Health: Ready},
		{ID: "b", Weight: 3, Health: Ready},
		{ID: "c", Weight: 4, Health: Ready},
	})
	// Drop a to zero: quotas become a=0, b=4, c=5 and both b and c are under.
	if err := r.SetWeight("a", 0); err != nil {
		t.Fatal(err)
	}
	rnd := r.Rebalance()
	// Both b and c are one short of quota; b wins on ID.  Bucket 0 is the
	// lowest-indexed bucket owned by the over-quota node a.
	assertMoves(t, rnd.Moves, []Move{{Bucket: 0, From: "a", To: "b"}})
	if got := statOf(t, rnd, "a").Quota; got != 0 {
		t.Fatalf("a quota = %d, want 0", got)
	}
	if got := statOf(t, rnd, "b").Quota; got != 4 {
		t.Fatalf("b quota = %d, want 4", got)
	}
	if got := statOf(t, rnd, "c").Quota; got != 5 {
		t.Fatalf("c quota = %d, want 5", got)
	}
}

// TestDownweightThenFailureSequence is the required event sequence: a
// downweight that has not converged, then a hard failure.  It asserts the
// exact bucket map after every round and exactly which connections rerouted.
func TestDownweightThenFailureSequence(t *testing.T) {
	r := mustNew(t, Config{Buckets: 12, MaxMovesPerRound: 1}, []Node{
		{ID: alpha, Weight: 6, Health: Ready},
		{ID: bravo, Weight: 3, Health: Ready},
		{ID: charlie, Weight: 3, Health: Ready},
	})
	used := map[string]bool{}
	type role struct {
		id  string
		old *Conn
	}
	roles := map[string]*role{}
	add := func(name string, bucket int) {
		id := pickID(t, r, used, bucket)
		roles[name] = &role{id: id, old: mustConnectAt(t, r, id, bucket)}
	}
	add("a1", 0)
	add("a2", 1)
	add("a3", 5)
	add("b1", 6)
	add("b2", 8)
	add("c1", 9)
	add("c2", 10)
	add("c3", 11)

	// --- Round 1: alpha 6 -> 1, no convergence yet ----------------------
	if err := r.SetWeight(alpha, 1); err != nil {
		t.Fatal(err)
	}
	r1 := r.Rebalance()
	if r1.Number != 1 {
		t.Fatalf("round number = %d, want 1", r1.Number)
	}
	assertMoves(t, r1.Moves, []Move{{Bucket: 0, From: alpha, To: bravo}})
	assertBuckets(t, r1.Buckets, []string{
		bravo, alpha, alpha, alpha, alpha, alpha,
		bravo, bravo, bravo, charlie, charlie, charlie,
	})
	if len(r1.Rerouted) != 0 {
		t.Fatalf("round 1 rerouted %d connections, want 0", len(r1.Rerouted))
	}
	if r1.Deviation != 6 {
		t.Fatalf("round 1 deviation = %d, want 6", r1.Deviation)
	}
	// Bucket 0 moved to bravo, but a1 was bound earlier and must stay put.
	if got := roles["a1"].old.Target(); got != alpha {
		t.Fatalf("a1 target after bucket move = %q, want %q", got, alpha)
	}
	if got := r1.Buckets[0]; got != bravo {
		t.Fatalf("bucket 0 = %q, want %q", got, bravo)
	}

	// --- Event: charlie fails while reweighting is still unconverged ----
	if err := r.SetHealth(charlie, Failed); err != nil {
		t.Fatal(err)
	}
	r2 := r.Rebalance()

	// All three charlie connections must be rerouted to the nearest ready
	// bucket: bucket 0 (bravo), because buckets 9-11 are still stale.
	wantReroutes := []string{roles["c1"].id, roles["c2"].id, roles["c3"].id}
	sort.Strings(wantReroutes)
	if len(r2.Rerouted) != len(wantReroutes) {
		t.Fatalf("round 2 rerouted %d, want %d: %#v", len(r2.Rerouted), len(wantReroutes), r2.Rerouted)
	}
	for i, rr := range r2.Rerouted {
		if rr.ConnID != wantReroutes[i] {
			t.Fatalf("reroute[%d] id = %q, want %q", i, rr.ConnID, wantReroutes[i])
		}
		if rr.From != charlie || rr.To != bravo || rr.Bucket != 0 || rr.Stranded {
			t.Fatalf("reroute[%d] = %#v, want charlie->bravo bucket 0", i, rr)
		}
	}
	assertMoves(t, r2.Moves, []Move{{Bucket: 1, From: alpha, To: bravo}})
	assertBuckets(t, r2.Buckets, []string{
		bravo, bravo, alpha, alpha, alpha, alpha,
		bravo, bravo, bravo, charlie, charlie, charlie,
	})
	if r2.Deviation != 8 {
		t.Fatalf("round 2 deviation = %d, want 8", r2.Deviation)
	}

	// Old handles are frozen tombstones; live handles point at bravo.
	for _, name := range []string{"c1", "c2", "c3"} {
		old := roles[name].old
		if !old.Stale() {
			t.Fatalf("%s old handle not marked stale", name)
		}
		if old.Target() != charlie {
			t.Fatalf("%s old handle target = %q, want frozen charlie", name, old.Target())
		}
		live, ok := r.Conn(roles[name].id)
		if !ok {
			t.Fatalf("%s live connection missing", name)
		}
		if live == old {
			t.Fatalf("%s live handle is the stale handle", name)
		}
		if live.Target() != bravo || live.Bucket() != 0 {
			t.Fatalf("%s live = (%q,%d), want (bravo,0)", name, live.Target(), live.Bucket())
		}
	}
	if got := connCounts(r.Snapshot()); got[alpha] != 3 || got[bravo] != 5 || got[charlie] != 0 {
		t.Fatalf("connection counts after round 2 = %v, want alpha 3 bravo 5 charlie 0", got)
	}

	// --- Rounds 3..6 converge, one bucket per round ---------------------
	type step struct {
		move    Move
		buckets []string
		dev     int
	}
	steps := []step{
		{
			Move{Bucket: 2, From: alpha, To: bravo},
			[]string{bravo, bravo, bravo, alpha, alpha, alpha, bravo, bravo, bravo, charlie, charlie, charlie},
			6,
		},
		{
			Move{Bucket: 9, From: charlie, To: bravo},
			[]string{bravo, bravo, bravo, alpha, alpha, alpha, bravo, bravo, bravo, bravo, charlie, charlie},
			4,
		},
		{
			Move{Bucket: 10, From: charlie, To: bravo},
			[]string{bravo, bravo, bravo, alpha, alpha, alpha, bravo, bravo, bravo, bravo, bravo, charlie},
			2,
		},
		{
			Move{Bucket: 11, From: charlie, To: bravo},
			[]string{bravo, bravo, bravo, alpha, alpha, alpha, bravo, bravo, bravo, bravo, bravo, bravo},
			0,
		},
	}
	for i, s := range steps {
		rnd := r.Rebalance()
		if rnd.Number != i+3 {
			t.Fatalf("round number = %d, want %d", rnd.Number, i+3)
		}
		assertMoves(t, rnd.Moves, []Move{s.move})
		assertBuckets(t, rnd.Buckets, s.buckets)
		if rnd.Deviation != s.dev {
			t.Fatalf("round %d deviation = %d, want %d", rnd.Number, rnd.Deviation, s.dev)
		}
		if len(rnd.Rerouted) != 0 {
			t.Fatalf("round %d rerouted %d connections, want 0", rnd.Number, len(rnd.Rerouted))
		}
	}

	// Another round is a no-op once the map matches the quotas.
	r7 := r.Rebalance()
	if len(r7.Moves) != 0 || r7.Deviation != 0 || r7.Stalled {
		t.Fatalf("converged round = %#v, want no moves, deviation 0, not stalled", r7)
	}
	final := r.Snapshot()
	for _, s := range final.Nodes {
		if s.Buckets != s.Quota || s.Difference != 0 {
			t.Fatalf("node %s final buckets=%d quota=%d diff=%d", s.ID, s.Buckets, s.Quota, s.Difference)
		}
	}

	// --- Stale close must not decrement the new target's count ----------
	live, _ := r.Conn(roles["c1"].id)
	if live.Target() != bravo {
		t.Fatalf("c1 live target = %q, want bravo", live.Target())
	}
	roles["c1"].old.Close() // stale handle: must be a no-op
	if got := connCounts(r.Snapshot()); got[bravo] != 5 {
		t.Fatalf("bravo connections after stale close = %d, want 5", got[bravo])
	}
	if _, ok := r.Conn(roles["c1"].id); !ok {
		t.Fatal("c1 disappeared after stale close")
	}
	live.Close()
	if got := connCounts(r.Snapshot()); got[bravo] != 4 {
		t.Fatalf("bravo connections after live close = %d, want 4", got[bravo])
	}
	if _, ok := r.Conn(roles["c1"].id); ok {
		t.Fatal("c1 still present after live close")
	}
}

// TestDrainingKeepsConnectionsButMovesBuckets covers drain semantics: old
// connections stay, stale buckets accept nothing new, buckets drift away.
func TestDrainingKeepsConnectionsButMovesBuckets(t *testing.T) {
	r := mustNew(t, Config{Buckets: 8, MaxMovesPerRound: 2}, []Node{
		{ID: alpha, Weight: 4, Health: Ready},
		{ID: bravo, Weight: 4, Health: Ready},
	})
	used := map[string]bool{}
	d1 := mustConnectAt(t, r, pickID(t, r, used, 0), 0) // alpha
	d2 := mustConnectAt(t, r, pickID(t, r, used, 4), 4) // bravo

	if err := r.SetHealth(alpha, Draining); err != nil {
		t.Fatal(err)
	}
	// A brand new connection whose preferred bucket is still owned by the
	// draining node must not land there; it scans on to bucket 4.
	probeID := pickID(t, r, used, 2)
	probe := mustConnectAt(t, r, probeID, 4)
	if probe.Target() != bravo {
		t.Fatalf("probe target = %q, want bravo", probe.Target())
	}

	r1 := r.Rebalance()
	if len(r1.Rerouted) != 0 {
		t.Fatalf("draining must not reroute, got %#v", r1.Rerouted)
	}
	assertMoves(t, r1.Moves, []Move{
		{Bucket: 0, From: alpha, To: bravo},
		{Bucket: 1, From: alpha, To: bravo},
	})
	assertBuckets(t, r1.Buckets, []string{bravo, bravo, alpha, alpha, bravo, bravo, bravo, bravo})
	if d1.Target() != alpha {
		t.Fatalf("draining connection target = %q, want alpha", d1.Target())
	}

	r2 := r.Rebalance()
	assertMoves(t, r2.Moves, []Move{
		{Bucket: 2, From: alpha, To: bravo},
		{Bucket: 3, From: alpha, To: bravo},
	})
	assertBuckets(t, r2.Buckets, []string{bravo, bravo, bravo, bravo, bravo, bravo, bravo, bravo})
	if r2.Deviation != 0 {
		t.Fatalf("deviation = %d, want 0", r2.Deviation)
	}
	// d1 still rides alpha; its bucket moved away but the connection did not.
	if d1.Target() != alpha || d1.Bucket() != 0 {
		t.Fatalf("d1 after drain = (%q,%d), want (alpha,0)", d1.Target(), d1.Bucket())
	}
	if d2.Target() != bravo {
		t.Fatalf("d2 target = %q, want bravo", d2.Target())
	}
	counts := connCounts(r.Snapshot())
	if counts[alpha] != 1 || counts[bravo] != 2 {
		t.Fatalf("connection counts = %v, want alpha 1 bravo 2", counts)
	}
}

// TestFailedBucketRejectsNewConnection proves new connections never bind to a
// bucket that still points at a failed node.
func TestFailedBucketRejectsNewConnection(t *testing.T) {
	r := mustNew(t, Config{Buckets: 6, MaxMovesPerRound: 1}, []Node{
		{ID: alpha, Weight: 1, Health: Ready},
		{ID: bravo, Weight: 1, Health: Ready},
	})
	used := map[string]bool{}
	first := mustConnectAt(t, r, pickID(t, r, used, 1), 1) // alpha
	if err := r.SetHealth(alpha, Failed); err != nil {
		t.Fatal(err)
	}
	// Preferred bucket 0 still points at failed alpha; the new connection
	// must scan forward to bucket 3 (first bravo bucket).
	probe := mustConnectAt(t, r, pickID(t, r, used, 0), 3)
	if probe.Target() != bravo {
		t.Fatalf("probe target = %q, want bravo", probe.Target())
	}
	rnd := r.Rebalance()
	if len(rnd.Rerouted) != 1 || rnd.Rerouted[0].ConnID != first.ID() {
		t.Fatalf("rerouted = %#v, want just %q", rnd.Rerouted, first.ID())
	}
	if !first.Stale() {
		t.Fatalf("old handle for %q should be stale after reroute", first.ID())
	}
	live, ok := r.Conn(first.ID())
	if !ok || live.Target() != bravo {
		t.Fatalf("live handle = %#v ok=%v, want bravo", live, ok)
	}
	if live == first {
		t.Fatal("live handle must not be the stale handle")
	}
}

// TestStrandedFailure covers the case where no ready bucket exists.
func TestStrandedFailure(t *testing.T) {
	r := mustNew(t, Config{Buckets: 4, MaxMovesPerRound: 2}, []Node{
		{ID: alpha, Weight: 1, Health: Ready},
		{ID: bravo, Weight: 1, Health: Ready},
	})
	c, err := r.Connect(pickID(t, r, map[string]bool{}, 0))
	if err != nil {
		t.Fatal(err)
	}
	if err := r.SetHealth(alpha, Failed); err != nil {
		t.Fatal(err)
	}
	if err := r.SetHealth(bravo, Failed); err != nil {
		t.Fatal(err)
	}
	rnd := r.Rebalance()
	if len(rnd.Rerouted) != 1 {
		t.Fatalf("rerouted = %#v, want 1 stranded entry", rnd.Rerouted)
	}
	if rr := rnd.Rerouted[0]; !rr.Stranded || rr.Bucket != -1 || rr.To != "" || rr.ConnID != c.ID() {
		t.Fatalf("stranded reroute = %#v", rr)
	}
	if !rnd.Stalled || rnd.Deviation == 0 {
		t.Fatalf("round = %#v, want stalled with residual deviation", rnd)
	}
	if _, err := r.Connect("late"); !errors.Is(err, ErrNoReadyTarget) {
		t.Fatalf("Connect with no ready node = %v, want ErrNoReadyTarget", err)
	}
	// The stranded connection is retried, not silently dropped.
	if _, ok := r.Conn(c.ID()); !ok {
		t.Fatal("stranded connection was dropped")
	}
}

// TestGreedyMonotonicProperty checks that every move lowers the deviation by
// exactly two and that the budget is never exceeded, over a few weight mixes.
func TestGreedyMonotonicProperty(t *testing.T) {
	cases := []struct {
		buckets int
		max     int
		nodes   []Node
	}{
		{12, 2, []Node{{ID: "a", Weight: 5, Health: Ready}, {ID: "b", Weight: 4, Health: Ready}, {ID: "c", Weight: 3, Health: Ready}}},
		{10, 3, []Node{{ID: "a", Weight: 7, Health: Ready}, {ID: "b", Weight: 2, Health: Ready}, {ID: "c", Weight: 1, Health: Ready}}},
		{7, 1, []Node{{ID: "a", Weight: 1, Health: Ready}, {ID: "b", Weight: 1, Health: Ready}, {ID: "c", Weight: 1, Health: Ready}}},
	}
	for ci, tc := range cases {
		r := mustNew(t, Config{Buckets: tc.buckets, MaxMovesPerRound: tc.max}, tc.nodes)
		prev := r.Snapshot()
		prevDev := 0
		for _, s := range prev.Nodes {
			prevDev += abs(s.Difference)
		}
		converged := false
		for round := 0; round < 20; round++ {
			rnd := r.Rebalance()
			if len(rnd.Moves) > tc.max {
				t.Fatalf("case %d: round moved %d buckets, cap %d", ci, len(rnd.Moves), tc.max)
			}
			if want := prevDev - 2*len(rnd.Moves); rnd.Deviation != want {
				t.Fatalf("case %d: deviation %d, want %d (prev %d, moves %d)", ci, rnd.Deviation, want, prevDev, len(rnd.Moves))
			}
			prevDev = rnd.Deviation
			if prevDev == 0 {
				converged = true
				break
			}
		}
		if !converged {
			t.Fatalf("case %d (%+v) did not converge", ci, tc)
		}
	}
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

// TestValidationErrors checks the guard rails on inputs.
func TestValidationErrors(t *testing.T) {
	good := []Node{{ID: "a", Weight: 1, Health: Ready}}
	if _, err := New(Config{Buckets: 0, MaxMovesPerRound: 1}, good); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("buckets 0: %v", err)
	}
	if _, err := New(Config{Buckets: 4, MaxMovesPerRound: 0}, good); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("maxMoves 0: %v", err)
	}
	if _, err := New(Config{Buckets: 4, MaxMovesPerRound: 1}, nil); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("empty nodes: %v", err)
	}
	if _, err := New(Config{Buckets: 4, MaxMovesPerRound: 1}, []Node{
		{ID: "a", Weight: 1, Health: Ready}, {ID: "a", Weight: 1, Health: Ready},
	}); !errors.Is(err, ErrDuplicateNode) {
		t.Fatalf("dup node: %v", err)
	}
	if _, err := New(Config{Buckets: 4, MaxMovesPerRound: 1}, []Node{{ID: "a", Weight: -1, Health: Ready}}); !errors.Is(err, ErrInvalidWeight) {
		t.Fatalf("negative weight: %v", err)
	}
	if _, err := New(Config{Buckets: 4, MaxMovesPerRound: 1}, []Node{{ID: "a", Weight: 1, Health: Health(9)}}); !errors.Is(err, ErrInvalidHealth) {
		t.Fatalf("bad health: %v", err)
	}
	if _, err := New(Config{Buckets: 4, MaxMovesPerRound: 1}, []Node{{ID: "a", Weight: 0, Health: Draining}}); !errors.Is(err, ErrNoEligibleTarget) {
		t.Fatalf("no eligible: %v", err)
	}

	r := mustNew(t, Config{Buckets: 4, MaxMovesPerRound: 1}, good)
	if err := r.SetWeight("nope", 1); !errors.Is(err, ErrUnknownNode) {
		t.Fatalf("unknown weight: %v", err)
	}
	if err := r.SetWeight("a", -1); !errors.Is(err, ErrInvalidWeight) {
		t.Fatalf("bad weight: %v", err)
	}
	if err := r.SetHealth("nope", Ready); !errors.Is(err, ErrUnknownNode) {
		t.Fatalf("unknown health: %v", err)
	}
	if err := r.SetHealth("a", Health(9)); !errors.Is(err, ErrInvalidHealth) {
		t.Fatalf("bad health set: %v", err)
	}
	if err := r.AddNode(Node{ID: "a", Weight: 1, Health: Ready}); !errors.Is(err, ErrDuplicateNode) {
		t.Fatalf("dup add: %v", err)
	}
	if err := r.AddNode(Node{Weight: 1, Health: Ready}); !errors.Is(err, ErrInvalidNode) {
		t.Fatalf("empty id add: %v", err)
	}
	if _, err := r.Connect(""); !errors.Is(err, ErrInvalidConnID) {
		t.Fatalf("empty conn id: %v", err)
	}
	c, err := r.Connect("x")
	if err != nil {
		t.Fatalf("connect x: %v", err)
	}
	if _, err := r.Connect("x"); !errors.Is(err, ErrConnectionExists) {
		t.Fatalf("dup conn: %v", err)
	}
	c.Close()
	c.Close() // idempotent
}

// TestAddNodeMovesBucketsIn covers a new node joining, then receiving quota.
// With 6 buckets and three equal weights the target is 2/2/2.  alpha (owning
// buckets 0-2) gives bucket 0 away, which balances alpha at once; the second
// move of the round then has to come from bravo, bucket 3.  Both destinations
// are charlie, the only node under quota.
func TestAddNodeMovesBucketsIn(t *testing.T) {
	r := mustNew(t, Config{Buckets: 6, MaxMovesPerRound: 2}, []Node{
		{ID: alpha, Weight: 1, Health: Ready},
		{ID: bravo, Weight: 1, Health: Ready},
	})
	assertBuckets(t, r.Snapshot().Buckets, []string{alpha, alpha, alpha, bravo, bravo, bravo})
	if err := r.AddNode(Node{ID: charlie, Weight: 1, Health: Ready}); err != nil {
		t.Fatal(err)
	}
	r1 := r.Rebalance()
	assertMoves(t, r1.Moves, []Move{
		{Bucket: 0, From: alpha, To: charlie},
		{Bucket: 3, From: bravo, To: charlie},
	})
	assertBuckets(t, r1.Buckets, []string{charlie, alpha, alpha, charlie, bravo, bravo})
	if r1.Deviation != 0 {
		t.Fatalf("deviation = %d, want 0", r1.Deviation)
	}
	r2 := r.Rebalance()
	if len(r2.Moves) != 0 || r2.Deviation != 0 {
		t.Fatalf("converged round = %#v, want no moves", r2)
	}
}

// TestConcurrentConnectCloseRebalance is the interleaving test: connects,
// closes, health/weight churn and rebalances race under -race.  alpha stays
// Ready so reroutes always have somewhere to go.
func TestConcurrentConnectCloseRebalance(t *testing.T) {
	r := mustNew(t, Config{Buckets: 64, MaxMovesPerRound: 8}, []Node{
		{ID: alpha, Weight: 4, Health: Ready},
		{ID: bravo, Weight: 3, Health: Ready},
		{ID: charlie, Weight: 3, Health: Ready},
	})

	var workers sync.WaitGroup
	var controller sync.WaitGroup
	stop := make(chan struct{})

	// Churn health/weights and rebalance until the workers are done.
	controller.Add(1)
	go func() {
		defer controller.Done()
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			default:
			}
			h := Health(i % 3) // Ready, Draining, Failed
			_ = r.SetHealth(bravo, h)
			_ = r.SetHealth(charlie, h)
			_ = r.SetWeight(charlie, 1+i%5)
			r.Rebalance()
		}
	}()

	// Many connection churners.
	const workersN = 8
	for w := 0; w < workersN; w++ {
		workers.Add(1)
		go func(w int) {
			defer workers.Done()
			for i := 0; i < 300; i++ {
				id := fmt.Sprintf("w%d-%d", w, i)
				c, err := r.Connect(id)
				if err != nil {
					continue // no ready target in that instant
				}
				if i%2 == 0 {
					c.Close()
					continue
				}
				// Sometimes close a handle that a concurrent reroute may have
				// superseded; that Close must not disturb the new target.
				_ = r.Rebalance()
				c.Close()
			}
		}(w)
	}

	workers.Wait()
	close(stop)
	controller.Wait()

	final := r.Snapshot()
	sum := 0
	for _, n := range final.Nodes {
		sum += n.Connections
	}
	if sum != len(final.Connections) {
		t.Fatalf("per-node connection sum %d != live list %d", sum, len(final.Connections))
	}
	for _, c := range final.Connections {
		if c.Bucket < 0 || c.Bucket >= len(final.Buckets) {
			t.Fatalf("conn %s bucket %d out of range", c.ID, c.Bucket)
		}
	}
	// Every bucket must name a known node throughout.
	for i, id := range final.Buckets {
		found := false
		for _, n := range final.Nodes {
			if n.ID == id {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("bucket %d names unknown node %q", i, id)
		}
	}

	// Settle the ring and drain every live connection through its live handle.
	// If any stale Close had corrupted the counters, this final bookkeeping
	// check would catch it: after closing everything, counts must all be zero.
	for _, id := range []string{alpha, bravo, charlie} {
		if err := r.SetHealth(id, Ready); err != nil {
			t.Fatal(err)
		}
	}
	r.Rebalance()
	conns := r.Connections()
	for _, ci := range conns {
		live, ok := r.Conn(ci.ID)
		if !ok {
			t.Fatalf("conn %s listed but not retrievable", ci.ID)
		}
		live.Close()
	}
	after := r.Snapshot()
	if len(after.Connections) != 0 {
		t.Fatalf("%d connections survived close-all", len(after.Connections))
	}
	for _, n := range after.Nodes {
		if n.Connections != 0 {
			t.Fatalf("node %s still holds %d connections after close-all", n.ID, n.Connections)
		}
	}
}

// TestDeterministicReplay asserts the same inputs yield identical maps.
func TestDeterministicReplay(t *testing.T) {
	run := func() Snapshot {
		r := mustNew(t, Config{Buckets: 16, MaxMovesPerRound: 3}, []Node{
			{ID: alpha, Weight: 7, Health: Ready},
			{ID: bravo, Weight: 5, Health: Ready},
			{ID: charlie, Weight: 4, Health: Ready},
		})
		_ = r.SetWeight(alpha, 2)
		for i := 0; i < 4; i++ {
			r.Rebalance()
		}
		_ = r.SetHealth(charlie, Failed)
		for i := 0; i < 6; i++ {
			r.Rebalance()
		}
		return r.Snapshot()
	}
	a, b := run(), run()
	if !reflect.DeepEqual(a.Buckets, b.Buckets) {
		t.Fatalf("replay diverged\n a=%v\n b=%v", a.Buckets, b.Buckets)
	}
	for i := range a.Nodes {
		if a.Nodes[i] != b.Nodes[i] {
			t.Fatalf("node stat %d diverged: %+v vs %+v", i, a.Nodes[i], b.Nodes[i])
		}
	}
}
