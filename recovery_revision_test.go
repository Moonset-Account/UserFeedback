package graphwalk

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
)

func lazyTestFixture(t *testing.T) (*MemoryReader, Catalog, Query) {
	t.Helper()
	r := NewMemoryReader()
	cheap := PageInfo{ID: "cheap", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 1}
	isolated := PageInfo{ID: "isolated", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "z", MinCost: 1000}
	r.Put(cheap, []Edge{
		{ID: 1, From: "s", To: "t", Label: "a", Cost: 1},
		{ID: 2, From: "s", To: "t", Label: "a", Cost: 1},
		{ID: 3, From: "s", To: "t", Label: "a", Cost: 2},
	})
	r.Put(isolated, []Edge{{ID: 9, From: "s", To: "t", Label: "a", Cost: 1000}})
	cat := NewCatalog("cat-v1", cheap, isolated)
	q := Query{Source: "s", Target: "t", Expr: mustParse(t, "a"), MaxHops: 8}
	return r, cat, q
}

func mustNext(t *testing.T, c *Cursor) Walk {
	t.Helper()
	w, err := c.Next(context.Background())
	if err != nil {
		t.Fatalf("Next: %v", err)
	}
	return w
}

func TestCheckpointResumesPendingPathsAndPageVersions(t *testing.T) {
	reader, cat, q := lazyTestFixture(t)
	c, err := NewCursor(q, 4, cat, reader.ReadPage)
	if err != nil {
		t.Fatal(err)
	}
	first := collect(t, c, 2)
	assertIDs(t, first, []EdgeID{1}, []EdgeID{2})

	data, err := c.MarshalCheckpoint()
	if err != nil {
		t.Fatal(err)
	}
	var cp Checkpoint
	if err := json.Unmarshal(data, &cp); err != nil {
		t.Fatal(err)
	}
	if len(cp.Emitted) != 2 {
		t.Fatalf("checkpoint emitted %d, want 2", len(cp.Emitted))
	}
	if len(cp.Queue) == 0 {
		t.Fatal("checkpoint has no pending paths/guards")
	}
	if cp.Fetched["cheap"].Info.Version != "v1" {
		t.Fatalf("cheap page version = %q, want v1", cp.Fetched["cheap"].Info.Version)
	}
	foundGuard := false
	for _, qi := range cp.Queue {
		if qi.Kind == kindGuard && qi.Page != nil && qi.Page.ID == "isolated" {
			foundGuard = true
		}
	}
	if !foundGuard {
		t.Fatal("checkpoint lacks isolated page guard")
	}

	fresh := NewMemoryReader()
	cheap := cat.Pages()[0]
	var isolated PageInfo
	for _, p := range cat.Pages() {
		if p.ID == "isolated" {
			isolated = p
		}
	}
	fresh.Put(cheap, []Edge{
		{ID: 1, From: "s", To: "t", Label: "a", Cost: 1},
		{ID: 2, From: "s", To: "t", Label: "a", Cost: 1},
		{ID: 3, From: "s", To: "t", Label: "a", Cost: 2},
	})
	fresh.Put(isolated, []Edge{{ID: 9, From: "s", To: "t", Label: "a", Cost: 1000}})

	restored, err := RestoreCursor(data, RestoreOptions{
		Reader:       fresh.ReadPage,
		Availability: fresh.AvailabilityFunc(),
	})
	if err != nil {
		t.Fatal(err)
	}
	if got := restored.Results(); len(got) != 2 || got[0].IDs[0] != 1 {
		t.Fatalf("restored prefix = %v", idSequences(got))
	}
	rest := collect(t, restored, 2)
	assertIDs(t, rest, []EdgeID{3}, []EdgeID{9})
	if got := fresh.OpenCount(cheap.Ref()); got != 0 {
		t.Fatalf("checkpoint-fetched cheap page reopened %d times", got)
	}
	if got := fresh.OpenCount(isolated.Ref()); got != 1 {
		t.Fatalf("isolated page opens after restore = %d, want 1", got)
	}
}

func TestCheckpointStaleVersionNamesUnavailablePage(t *testing.T) {
	_, cat, q := lazyTestFixture(t)
	c, err := NewCursor(q, 1, cat, func(context.Context, PageRef) ([]Edge, error) {
		t.Fatal("NewCursor unexpectedly read a page")
		return nil, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	data, err := c.MarshalCheckpoint()
	if err != nil {
		t.Fatal(err)
	}
	missing := NewMemoryReader()
	restored, err := RestoreCursor(data, RestoreOptions{
		Reader: missing.ReadPage,
		Availability: func(PageRef) bool {
			return false
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	err = restored.ValidateVersions()
	var stale *StaleVersionError
	if !errors.As(err, &stale) {
		t.Fatalf("ValidateVersions error = %v, want StaleVersionError", err)
	}
	if len(stale.UnavailablePages) != 2 {
		t.Fatalf("unavailable refs = %v, want both fixed pages", stale.UnavailablePages)
	}

	_, err = restored.Next(context.Background())
	if !errors.As(err, &stale) {
		t.Fatalf("Next error = %v, want StaleVersionError", err)
	}
	if len(stale.UnavailablePages) != 1 || stale.UnavailablePages[0].ID != "cheap" {
		t.Fatalf("Next stale refs = %v, want cheap@v1", stale.UnavailablePages)
	}
	if stale.UnavailablePages[0].Version != "v1" {
		t.Fatalf("resumed version = %q, want fixed v1", stale.UnavailablePages[0].Version)
	}
}

func insertionRevisionFixture(t *testing.T) (*MemoryReader, Catalog, Catalog, Query, PageRef, PageRef, PageRef) {
	t.Helper()
	r := NewMemoryReader()
	keep := PageInfo{ID: "a-keep", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 1}
	changeOld := PageInfo{ID: "b-change", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 2}
	heavy := PageInfo{ID: "z-heavy", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "z", MinCost: 1000}
	r.Put(keep, []Edge{
		{ID: 1, From: "s", To: "t", Label: "a", Cost: 1},
		{ID: 2, From: "s", To: "t", Label: "a", Cost: 1},
	})
	r.Put(changeOld, []Edge{
		{ID: 3, From: "s", To: "t", Label: "a", Cost: 2},
		{ID: 4, From: "s", To: "t", Label: "a", Cost: 3},
	})
	r.Put(heavy, []Edge{{ID: 9, From: "s", To: "t", Label: "a", Cost: 1000}})

	changeNew := changeOld
	changeNew.Version = "v2"
	changeNew.MinCost = 0
	r.Put(changeNew, []Edge{
		{ID: 5, From: "s", To: "t", Label: "a", Cost: 0},
		{ID: 3, From: "s", To: "t", Label: "a", Cost: 2},
		{ID: 4, From: "s", To: "t", Label: "a", Cost: 3},
	})
	oldCat := NewCatalog("catalog-v1", keep, changeOld, heavy)
	newCat := NewCatalog("catalog-v2", keep, changeNew, heavy)
	q := Query{Source: "s", Target: "t", Expr: mustParse(t, "a"), MaxHops: 8}
	return r, oldCat, newCat, q, keep.Ref(), changeNew.Ref(), heavy.Ref()
}

func TestRevisionInsertedCheaperEdgeAckBoundaryAndRecovery(t *testing.T) {
	r, oldCat, newCat, q, keepRef, changeNewRef, heavyRef := insertionRevisionFixture(t)
	oldCursor, err := NewCursor(q, 4, oldCat, r.ReadPage)
	if err != nil {
		t.Fatal(err)
	}
	firstTwo := collect(t, oldCursor, 2)
	assertIDs(t, firstTwo, []EdgeID{1}, []EdgeID{2})

	rc, err := oldCursor.DeriveRevision(context.Background(), RevisionInput{
		NewCatalog:   newCat,
		Changes:      DiffCatalogs(oldCat, newCat),
		Availability: r.AvailabilityFunc(),
	})
	if err != nil {
		t.Fatal(err)
	}
	plan := rc.Plan()
	if plan.PrefixLength != 0 {
		t.Fatalf("prefix length = %d, want 0 after cost-zero insertion", plan.PrefixLength)
	}
	if got := retractionRanks(plan.Retractions); len(got) != 2 || got[0] != 1 || got[1] != 2 {
		t.Fatalf("retraction ranks = %v, want [1 2]", got)
	}
	var reissueIDs [][]EdgeID
	for _, ri := range plan.Reissues {
		reissueIDs = append(reissueIDs, []EdgeID{ri.Walk.IDs[0]})
		if (ri.Rank <= 2) != ri.Replaces {
			t.Fatalf("rank %d Replaces=%v unexpectedly", ri.Rank, ri.Replaces)
		}
	}
	assertIDs(t, walksFromReissues(plan.Reissues),
		[]EdgeID{5}, []EdgeID{1}, []EdgeID{2}, []EdgeID{3})

	if got := r.OpenCount(keepRef); got != 1 {
		t.Fatalf("unchanged fetched page opens = %d, want cached 1", got)
	}
	if got := r.OpenCount(changeNewRef); got != 1 {
		t.Fatalf("new changed page opens = %d, want 1", got)
	}
	if got := r.OpenCount(heavyRef); got != 0 {
		t.Fatalf("high-bound page opens during derive = %d, want 0", got)
	}
	proof := rc.Proof()
	for _, pb := range proof.Pages {
		if pb.Ref.ID == heavyRef.ID {
			if pb.Fetched || pb.MinimumGuardBound == nil {
				t.Fatalf("heavy basis = %+v, want unread guard", pb)
			}
			fourth := plan.NewResults[3].Key()
			if pb.IsOpenBeforeKey(fourth) {
				t.Fatalf("heavy lower bound %v competes with fourth %v", *pb.MinimumGuardBound, fourth)
			}
		}
	}

	// The old cursor remains on catalog v1 and reads old page versions.
	w3 := mustNext(t, oldCursor)
	w4 := mustNext(t, oldCursor)
	assertIDs(t, []Walk{w3, w4}, []EdgeID{3}, []EdgeID{4})
	if _, err := oldCursor.Next(context.Background()); !errors.Is(err, ErrDone) {
		t.Fatalf("old cursor after k=4 err=%v, want ErrDone", err)
	}
	if got := r.OpenCount(PageRef{ID: "b-change", Version: "v1"}); got != 1 {
		t.Fatalf("old changed page opens = %d, want 1", got)
	}

	ev, err := rc.NextEvent(context.Background())
	if err != nil || ev.Type != EventRetract || ev.Rank != 1 {
		t.Fatalf("first event = %+v err=%v, want retract rank1", ev, err)
	}
	if err := rc.AckRetraction(1); err != nil {
		t.Fatal(err)
	}
	data, err := rc.MarshalCheckpoint()
	if err != nil {
		t.Fatal(err)
	}
	restored, err := RestoreRevisionCursor(data)
	if err != nil {
		t.Fatal(err)
	}
	if restored.AckedThrough() != 1 {
		t.Fatalf("restored ack boundary = %d, want 1", restored.AckedThrough())
	}
	pending := restored.PendingRetractions()
	if len(pending) != 1 || pending[0].Rank != 2 {
		t.Fatalf("pending after recovery = %+v, want only rank2", pending)
	}
	ev, err = restored.NextEvent(context.Background())
	if err != nil || ev.Type != EventRetract || ev.Rank != 2 {
		t.Fatalf("recovered event = %+v err=%v, want retract rank2; rank1 must not repeat", ev, err)
	}
	if err := restored.AckRetraction(2); err != nil {
		t.Fatal(err)
	}
	data2, err := restored.MarshalCheckpoint()
	if err != nil {
		t.Fatal(err)
	}
	restored2, err := RestoreRevisionCursor(data2)
	if err != nil {
		t.Fatal(err)
	}
	ev, err = restored2.NextEvent(context.Background())
	if err != nil || ev.Type != EventReissue || ev.Rank != 1 || ev.New.IDs[0] != 5 {
		t.Fatalf("post-boundary event = %+v err=%v, want reissue rank1 edge5", ev, err)
	}

	var gotEvents []RevisionEvent
	for {
		ev, err := restored2.NextEvent(context.Background())
		if errors.Is(err, ErrDone) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		gotEvents = append(gotEvents, ev)
	}
	// First reissue rank1 was already consumed; expect ranks2,3,4 then complete.
	var reissueRanks []int
	var sawComplete bool
	for _, ev := range gotEvents {
		if ev.Type == EventRetract && ev.Rank == 1 {
			t.Fatal("acknowledged retraction rank1 was repeated")
		}
		if ev.Type == EventReissue {
			reissueRanks = append(reissueRanks, ev.Rank)
		}
		if ev.Type == EventComplete {
			sawComplete = true
		}
	}
	if len(reissueRanks) != 3 || reissueRanks[0] != 2 || reissueRanks[2] != 4 {
		t.Fatalf("remaining reissue ranks = %v, want [2 3 4]", reissueRanks)
	}
	if !sawComplete {
		t.Fatal("revision stream lacks completion")
	}
}

func TestDeriveRevisionFromRestoredOldCheckpoint(t *testing.T) {
	r, oldCat, newCat, q, keepRef, changeNewRef, heavyRef := insertionRevisionFixture(t)
	oldCursor, err := NewCursor(q, 4, oldCat, r.ReadPage)
	if err != nil {
		t.Fatal(err)
	}
	collect(t, oldCursor, 2)
	data, err := oldCursor.MarshalCheckpoint()
	if err != nil {
		t.Fatal(err)
	}
	restoredOld, err := RestoreCursor(data, RestoreOptions{
		Reader:       r.ReadPage,
		Availability: r.AvailabilityFunc(),
	})
	if err != nil {
		t.Fatal(err)
	}

	rc, err := restoredOld.DeriveRevision(context.Background(), RevisionInput{
		NewCatalog: newCat,
		Changes:    DiffCatalogs(oldCat, newCat),
	})
	if err != nil {
		t.Fatal(err)
	}
	plan := rc.Plan()
	if plan.PrefixLength != 0 || len(plan.Retractions) != 2 {
		t.Fatalf("prefix=%d retractions=%d, want 0 and 2", plan.PrefixLength, len(plan.Retractions))
	}
	assertIDs(t, walksFromReissues(plan.Reissues),
		[]EdgeID{5}, []EdgeID{1}, []EdgeID{2}, []EdgeID{3})
	if got := r.OpenCount(keepRef); got != 1 {
		t.Fatalf("unchanged keep page opens after restore+derive = %d, want original 1", got)
	}
	if got := r.OpenCount(changeNewRef); got != 1 {
		t.Fatalf("new change page opens = %d, want1", got)
	}
	if got := r.OpenCount(heavyRef); got != 0 {
		t.Fatalf("heavy page opens = %d, want0", got)
	}
}

func TestRevisionRemovalKeepsMaxPrefixAndAckBoundary(t *testing.T) {
	r := NewMemoryReader()
	keep := PageInfo{ID: "a-keep", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 1}
	changeOld := PageInfo{ID: "b-change", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 2}
	other := PageInfo{ID: "c-other", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "a", MinCost: 3}
	heavy := PageInfo{ID: "z-heavy", Version: "v1", From: "s", MinLabel: "a", MaxLabel: "z", MinCost: 1000}
	r.Put(keep, []Edge{{ID: 1, From: "s", To: "t", Label: "a", Cost: 1}, {ID: 2, From: "s", To: "t", Label: "a", Cost: 1}})
	r.Put(changeOld, []Edge{{ID: 3, From: "s", To: "t", Label: "a", Cost: 2}, {ID: 4, From: "s", To: "t", Label: "a", Cost: 3}})
	r.Put(other, []Edge{{ID: 5, From: "s", To: "t", Label: "a", Cost: 3}})
	r.Put(heavy, []Edge{{ID: 9, From: "s", To: "t", Label: "a", Cost: 1000}})
	changeNew := changeOld
	changeNew.Version = "v2"
	changeNew.MinCost = 3
	r.Put(changeNew, []Edge{{ID: 4, From: "s", To: "t", Label: "a", Cost: 3}})

	oldCat := NewCatalog("old", keep, changeOld, other, heavy)
	newCat := NewCatalog("new", keep, changeNew, other, heavy)
	q := Query{Source: "s", Target: "t", Expr: mustParse(t, "a"), MaxHops: 8}
	oldCursor, err := NewCursor(q, 4, oldCat, r.ReadPage)
	if err != nil {
		t.Fatal(err)
	}
	oldWalks := collect(t, oldCursor, 4)
	assertIDs(t, oldWalks, []EdgeID{1}, []EdgeID{2}, []EdgeID{3}, []EdgeID{4})

	rc, err := oldCursor.DeriveRevision(context.Background(), RevisionInput{
		NewCatalog: newCat,
		Changes:    DiffCatalogs(oldCat, newCat),
	})
	if err != nil {
		t.Fatal(err)
	}
	plan := rc.Plan()
	if plan.PrefixLength != 2 {
		t.Fatalf("prefix = %d, want 2", plan.PrefixLength)
	}
	if got := retractionRanks(plan.Retractions); len(got) != 2 || got[0] != 3 || got[1] != 4 {
		t.Fatalf("retractions = %v, want [3 4]", got)
	}
	assertIDs(t, walksFromReissues(plan.Reissues), []EdgeID{4}, []EdgeID{5})
	if plan.Reissues[0].Rank != 3 || plan.Reissues[1].Rank != 4 {
		t.Fatalf("reissue ranks = %d,%d want 3,4", plan.Reissues[0].Rank, plan.Reissues[1].Rank)
	}

	if got := r.OpenCount(keep.Ref()); got != 1 {
		t.Fatalf("keep opens %d want1", got)
	}
	if got := r.OpenCount(other.Ref()); got != 1 {
		t.Fatalf("other opens %d want1 (derivation used cache callback path)", got)
	}
	if got := r.OpenCount(changeNew.Ref()); got != 1 {
		t.Fatalf("change new opens %d want1", got)
	}
	if got := r.OpenCount(heavy.Ref()); got != 0 {
		t.Fatalf("heavy opens %d want0", got)
	}

	if err := rc.AckRetractionsThrough(3); err != nil {
		t.Fatal(err)
	}
	data, err := rc.MarshalCheckpoint()
	if err != nil {
		t.Fatal(err)
	}
	restored, err := RestoreRevisionCursor(data)
	if err != nil {
		t.Fatal(err)
	}
	if restored.AckedThrough() != 3 {
		t.Fatalf("boundary = %d want3", restored.AckedThrough())
	}
	pending := restored.PendingRetractions()
	if len(pending) != 1 || pending[0].Rank != 4 {
		t.Fatalf("pending = %+v want rank4 only; rank3 must not be retracted again", pending)
	}
	ev, err := restored.NextEvent(context.Background())
	if err != nil || ev.Type != EventRetract || ev.Rank != 4 {
		t.Fatalf("event = %+v err=%v want rank4 retraction", ev, err)
	}
}

func retractionRanks(rs []Retraction) []int {
	out := make([]int, len(rs))
	for i, r := range rs {
		out[i] = r.Rank
	}
	return out
}

func walksFromReissues(rs []Reissue) []Walk {
	out := make([]Walk, len(rs))
	for i, r := range rs {
		out[i] = r.Walk
	}
	return out
}
