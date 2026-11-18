package cloneplan

import (
	"errors"
	"fmt"
	"sync"
	"testing"
)

// Fixture tenants and fixed-version snapshot layers.
const (
	dest = "dest-tenant"
	src  = "src-tenant"
)

var (
	baseRef = SnapshotRef{SnapshotID: "snap-base", Version: "v1"}
	midRef  = SnapshotRef{SnapshotID: "snap-mid", Version: "v2"}
	headRef = SnapshotRef{SnapshotID: "snap-head", Version: "v3"}
)

// newFixtureStore builds the chain
//
//	base v1 (root)
//	  [0,20)  public    base-pub   owner src
//	  [20,40) sensitive base-sec1  owner src
//	  [50,60) internal  base-int   owner src
//	mid v2 (parent base)
//	  [10,20) sensitive mid-sec2   owner src           (overrides base public)
//	  [40,50) explicit zero
//	  [70,80) sensitive mid-disc   owner src, discardable
//	head v3 (parent mid)
//	  [0,10)  internal  head-int        owner dest      (owned by target)
//	  [30,40) explicit zero                                (shadows base sensitive)
//	  [60,70) sensitive head-sec3  owner src, non-discardable
//	  [80,90) sensitive head-owned owner dest             (owned sensitive)
//
// [90,100) is a hole in every layer and terminates as sparse-hole zero.
func newFixtureStore(t *testing.T) *Store {
	t.Helper()
	s := NewStore()
	must(t, s.Register(Snapshot{
		Ref: baseRef,
		Records: []BlockRecord{
			{Range: Range{0, 20}, BlockVersion: "base-pub", Classification: ClassPublic, OwnerTenant: src},
			{Range: Range{20, 40}, BlockVersion: "base-sec1", Classification: ClassSensitive, OwnerTenant: src},
			{Range: Range{50, 60}, BlockVersion: "base-int", Classification: ClassInternal, OwnerTenant: src},
		},
	}))
	must(t, s.Register(Snapshot{
		Ref:    midRef,
		Parent: &baseRef,
		Records: []BlockRecord{
			{Range: Range{10, 20}, BlockVersion: "mid-sec2", Classification: ClassSensitive, OwnerTenant: src},
			{Range: Range{40, 50}, Zero: true},
			{Range: Range{70, 80}, BlockVersion: "mid-disc", Classification: ClassSensitive, OwnerTenant: src, Discardable: true},
		},
	}))
	must(t, s.Register(Snapshot{
		Ref:    headRef,
		Parent: &midRef,
		Records: []BlockRecord{
			{Range: Range{0, 10}, BlockVersion: "head-int", Classification: ClassInternal, OwnerTenant: dest},
			{Range: Range{30, 40}, Zero: true},
			{Range: Range{60, 70}, BlockVersion: "head-sec3", Classification: ClassSensitive, OwnerTenant: src},
			{Range: Range{80, 90}, BlockVersion: "head-owned-sec", Classification: ClassSensitive, OwnerTenant: dest},
		},
	}))
	return s
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func assertErrIs(t *testing.T, err error, target error) {
	t.Helper()
	if !errors.Is(err, target) {
		t.Fatalf("want error matching %v, got %v", target, err)
	}
}

// segmentAt returns the planned segment covering block at.
func segmentAt(plan *ClonePlan, at uint64) PlannedSegment {
	for _, seg := range plan.Segments {
		if seg.Range.Start <= at && at < seg.Range.End {
			return seg
		}
	}
	panic(fmt.Sprintf("no planned segment covers block %d", at))
}

func resolvedAt(plan *ClonePlan, at uint64) ResolvedSegment {
	for _, seg := range plan.Resolved {
		if seg.Range.Start <= at && at < seg.Range.End {
			return seg
		}
	}
	panic(fmt.Sprintf("no resolved segment covers block %d", at))
}

func assertContiguous(t *testing.T, plan *ClonePlan) {
	t.Helper()
	if len(plan.Segments) == 0 {
		t.Fatal("plan has no segments")
	}
	start := plan.CloneRange.Start
	for _, seg := range plan.Segments {
		if seg.Range.Start != start {
			t.Fatalf("gap/overlap before %d..%d (cursor %d)", seg.Range.Start, seg.Range.End, start)
		}
		if !seg.Range.Valid() {
			t.Fatalf("invalid segment range %d..%d", seg.Range.Start, seg.Range.End)
		}
		start = seg.Range.End
	}
	if start != plan.CloneRange.End {
		t.Fatalf("segments end at %d, want %d", start, plan.CloneRange.End)
	}
}

// fullValidReceipts covers every non-owned data range with receipts whose
// block version is the effective one; [10,20) is intentionally covered by two
// receipts to exercise multi-receipt coverage of one interval.
func fullValidReceipts() []SanitizationReceipt {
	return []SanitizationReceipt{
		{ReceiptID: "rc-a", TargetTenant: dest, Range: Range{10, 15}, BlockVersion: "mid-sec2"},
		{ReceiptID: "rc-b", TargetTenant: dest, Range: Range{15, 20}, BlockVersion: "mid-sec2"},
		{ReceiptID: "rc-c", TargetTenant: dest, Range: Range{20, 30}, BlockVersion: "base-sec1"},
		{ReceiptID: "rc-d", TargetTenant: dest, Range: Range{50, 60}, BlockVersion: "base-int"},
		{ReceiptID: "rc-e", TargetTenant: dest, Range: Range{60, 70}, BlockVersion: "head-sec3"},
		{ReceiptID: "rc-f", TargetTenant: dest, Range: Range{70, 80}, BlockVersion: "mid-disc"},
	}
}

func TestBuildPlan_AcceptableFullClone(t *testing.T) {
	s := newFixtureStore(t)
	plan, err := s.BuildPlan(PlanRequest{
		TargetTenant: dest,
		Leaf:         headRef,
		CloneRange:   Range{0, 100},
		Receipts:     fullValidReceipts(),
	})
	must(t, err)
	if plan.Blocked() {
		t.Fatalf("plan should be publishable, blocked reasons: %v", plan.BlockedReasons)
	}
	assertContiguous(t, plan)

	wantChain := []SnapshotRef{headRef, midRef, baseRef}
	if fmt.Sprint(plan.PinnedChain) != fmt.Sprint(wantChain) {
		t.Fatalf("pinned chain = %v, want %v", plan.PinnedChain, wantChain)
	}

	cases := []struct {
		at           uint64
		decision     Decision
		version      string
		origin       *SnapshotRef
		zero         bool
		explicitZero bool
		reason       string
	}{
		{5, DecisionPublish, "head-int", &headRef, false, false, "owned-by-target"},
		{12, DecisionPublish, "mid-sec2", &midRef, false, false, "sanitization-receipt"},
		{17, DecisionPublish, "mid-sec2", &midRef, false, false, "sanitization-receipt"},
		{25, DecisionPublish, "base-sec1", &baseRef, false, false, "sanitization-receipt"},
		{35, DecisionZero, "", &headRef, true, true, ReasonExplicitZero},
		{45, DecisionZero, "", &midRef, true, true, ReasonExplicitZero},
		{55, DecisionPublish, "base-int", &baseRef, false, false, "sanitization-receipt"},
		{65, DecisionPublish, "head-sec3", &headRef, false, false, "sanitization-receipt"},
		{75, DecisionPublish, "mid-disc", &midRef, false, false, "sanitization-receipt"},
		{85, DecisionPublish, "head-owned-sec", &headRef, false, false, "owned-by-target"},
		{95, DecisionZero, "", nil, true, false, ReasonSparseHole},
	}
	for _, c := range cases {
		seg := segmentAt(plan, c.at)
		if seg.Decision != c.decision || seg.BlockVersion != c.version || seg.Zero != c.zero ||
			seg.ExplicitZero != c.explicitZero || seg.Reason != c.reason {
			t.Fatalf("block %d segment = %+v, want decision=%s version=%s zero=%v explicit=%v reason=%s",
				c.at, seg, c.decision, c.version, c.zero, c.explicitZero, c.reason)
		}
		if (seg.Origin == nil) != (c.origin == nil) {
			t.Fatalf("block %d origin presence mismatch: %v", c.at, seg.Origin)
		}
		if c.origin != nil && *seg.Origin != *c.origin {
			t.Fatalf("block %d origin = %v, want %v", c.at, seg.Origin, c.origin)
		}
	}

	// Staging + atomic commit: the clone becomes visible and gets a sequence.
	must(t, s.Stage(plan))
	if _, ok := s.GetPublished(plan.PlanID); ok {
		t.Fatal("staged draft must not be visible before commit")
	}
	pub, err := s.Commit(plan.PlanID)
	must(t, err)
	if pub.Sequence != 1 || pub.TargetTenant != dest || pub.Leaf != headRef {
		t.Fatalf("unexpected published clone: %+v", pub)
	}
	if got, ok := s.GetPublished(plan.PlanID); !ok || got.PlanID != plan.PlanID {
		t.Fatal("committed clone must be visible")
	}
	if len(s.ListPublished()) != 1 {
		t.Fatal("exactly one visible clone expected")
	}
	if _, err = s.Commit(plan.PlanID); !errors.Is(err, ErrAlreadyStaged) {
		t.Fatalf("re-commit should fail with ErrAlreadyStaged, got %v", err)
	}
	if _, err = s.Commit("does-not-exist"); !errors.Is(err, ErrPlanNotFound) {
		t.Fatalf("unknown commit should fail with ErrPlanNotFound, got %v", err)
	}
}

func TestBuildPlan_BlockedMissingReceipt(t *testing.T) {
	s := newFixtureStore(t)

	// Non-discardable sensitive [60,70) has no receipt: publication must be
	// blocked, while the discardable [70,80) without receipt is zeroed.
	receipts := []SanitizationReceipt{
		{ReceiptID: "rc-a", TargetTenant: dest, Range: Range{10, 20}, BlockVersion: "mid-sec2"},
		{ReceiptID: "rc-c", TargetTenant: dest, Range: Range{20, 30}, BlockVersion: "base-sec1"},
		{ReceiptID: "rc-d", TargetTenant: dest, Range: Range{50, 60}, BlockVersion: "base-int"},
	}
	plan, err := s.BuildPlan(PlanRequest{
		TargetTenant: dest,
		Leaf:         headRef,
		CloneRange:   Range{0, 100},
		Receipts:     receipts,
	})
	assertErrIs(t, err, ErrPublicationBlocked)
	if plan == nil || !plan.Blocked() || len(plan.BlockedReasons) == 0 {
		t.Fatalf("blocked plan with reasons expected, plan=%+v err=%v", plan, err)
	}
	blocked := segmentAt(plan, 65)
	if blocked.Decision != DecisionBlocked || blocked.Reason != BlockedMissingReceipt {
		t.Fatalf("[60,70) should be blocked, got %+v", blocked)
	}
	discarded := segmentAt(plan, 75)
	if discarded.Decision != DecisionZero || discarded.Reason != "discardable-sensitive-zeroed" || !discarded.Discardable {
		t.Fatalf("[70,80) discardable sensitive should be zeroed, got %+v", discarded)
	}
	// A blocked plan cannot be staged and therefore can never become visible.
	assertErrIs(t, s.Stage(plan), ErrPlanBlocked)
	if _, ok := s.GetDraft(plan.PlanID); ok {
		t.Fatal("blocked plan must not be held as a draft")
	}
	if len(s.ListPublished()) != 0 {
		t.Fatal("nothing should be visible")
	}
}

func TestBuildPlan_StaleReceiptVersionRejected(t *testing.T) {
	s := newFixtureStore(t)

	// Receipt names a superseded version of the ancestor block.
	stale := SanitizationReceipt{ReceiptID: "rc-stale", TargetTenant: dest, Range: Range{20, 30}, BlockVersion: "base-sec0"}
	_, err := s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: headRef, CloneRange: Range{0, 100},
		Receipts: append(fullValidReceipts(), stale),
	})
	assertErrIs(t, err, ErrReceiptStaleVersion)

	// Receipt names the base version of a range the head actually overrides.
	override := SanitizationReceipt{ReceiptID: "rc-override", TargetTenant: dest, Range: Range{0, 10}, BlockVersion: "base-pub"}
	_, err = s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: headRef, CloneRange: Range{0, 100},
		Receipts: []SanitizationReceipt{override},
	})
	assertErrIs(t, err, ErrReceiptStaleVersion)
}

func TestBuildPlan_BoundaryInterleave(t *testing.T) {
	s := newFixtureStore(t)

	// Receipt straddles a data segment and an explicit-zero segment.
	crossZero := SanitizationReceipt{ReceiptID: "rc-cross-zero", TargetTenant: dest, Range: Range{25, 35}, BlockVersion: "base-sec1"}
	_, err := s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: headRef, CloneRange: Range{0, 100},
		Receipts: []SanitizationReceipt{crossZero},
	})
	assertErrIs(t, err, ErrReceiptCoversZero)

	// Receipt straddles two effective versions: head head-int [8,10) and
	// mid mid-sec2 [10,12). Neither single version is effective over all of it.
	crossVersion := SanitizationReceipt{ReceiptID: "rc-cross-ver", TargetTenant: dest, Range: Range{8, 12}, BlockVersion: "head-int"}
	_, err = s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: headRef, CloneRange: Range{0, 100},
		Receipts: []SanitizationReceipt{crossVersion},
	})
	assertErrIs(t, err, ErrReceiptStaleVersion)

	// Structural boundary: receipt escapes the requested clone range.
	outOfRange := SanitizationReceipt{ReceiptID: "rc-out", TargetTenant: dest, Range: Range{55, 65}, BlockVersion: "base-int"}
	_, err = s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: headRef, CloneRange: Range{20, 60},
		Receipts: []SanitizationReceipt{outOfRange},
	})
	assertErrIs(t, err, ErrReceiptOutOfRange)

	// Accepted boundary case: a partial clone range with exact-fit receipts.
	plan, err := s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: headRef, CloneRange: Range{20, 60},
		Receipts: []SanitizationReceipt{
			{ReceiptID: "rc-1", TargetTenant: dest, Range: Range{20, 30}, BlockVersion: "base-sec1"},
			{ReceiptID: "rc-2", TargetTenant: dest, Range: Range{50, 60}, BlockVersion: "base-int"},
		},
	})
	must(t, err)
	assertContiguous(t, plan)
	if segmentAt(plan, 35).Origin == nil || *segmentAt(plan, 35).Origin != headRef || !segmentAt(plan, 35).ExplicitZero {
		t.Fatal("partial clone must keep head explicit-zero semantics on [30,40)")
	}
	if seg := segmentAt(plan, 45); seg.Origin == nil || *seg.Origin != midRef || !seg.ExplicitZero {
		t.Fatal("partial clone must resolve [40,50) to the mid explicit-zero layer")
	}
	if seg := segmentAt(plan, 25); seg.Origin == nil || *seg.Origin != baseRef || seg.BlockVersion != "base-sec1" {
		t.Fatal("hole in head and mid must be looked up in the ancestor base")
	}
}

func TestBuildPlan_ReceiptStructuralGuards(t *testing.T) {
	s := newFixtureStore(t)
	base := PlanRequest{TargetTenant: dest, Leaf: headRef, CloneRange: Range{0, 100}}

	_, err := s.BuildPlan(PlanRequest{TargetTenant: "", Leaf: headRef, CloneRange: Range{0, 100}})
	assertErrIs(t, err, ErrEmptyTenant)

	_, err = s.BuildPlan(PlanRequest{TargetTenant: dest, Leaf: headRef, CloneRange: Range{50, 40}})
	assertErrIs(t, err, ErrInvalidRange)

	unknown := SnapshotRef{SnapshotID: "nope", Version: "v9"}
	_, err = s.BuildPlan(PlanRequest{TargetTenant: dest, Leaf: unknown, CloneRange: Range{0, 100}})
	assertErrIs(t, err, ErrSnapshotNotFound)

	req := base
	req.Receipts = []SanitizationReceipt{
		{ReceiptID: "rc-x", TargetTenant: "other-tenant", Range: Range{20, 30}, BlockVersion: "base-sec1"},
	}
	_, err = s.BuildPlan(req)
	assertErrIs(t, err, ErrReceiptTenantMismatch)

	req = base
	req.Receipts = []SanitizationReceipt{
		{ReceiptID: "dup", TargetTenant: dest, Range: Range{20, 25}, BlockVersion: "base-sec1"},
		{ReceiptID: "dup", TargetTenant: dest, Range: Range{25, 30}, BlockVersion: "base-sec1"},
	}
	_, err = s.BuildPlan(req)
	assertErrIs(t, err, ErrDuplicateReceiptID)
}

func TestCommit_ChainVerificationFailureIsInvisible(t *testing.T) {
	s := newFixtureStore(t)
	plan, err := s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: headRef, CloneRange: Range{0, 100}, Receipts: fullValidReceipts(),
	})
	must(t, err)
	must(t, s.Stage(plan))

	// Fixed-version rotation removes an ancestor the draft still depends on.
	must(t, s.RetireVersion(baseRef))

	_, err = s.Commit(plan.PlanID)
	assertErrIs(t, err, ErrChainMismatch)
	if len(s.ListPublished()) != 0 {
		t.Fatal("failed commit must not create any visible clone")
	}
	if _, ok := s.GetDraft(plan.PlanID); ok {
		t.Fatal("failed draft must be discarded")
	}

	// Source snapshots remain registered and usable.
	if head, ok := s.Get(headRef); !ok || head.Ref != headRef {
		t.Fatal("head source snapshot must remain readable after failed commit")
	}

	// An independent, still-valid chain can be planned and published.
	isoRef := SnapshotRef{SnapshotID: "snap-iso", Version: "v1"}
	must(t, s.Register(Snapshot{
		Ref: isoRef,
		Records: []BlockRecord{
			{Range: Range{0, 10}, BlockVersion: "iso-1", Classification: ClassPublic, OwnerTenant: dest},
		},
	}))
	plan2, err := s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: isoRef, CloneRange: Range{0, 10},
	})
	must(t, err)
	must(t, s.Stage(plan2))
	pub2, err := s.Commit(plan2.PlanID)
	must(t, err)
	if pub2.Sequence != 1 || len(s.ListPublished()) != 1 {
		t.Fatalf("independent clone should publish with sequence 1, got %+v", pub2)
	}
}

func TestRegistry_GuardsAndDefensiveCopies(t *testing.T) {
	s := NewStore()

	dup := Snapshot{Ref: baseRef, Records: []BlockRecord{{Range: Range{0, 10}, BlockVersion: "a", Classification: ClassPublic, OwnerTenant: src}}}
	must(t, s.Register(dup))
	assertErrIs(t, s.Register(dup), ErrDuplicateSnapshot)

	orphan := Snapshot{
		Ref:    SnapshotRef{SnapshotID: "orphan", Version: "v1"},
		Parent: &SnapshotRef{SnapshotID: "ghost", Version: "v1"},
	}
	assertErrIs(t, s.Register(orphan), ErrParentNotFound)

	overlap := Snapshot{
		Ref: SnapshotRef{SnapshotID: "overlap", Version: "v1"},
		Records: []BlockRecord{
			{Range: Range{0, 11}, BlockVersion: "a", Classification: ClassPublic, OwnerTenant: src},
			{Range: Range{10, 20}, BlockVersion: "b", Classification: ClassPublic, OwnerTenant: src},
		},
	}
	assertErrIs(t, s.Register(overlap), ErrOverlappingRecords)

	badRange := Snapshot{
		Ref:     SnapshotRef{SnapshotID: "bad", Version: "v1"},
		Records: []BlockRecord{{Range: Range{5, 5}, Classification: ClassPublic}},
	}
	assertErrIs(t, s.Register(badRange), ErrInvalidRange)

	badDiscard := Snapshot{
		Ref: SnapshotRef{SnapshotID: "bad-disc", Version: "v1"},
		Records: []BlockRecord{
			{Range: Range{0, 10}, Classification: ClassPublic, OwnerTenant: src, Discardable: true},
		},
	}
	assertErrIs(t, s.Register(badDiscard), ErrInvalidRange)

	// Mutating the input after registration must not affect the registry.
	in := Snapshot{
		Ref:     SnapshotRef{SnapshotID: "mut", Version: "v1"},
		Records: []BlockRecord{{Range: Range{0, 10}, BlockVersion: "v", Classification: ClassPublic, OwnerTenant: src}},
	}
	must(t, s.Register(in))
	in.Records[0].BlockVersion = "tampered"
	got, ok := s.Get(in.Ref)
	if !ok || got.Records[0].BlockVersion != "v" {
		t.Fatalf("registry must keep its own copy, got %+v", got)
	}
	got.Records[0].BlockVersion = "tampered-too"
	again, _ := s.Get(in.Ref)
	if again.Records[0].BlockVersion != "v" {
		t.Fatal("Get must return a defensive copy")
	}
}

func TestCommit_DefensiveCopiesOfPublishedClone(t *testing.T) {
	s := newFixtureStore(t)
	plan, err := s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: headRef, CloneRange: Range{0, 100}, Receipts: fullValidReceipts(),
	})
	must(t, err)
	must(t, s.Stage(plan))
	pub, err := s.Commit(plan.PlanID)
	must(t, err)
	pub.Segments[0].Decision = DecisionBlocked
	pub.ParentChain[0] = SnapshotRef{SnapshotID: "forged", Version: "x"}
	fresh, ok := s.GetPublished(plan.PlanID)
	if !ok {
		t.Fatal("published clone disappeared")
	}
	if fresh.Segments[0].Decision != DecisionPublish || fresh.ParentChain[0] != headRef {
		t.Fatal("caller mutation of a returned clone must not affect the stored publication")
	}
}

func TestConcurrentCommitsAreAtomic(t *testing.T) {
	s := newFixtureStore(t)
	const n = 16
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			receipts := fullValidReceipts()
			for j := range receipts {
				receipts[j].ReceiptID = fmt.Sprintf("g%d-%s", i, receipts[j].ReceiptID)
			}
			plan, err := s.BuildPlan(PlanRequest{
				TargetTenant: dest,
				Leaf:         headRef,
				CloneRange:   Range{0, 100},
				Receipts:     receipts,
			})
			if err != nil {
				errs <- err
				return
			}
			if err := s.Stage(plan); err != nil {
				errs <- err
				return
			}
			if _, err := s.Commit(plan.PlanID); err != nil {
				errs <- err
			}
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent publication failed: %v", err)
	}
	pubs := s.ListPublished()
	if len(pubs) != n {
		t.Fatalf("want %d visible clones, got %d", n, len(pubs))
	}
	seenSeq := map[int64]bool{}
	for _, pub := range pubs {
		if seenSeq[pub.Sequence] {
			t.Fatalf("duplicate commit sequence %d", pub.Sequence)
		}
		seenSeq[pub.Sequence] = true
	}
}

func TestBuildPlan_PartialReceiptOnOneSegment(t *testing.T) {
	s := newFixtureStore(t)

	// A single internal-data segment [50,60) receives a receipt covering only
	// [50,55). The covered sub-segment publishes; the uncovered remainder must
	// block rather than silently leaking data.
	plan, err := s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: headRef, CloneRange: Range{50, 60},
		Receipts: []SanitizationReceipt{
			{ReceiptID: "rc-part", TargetTenant: dest, Range: Range{50, 55}, BlockVersion: "base-int"},
		},
	})
	assertErrIs(t, err, ErrPublicationBlocked)
	covered := segmentAt(plan, 52)
	if covered.Decision != DecisionPublish || covered.Reason != "sanitization-receipt" {
		t.Fatalf("covered sub-segment should publish, got %+v", covered)
	}
	exposed := segmentAt(plan, 57)
	if exposed.Decision != DecisionBlocked || exposed.Reason != BlockedMissingReceipt {
		t.Fatalf("uncovered sub-segment must block, got %+v", exposed)
	}
	assertContiguous(t, plan)
}

func TestResolution_ZeroSemanticsAndAncestorLookup(t *testing.T) {
	s := newFixtureStore(t)
	plan, err := s.BuildPlan(PlanRequest{
		TargetTenant: dest, Leaf: headRef, CloneRange: Range{0, 100}, Receipts: fullValidReceipts(),
	})
	must(t, err)

	// Head explicit zero shadows the ancestor's sensitive [30,40) content.
	z := resolvedAt(plan, 35)
	if !z.Zero || !z.ExplicitZero || z.Origin == nil || *z.Origin != headRef || z.EffectiveVersion != "" {
		t.Fatalf("explicit zero semantics lost: %+v", z)
	}

	// Mid explicit zero reached through a head hole.
	mz := resolvedAt(plan, 45)
	if !mz.Zero || !mz.ExplicitZero || mz.Origin == nil || *mz.Origin != midRef {
		t.Fatalf("ancestor explicit zero not resolved: %+v", mz)
	}

	// Hole in head and mid resolved from the base ancestor.
	anc := resolvedAt(plan, 25)
	if anc.Origin == nil || *anc.Origin != baseRef || anc.EffectiveVersion != "base-sec1" || anc.Reason != ReasonResolved {
		t.Fatalf("ancestor lookup failed: %+v", anc)
	}

	// Hole through the whole chain terminates as sparse-hole zero.
	hole := resolvedAt(plan, 95)
	if !hole.Zero || hole.ExplicitZero || hole.Origin != nil || hole.Reason != ReasonSparseHole {
		t.Fatalf("sparse hole semantics wrong: %+v", hole)
	}
}
