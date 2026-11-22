package ssiarbiter

import (
	"fmt"
	"sync"
	"testing"
)

// runThreeTxnCycle replays the benchmark history: three transactions read
// disjoint primary keys and each installs a row on the key read by the next
// transaction. Only anti-dependency edges are possible, and they close a ring.
func runThreeTxnCycle(interleaveGC bool) (Decision, []Edge) {
	a := New(nil)
	if err := a.Begin("t1", 0); err != nil {
		panic(err)
	}
	if err := a.Begin("t2", 0); err != nil {
		panic(err)
	}
	if err := a.Begin("t3", 0); err != nil {
		panic(err)
	}

	// All three reads are empty reads and must be recorded.
	for _, key := range []string{"k1", "k2", "k3"} {
		var who TxnID
		switch key {
		case "k1":
			who = "t1"
		case "k2":
			who = "t2"
		case "k3":
			who = "t3"
		}
		got, err := a.Read(ReadSpec{TxnID: who, StartKey: key, Snapshot: 0})
		if err != nil {
			panic(err)
		}
		if len(got) != 0 {
			panic("expected empty read on " + key)
		}
	}

	// t1 fills the slot t2 read empty: t2 -> t1 anti-dependency.
	d1, err := a.Commit("t1", []WriteIntent{
		{Key: "k3", Before: nil, After: Row{"v": "1"}},
	})
	if err != nil {
		panic(err)
	}
	if !d1.Committed {
		panic("t1 must commit")
	}

	if interleaveGC {
		// All active readers hold snapshot 0, so nothing below it is dropped.
		if stats := a.Collect(); stats.ReclaimedTxns != 0 {
			panic("no finished txn should be below the oldest snapshot")
		}
	}

	// t2 fills the slot t3 read empty: t3 -> t2 anti-dependency.
	d2, err := a.Commit("t2", []WriteIntent{
		{Key: "k1", Before: nil, After: Row{"v": "2"}},
	})
	if err != nil {
		panic(err)
	}
	if !d2.Committed {
		panic("t2 must commit")
	}

	// t3 fills the slot t1 read empty; with t1 -> t3 the ring closes.
	d3, err := a.Commit("t3", []WriteIntent{
		{Key: "k2", Before: nil, After: Row{"v": "3"}},
	})
	if err != nil {
		panic(err)
	}
	return d3, a.Edges()
}

// TestThreeDisjointKeysCloseRing is the required acceptance history: no two
// transactions write the same primary key, yet the anti-dependencies form a
// closed cycle and the last candidate is rejected with a full witness.
func TestThreeDisjointKeysCloseRing(t *testing.T) {
	decision, edges := runThreeTxnCycle(false)

	if decision.Committed {
		t.Fatal("expected the third commit to be rejected")
	}
	if len(decision.Cycle) != 3 {
		t.Fatalf("expected a 3-edge witness cycle, got %d: %v", len(decision.Cycle), decision.Cycle)
	}
	if decision.Cycle[0].From != "t3" || decision.Cycle[len(decision.Cycle)-1].To != "t3" {
		t.Fatalf("witness must start and end at the rejected txn t3, got %v", decision.Cycle)
	}
	// The ring is: t3 -> t1 (t3's empty read of k2, filled by t1),
	// t1 -> t2 (t1's empty read of k3, filled by t2),
	// t2 -> t3 (t2's empty read of k1, filled by t3). DFS starts at t3 and
	// follows adjacency insertion order.
	wantRing := map[string]struct {
		from, to TxnID
		key      string
	}{
		"t3->t1": {"t3", "t1", "k3"},
		"t1->t2": {"t1", "t2", "k1"},
		"t2->t3": {"t2", "t3", "k2"},
	}
	seen := map[string]bool{}
	for _, e := range decision.Cycle {
		if e.Kind != DepAnti {
			t.Fatalf("all edges in this ring must be anti-dependencies, got %s", e)
		}
		for name, want := range wantRing {
			if e.From == want.from && e.To == want.to {
				if e.Scope != "key="+want.key {
					t.Fatalf("edge %s carries wrong scope %q, want key=%s", e, e.Scope, want.key)
				}
				seen[name] = true
			}
		}
	}
	if len(seen) != 3 {
		t.Fatalf("witness does not contain the full ring, got %v", decision.Cycle)
	}

	antiCount := 0
	for _, e := range edges {
		if e.Kind == DepAnti {
			antiCount++
		}
	}
	if antiCount != 2 {
		t.Fatalf("rejected candidate edges must not be installed, found %d anti edges", antiCount)
	}
}

// TestUnrelatedColumnNoConflict is the required control: a write that only
// changes a column outside the read's projection does not change the query
// result, so no anti-dependency and no rejection.
func TestUnrelatedColumnNoConflict(t *testing.T) {
	a := New(map[string]Row{
		"p1": {"id": "p1", "city": "BJ", "comment": "old"},
		"p2": {"id": "p2", "city": "BJ", "comment": "old"},
	})
	if err := a.Begin("reader", 0); err != nil {
		t.Fatal(err)
	}
	got, err := a.Read(ReadSpec{
		TxnID:      "reader",
		StartKey:   "p1",
		EndKey:     "p2",
		Predicate:  map[string]string{"city": "BJ"},
		Projection: []string{"id", "city"},
		Snapshot:   0,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got["p1"]["comment"] != "" {
		t.Fatalf("projection must hide the comment column, got %v", got)
	}

	if err := a.Begin("writer", 0); err != nil {
		t.Fatal(err)
	}
	// Only the non-projected, non-predicate column changes: identical result.
	decision, err := a.Commit("writer", []WriteIntent{{
		Key:    "p1",
		Before: Row{"id": "p1", "city": "BJ", "comment": "old"},
		After:  Row{"id": "p1", "city": "BJ", "comment": "new"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !decision.Committed {
		t.Fatalf("unrelated-column write must commit, witness=%v", decision.Cycle)
	}
	for _, e := range a.Edges() {
		if e.Kind == DepAnti {
			t.Fatalf("no anti-dependency allowed for an invisible column change, got %s", e)
		}
	}

	// Sanity counter-check: changing a projected column does conflict.
	if err := a.Begin("writer2", 1); err != nil {
		t.Fatal(err)
	}
	d2, err := a.Commit("writer2", []WriteIntent{{
		Key:    "p2",
		Before: Row{"id": "p2", "city": "BJ", "comment": "old"},
		After:  Row{"id": "p2", "city": "SH", "comment": "old"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !d2.Committed {
		t.Fatalf("a two-edge history without ring still commits, got %v", d2.Cycle)
	}
	found := false
	for _, e := range a.Edges() {
		if e.Kind == DepAnti && e.From == "reader" && e.To == "writer2" {
			found = true
		}
	}
	if !found {
		t.Fatal("changing a projected column must create an anti-dependency")
	}
}

// TestWriteWritePlusAntiDependencyTwoNodeCycle covers the WW ordering
// constraint combined with an anti-dependency on the same key.
func TestWriteWritePlusAntiDependencyTwoNodeCycle(t *testing.T) {
	a := New(map[string]Row{"k": {"v": "0"}})
	if err := a.Begin("t1", 0); err != nil {
		t.Fatal(err)
	}
	if err := a.Begin("t2", 0); err != nil {
		t.Fatal(err)
	}

	// t2 reads the original row while t1 will overwrite it.
	if _, err := a.Read(ReadSpec{TxnID: "t2", StartKey: "k", Snapshot: 0}); err != nil {
		t.Fatal(err)
	}
	d1, err := a.Commit("t1", []WriteIntent{{
		Key:    "k",
		Before: Row{"v": "0"},
		After:  Row{"v": "1"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !d1.Committed {
		t.Fatal("t1 must commit")
	}

	// t2's write adds WW t1->t2, while its earlier read adds RW t2->t1.
	d2, err := a.Commit("t2", []WriteIntent{{
		Key:    "k",
		Before: Row{"v": "0"},
		After:  Row{"v": "2"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if d2.Committed {
		t.Fatal("t2 closes a t1->t2->t1 cycle and must be rejected")
	}
	kinds := map[DepKind]bool{}
	for _, e := range d2.Cycle {
		kinds[e.Kind] = true
	}
	if !kinds[DepWrite] || !kinds[DepAnti] {
		t.Fatalf("witness must contain both WW and RW edges, got %v", d2.Cycle)
	}

	if err := a.Abort("t2"); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Read(ReadSpec{TxnID: "t2", StartKey: "k", Snapshot: 0}); err == nil {
		t.Fatal("aborted transactions must reject further reads")
	}
}

// TestEmptyReadPhantom verifies empty reads are recorded and a later insert
// creates an anti-dependency (the phantom case).
func TestEmptyReadPhantom(t *testing.T) {
	a := New(nil)
	if err := a.Begin("reader", 0); err != nil {
		t.Fatal(err)
	}
	got, err := a.Read(ReadSpec{TxnID: "reader", StartKey: "x", Snapshot: 0})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 0 {
		t.Fatal("expected an empty read")
	}
	reads, err := a.Reads("reader")
	if err != nil || len(reads) != 1 {
		t.Fatalf("empty read must still be recorded, got %v (err=%v)", reads, err)
	}
	if reads[0].Observed == nil {
		t.Fatal("observed map must be non-nil even for empty reads")
	}

	if err := a.Begin("inserter", 0); err != nil {
		t.Fatal(err)
	}
	d, err := a.Commit("inserter", []WriteIntent{{Key: "x", After: Row{"v": "1"}}})
	if err != nil {
		t.Fatal(err)
	}
	if !d.Committed {
		t.Fatalf("single anti-dependency cannot form a ring, got %v", d.Cycle)
	}
	found := false
	for _, e := range a.Edges() {
		if e.Kind == DepAnti && e.From == "reader" && e.To == "inserter" && e.Scope == "key=x" {
			found = true
		}
	}
	if !found {
		t.Fatal("phantom insert into an empty read must create an anti-dependency")
	}
}

// TestGCOldSnapshotActiveTxn is the required recovery/GC acceptance scenario:
// an active transaction holds a snapshot older than finished transactions;
// after reclamation it must still be adjudicated correctly.
func TestGCOldSnapshotActiveTxn(t *testing.T) {
	a := New(map[string]Row{
		"k1": {"v": "seed"},
		"k2": {"v": "seed"},
	})
	if err := a.Begin("old", 0); err != nil {
		t.Fatal(err)
	}
	if err := a.Begin("finisher", 0); err != nil {
		t.Fatal(err)
	}
	d, err := a.Commit("finisher", []WriteIntent{{
		Key: "k1", Before: Row{"v": "seed"}, After: Row{"v": "new"},
	}})
	if err != nil || !d.Committed {
		t.Fatalf("finisher setup failed: %v %v", d, err)
	}

	// Phase 1: the old active snapshot pins the watermark at 0, so finisher
	// (seq 1) and its versions must be protected from reclamation.
	stats := a.Collect()
	if stats.Watermark != 0 {
		t.Fatalf("oldest active snapshot is 0, watermark got %d", stats.Watermark)
	}
	if stats.ReclaimedTxns != 0 || stats.CompactedKeys != 0 {
		t.Fatalf("active snapshot 0 must protect seq 1 data, got %+v", stats)
	}

	// old reads k2 at snapshot 0 while protected versions remain available.
	got, err := a.Read(ReadSpec{TxnID: "old", StartKey: "k2", Snapshot: 0})
	if err != nil {
		t.Fatal(err)
	}
	if got["k2"]["v"] != "seed" {
		t.Fatalf("old snapshot must still see the base row, got %v", got)
	}

	if err := a.Begin("writer", a.Current()); err != nil {
		t.Fatal(err)
	}
	d2, err := a.Commit("writer", []WriteIntent{{
		Key: "k2", Before: Row{"v": "seed"}, After: Row{"v": "later"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !d2.Committed {
		t.Fatalf("single anti edge is not a ring, got %v", d2.Cycle)
	}
	found := false
	for _, e := range a.Edges() {
		if e.Kind == DepAnti && e.From == "old" && e.To == "writer" {
			found = true
		}
	}
	if !found {
		t.Fatal("post-GC adjudication must still derive the old txn's anti-dependency")
	}

	// Stronger check: the old transaction itself now tries to commit. The
	// earlier writer (seq 2) changed k2, which old had read at snapshot 0, so
	// the graph already has RW old->writer; old's write on k2 adds WW
	// writer->old. The two edges close a 2-node cycle.
	dOld, err := a.Commit("old", []WriteIntent{{
		Key: "k2", Before: Row{"v": "seed"}, After: Row{"v": "old"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	if dOld.Committed {
		t.Fatal("old snapshot txn must still be adjudicated post-GC; expected rejection")
	}
	if len(dOld.Cycle) != 2 {
		t.Fatalf("expected a 2-edge witness after GC, got %v", dOld.Cycle)
	}
	kinds := map[DepKind]bool{}
	for _, e := range dOld.Cycle {
		kinds[e.Kind] = true
	}
	if !kinds[DepWrite] || !kinds[DepAnti] {
		t.Fatalf("post-GC witness must contain WW and RW edges, got %v", dOld.Cycle)
	}
	if err := a.Abort("old"); err != nil {
		t.Fatal(err)
	}

	// Phase 2: no active transaction remains. The watermark advances to the
	// latest commit sequence; finisher/writer payloads and stale versions are
	// now reclaimed, while the graph and its witnesses remain intact.
	stats2 := a.Collect()
	if stats2.Watermark != 2 {
		t.Fatalf("watermark must advance to latest seq 2, got %d", stats2.Watermark)
	}
	if stats2.ReclaimedTxns < 2 {
		t.Fatalf("both finished txn payloads should be reclaimed, got %+v", stats2)
	}
	if stats2.CompactedKeys < 2 {
		t.Fatalf("k1/k2 chains should be compacted, got %+v", stats2)
	}
	// Visibility at the new head snapshot still works after compaction.
	if row, ok := a.visible("k1", 2); !ok || row["v"] != "new" {
		t.Fatalf("compacted chain must still serve the head row, got %v ok=%v", row, ok)
	}
	// All witness edges survive reclamation.
	if len(a.Edges()) == 0 {
		t.Fatal("GC must retain the serialization graph and its witnesses")
	}
}

// TestReplaySameWitnessWithAndWithoutGC requires deterministic witnesses:
// replaying the same history, with GC interleaved, gives the same verdict and
// the same cycle.
func TestReplaySameWitnessWithAndWithoutGC(t *testing.T) {
	dNoGC, _ := runThreeTxnCycle(false)
	dGC, _ := runThreeTxnCycle(true)

	if dNoGC.Committed || dGC.Committed {
		t.Fatal("both replays must reject the final candidate")
	}
	if len(dNoGC.Cycle) != len(dGC.Cycle) {
		t.Fatalf("witness length differs: %d vs %d", len(dNoGC.Cycle), len(dGC.Cycle))
	}
	for i := range dNoGC.Cycle {
		if dNoGC.Cycle[i] != dGC.Cycle[i] {
			t.Fatalf("witness edge %d differs:\n no-gc=%v\n gc   =%v", i, dNoGC.Cycle[i], dGC.Cycle[i])
		}
	}

	// A third independent replay must produce the identical witness as well.
	dAgain, _ := runThreeTxnCycle(false)
	for i := range dNoGC.Cycle {
		if dNoGC.Cycle[i] != dAgain.Cycle[i] {
			t.Fatalf("replay is not deterministic at edge %d", i)
		}
	}
}

// TestRangeBoundariesAndPredicate exercises inclusive range edges, predicate
// filtering, out-of-range inserts and deletions observed through reads.
func TestRangeBoundariesAndPredicate(t *testing.T) {
	a := New(map[string]Row{
		"a":  {"kind": "x", "n": "1"},
		"b":  {"kind": "x", "n": "2"},
		"b0": {"kind": "y", "n": "3"}, // outside the [a,b] key range
		"c":  {"kind": "y", "n": "4"}, // in range but filtered by predicate
	})
	if err := a.Begin("reader", 0); err != nil {
		t.Fatal(err)
	}
	got, err := a.Read(ReadSpec{
		TxnID:     "reader",
		StartKey:  "a",
		EndKey:    "b",
		Predicate: map[string]string{"kind": "x"},
		Snapshot:  0,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got["a"]["n"] != "1" || got["b"]["n"] != "2" {
		t.Fatalf("inclusive range + predicate must return exactly a and b, got %v", got)
	}

	type tc struct {
		name     string
		key      string
		after    Row
		wantAnti bool
	}
	cases := []tc{
		{"in-range matching insert", "aa", Row{"kind": "x", "n": "9"}, true},
		{"in-range filtered insert", "ab", Row{"kind": "z", "n": "9"}, false},
		{"out-of-range insert", "d", Row{"kind": "x", "n": "9"}, false},
	}
	for i, c := range cases {
		writer := TxnID(fmt.Sprintf("w%d", i))
		if err := a.Begin(writer, uint64(i)); err != nil {
			t.Fatal(err)
		}
		d, err := a.Commit(writer, []WriteIntent{{Key: c.key, After: c.after}})
		if err != nil {
			t.Fatal(err)
		}
		if !d.Committed {
			t.Fatalf("%s: no ring possible, got %v", c.name, d.Cycle)
		}
		hasAnti := false
		for _, e := range a.Edges() {
			if e.Kind == DepAnti && e.From == "reader" && e.To == writer {
				hasAnti = true
			}
		}
		if hasAnti != c.wantAnti {
			t.Fatalf("%s: anti-dependency presence = %v, want %v", c.name, hasAnti, c.wantAnti)
		}
	}

	// Deleting a row the reader saw also changes its result.
	if err := a.Begin("deleter", 3); err != nil {
		t.Fatal(err)
	}
	d, err := a.Commit("deleter", []WriteIntent{{
		Key: "a", Before: Row{"kind": "x", "n": "1"}, After: nil,
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !d.Committed {
		t.Fatalf("deletion alone cannot close a ring, got %v", d.Cycle)
	}
	found := false
	for _, e := range a.Edges() {
		if e.Kind == DepAnti && e.From == "reader" && e.To == "deleter" {
			found = true
		}
	}
	if !found {
		t.Fatal("deleting an observed row must create an anti-dependency")
	}
}

// TestWriteReadDependency verifies writer->reader edges for observed versions.
func TestWriteReadDependency(t *testing.T) {
	a := New(nil)
	if err := a.Begin("writer", 0); err != nil {
		t.Fatal(err)
	}
	d, err := a.Commit("writer", []WriteIntent{{Key: "k", After: Row{"v": "1"}}})
	if err != nil || !d.Committed {
		t.Fatalf("setup failed: %v %v", d, err)
	}
	if err := a.Begin("reader", 1); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Read(ReadSpec{TxnID: "reader", StartKey: "k", Snapshot: 1}); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, e := range a.Edges() {
		if e.Kind == DepRead && e.From == "writer" && e.To == "reader" && e.Scope == "key=k" {
			found = true
		}
	}
	if !found {
		t.Fatal("reading a committed version must record a writer->reader dependency")
	}
}

// TestConcurrentInterleaving is the concurrency/交错 acceptance scenario:
// many goroutines interleave Begin/Read/Commit on disjoint keys plus churn on
// a shared key; all decisions must be internally consistent and the installed
// version count must match the commits.
func TestConcurrentInterleaving(t *testing.T) {
	a := New(map[string]Row{"shared": {"n": "0"}})
	const writers = 12
	const iterations = 40

	var wg sync.WaitGroup
	errs := make(chan error, writers*2)

	for w := 0; w < writers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < iterations; i++ {
				id := TxnID(fmt.Sprintf("w%d-%d", w, i))
				snap := a.Current()
				if err := a.Begin(id, snap); err != nil {
					errs <- err
					return
				}
				key := fmt.Sprintf("private-%d-%d", w, i)
				if _, err := a.Read(ReadSpec{TxnID: id, StartKey: key, Snapshot: snap}); err != nil {
					errs <- err
					return
				}
				d, err := a.Commit(id, []WriteIntent{{Key: key, After: Row{"n": "x"}}})
				if err != nil {
					errs <- err
					return
				}
				if !d.Committed {
					errs <- fmt.Errorf("disjoint-key txn %s unexpectedly rejected: %v", id, d.Cycle)
					return
				}
			}
		}(w)
	}

	wg.Add(1)
	go func() {
		defer wg.Done()
		for i := 0; i < iterations; i++ {
			id := TxnID(fmt.Sprintf("churn-%d", i))
			snap := a.Current()
			if err := a.Begin(id, snap); err != nil {
				errs <- err
				return
			}
			d, err := a.Commit(id, []WriteIntent{{
				Key:   "shared",
				After: Row{"n": fmt.Sprintf("%d", i)},
			}})
			if err != nil {
				errs <- err
				return
			}
			// Each churner commits or is rejected; a rejected one must abort.
			if !d.Committed {
				if err := a.Abort(id); err != nil {
					errs <- err
					return
				}
			}
		}
	}()

	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}

	committed := 0
	for _, e := range a.Edges() {
		_ = e
	}
	// Every disjoint-key writer must have committed; count their keys.
	for w := 0; w < writers; w++ {
		for i := 0; i < iterations; i++ {
			key := fmt.Sprintf("private-%d-%d", w, i)
			row, ok := a.visible(key, a.Current())
			if !ok || row["n"] != "x" {
				t.Fatalf("missing committed version for %s", key)
			}
			committed++
		}
	}
	if committed != writers*iterations {
		t.Fatalf("committed version count = %d, want %d", committed, writers*iterations)
	}

	// Read-only accessors must be safe under concurrent use as well.
	var wg2 sync.WaitGroup
	for g := 0; g < 4; g++ {
		wg2.Add(1)
		go func() {
			defer wg2.Done()
			for i := 0; i < 50; i++ {
				_ = a.Edges()
				_ = a.Current()
			}
		}()
	}
	wg2.Wait()
}

// TestValidationErrors checks boundary misuse rather than happy paths only.
func TestValidationErrors(t *testing.T) {
	a := New(nil)
	if err := a.Begin("", 0); err == nil {
		t.Fatal("empty transaction id must be rejected")
	}
	if err := a.Begin("t", 0); err != nil {
		t.Fatal(err)
	}
	if err := a.Begin("t", 0); err == nil {
		t.Fatal("duplicate transaction id must be rejected")
	}
	if _, err := a.Read(ReadSpec{TxnID: "t", StartKey: "", Snapshot: 0}); err == nil {
		t.Fatal("read without a key must be rejected")
	}
	if _, err := a.Read(ReadSpec{TxnID: "t", StartKey: "z", EndKey: "a", Snapshot: 0}); err == nil {
		t.Fatal("inverted key range must be rejected")
	}
	if err := a.Abort("t"); err != nil {
		t.Fatal(err)
	}
	if err := a.Abort("t"); err == nil {
		t.Fatal("double abort must be rejected")
	}
	if _, err := a.Commit("missing", nil); err == nil {
		t.Fatal("commit of unknown transaction must be rejected")
	}

	if err := a.Begin("dup", 0); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Commit("dup", []WriteIntent{
		{Key: "k", After: Row{"v": "1"}},
		{Key: "k", After: Row{"v": "2"}},
	}); err == nil {
		t.Fatal("commit writing the same key twice must be rejected")
	}
	if _, err := a.Commit("dup", []WriteIntent{{Key: "", After: Row{"v": "1"}}}); err == nil {
		t.Fatal("commit with an empty key must be rejected")
	}
}

// TestAbortedReaderLeavesNoEdges verifies that reads of an aborted transaction
// are not part of the serialization history: later writes on the same key must
// not derive anti-dependency edges from the aborted reader.
func TestAbortedReaderLeavesNoEdges(t *testing.T) {
	a := New(nil)
	if err := a.Begin("ghost", 0); err != nil {
		t.Fatal(err)
	}
	if _, err := a.Read(ReadSpec{TxnID: "ghost", StartKey: "k", Snapshot: 0}); err != nil {
		t.Fatal(err)
	}
	if err := a.Abort("ghost"); err != nil {
		t.Fatal(err)
	}

	if err := a.Begin("real", 0); err != nil {
		t.Fatal(err)
	}
	d, err := a.Commit("real", []WriteIntent{{Key: "k", After: Row{"v": "1"}}})
	if err != nil {
		t.Fatal(err)
	}
	if !d.Committed {
		t.Fatalf("write after an aborted empty reader must commit, got %v", d.Cycle)
	}
	for _, e := range a.Edges() {
		if e.From == "ghost" {
			t.Fatalf("aborted transaction must leave no outgoing edges, got %s", e)
		}
	}
}
