package graphwalk

import (
	"context"
	"errors"
	"testing"
)

func mustParse(t *testing.T, s string) Expr {
	t.Helper()
	e, err := ParseExpr(s)
	if err != nil {
		t.Fatalf("ParseExpr(%q): %v", s, err)
	}
	return e
}

func collect(t *testing.T, c *Cursor, n int) []Walk {
	t.Helper()
	ctx := context.Background()
	var out []Walk
	for i := 0; i < n; i++ {
		w, err := c.Next(ctx)
		if errors.Is(err, ErrDone) {
			break
		}
		if err != nil {
			t.Fatalf("Next %d: %v", i+1, err)
		}
		out = append(out, w)
	}
	return out
}

func assertIDs(t *testing.T, walks []Walk, want ...[]EdgeID) {
	t.Helper()
	if len(walks) != len(want) {
		got := make([][]EdgeID, len(walks))
		for i, w := range walks {
			got[i] = w.IDs
		}
		t.Fatalf("walk count = %d (%v), want %d (%v)", len(walks), got, len(want), want)
	}
	for i := range want {
		if len(walks[i].IDs) != len(want[i]) {
			t.Fatalf("rank %d IDs = %v, want %v", i+1, walks[i].IDs, want[i])
		}
		for j := range want[i] {
			if walks[i].IDs[j] != want[i][j] {
				t.Fatalf("rank %d IDs = %v, want %v", i+1, walks[i].IDs, want[i])
			}
		}
	}
}

func TestLazyExpensiveIsolatedPageNeverOpened(t *testing.T) {
	r := NewMemoryReader()
	cheap := PageInfo{ID: "cheap", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 1}
	isolated := PageInfo{ID: "isolated", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "z", MinCost: 1000}
	r.Put(cheap, []Edge{
		{ID: 1, From: "s", To: "t", Label: "a", Cost: 1},
		{ID: 2, From: "s", To: "t", Label: "a", Cost: 1},
		{ID: 3, From: "s", To: "t", Label: "a", Cost: 2},
	})
	r.Put(isolated, []Edge{
		{ID: 9, From: "s", To: "t", Label: "a", Cost: 1000},
	})
	cat := NewCatalog("cat-v1", cheap, isolated)
	q := Query{Source: "s", Target: "t", Expr: mustParse(t, "a"), MaxHops: 8}
	c, err := NewCursor(q, 3, cat, r.ReadPage)
	if err != nil {
		t.Fatal(err)
	}
	ws := collect(t, c, 3)
	assertIDs(t, ws, []EdgeID{1}, []EdgeID{2}, []EdgeID{3})

	if got := r.OpenCount(isolated.Ref()); got != 0 {
		t.Fatalf("isolated page opened %d times, want 0", got)
	}
	if got := r.OpenCount(cheap.Ref()); got != 1 {
		t.Fatalf("cheap page opened %d times, want exactly 1", got)
	}
	proof := c.Proof()
	var pb *PageBound
	for i := range proof.Pages {
		if proof.Pages[i].Ref.ID == "isolated" {
			pb = &proof.Pages[i]
		}
	}
	if pb == nil {
		t.Fatal("proof lacks isolated page")
	}
	if pb.Fetched {
		t.Fatal("isolated page marked fetched")
	}
	if pb.MinimumGuardBound == nil {
		t.Fatal("isolated page has no lower-bound guard")
	}
	if pb.IsOpenBeforeKey(ws[2].Key()) {
		t.Fatalf("isolated bound %v should not compete with kth key %v", *pb.MinimumGuardBound, ws[2].Key())
	}

	// A larger k must be able to open the same page and use its real edge.
	c2, err := NewCursor(q, 4, cat, r.ReadPage)
	if err != nil {
		t.Fatal(err)
	}
	ws2 := collect(t, c2, 4)
	assertIDs(t, ws2, []EdgeID{1}, []EdgeID{2}, []EdgeID{3}, []EdgeID{9})
	if got := r.OpenCount(isolated.Ref()); got != 1 {
		t.Fatalf("isolated page opens for k=4 = %d, want 1", got)
	}
}

func TestEmptyMatchAndZeroCostCycles(t *testing.T) {
	r := NewMemoryReader()
	page := PageInfo{ID: "loop", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 0}
	r.Put(page, []Edge{
		{ID: 1, From: "s", To: "s", Label: "a", Cost: 0},
		{ID: 2, From: "s", To: "s", Label: "a", Cost: 0},
	})
	cat := NewCatalog("zeros", page)
	q := Query{Source: "s", Target: "s", Expr: Star(Lit("a")), MaxHops: 3}
	c, err := NewCursor(q, 8, cat, r.ReadPage)
	if err != nil {
		t.Fatal(err)
	}
	ws := collect(t, c, 8)
	assertIDs(t, ws,
		[]EdgeID{},
		[]EdgeID{1},
		[]EdgeID{2},
		[]EdgeID{1, 1},
		[]EdgeID{1, 2},
		[]EdgeID{2, 1},
		[]EdgeID{2, 2},
		[]EdgeID{1, 1, 1},
	)

	all, err := NewCursor(q, 1000, cat, r.ReadPage)
	if err != nil {
		t.Fatal(err)
	}
	got := collect(t, all, 1000)
	if len(got) != 1+2+4+8 {
		t.Fatalf("bounded zero-cycle walks = %d, want %d", len(got), 1+2+4+8)
	}
	last, err := all.Next(context.Background())
	if !errors.Is(err, ErrDone) {
		t.Fatalf("after exhaustion err=%v last=%v, want ErrDone", err, last.IDs)
	}
}

func TestAmbiguousExpressionBranchesReturnEdgeSequenceOnce(t *testing.T) {
	r := NewMemoryReader()
	page := PageInfo{ID: "p", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 1}
	r.Put(page, []Edge{{ID: 7, From: "s", To: "t", Label: "a", Cost: 1}})
	cat := NewCatalog("amb", page)
	expr := Alt(Lit("a"), Repeat(Lit("a"), 1, 1), Star(Lit("a")))
	q := Query{Source: "s", Target: "t", Expr: expr, MaxHops: 4}
	c, err := NewCursor(q, 10, cat, r.ReadPage)
	if err != nil {
		t.Fatal(err)
	}
	ws := collect(t, c, 10)
	assertIDs(t, ws, []EdgeID{7})
	_, err = c.Next(context.Background())
	if !errors.Is(err, ErrDone) {
		t.Fatalf("expected deduplicated exhaustion, got %v / %v", ws, err)
	}
}

func TestMaxHopsBoundAndInvalidLimit(t *testing.T) {
	r := NewMemoryReader()
	page := PageInfo{ID: "loop", Version: "v", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 0}
	r.Put(page, []Edge{{ID: 1, From: "s", To: "s", Label: "a", Cost: 0}})
	cat := NewCatalog("cat", page)
	q := Query{Source: "s", Target: "s", Expr: Star(Lit("a")), MaxHops: 2}
	c, err := NewCursor(q, 100, cat, r.ReadPage)
	if err != nil {
		t.Fatal(err)
	}
	ws := collect(t, c, 100)
	assertIDs(t, ws, []EdgeID{}, []EdgeID{1}, []EdgeID{1, 1})

	bad := Query{Source: "s", Target: "s", Expr: Epsilon(), MaxHops: MaxAllowedHops + 1}
	if _, err := NewCursor(bad, 1, cat, r.ReadPage); err == nil {
		t.Fatal("MaxHops=33 was accepted")
	}
}
