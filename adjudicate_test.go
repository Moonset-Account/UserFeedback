package mergequeue_test

import (
	"errors"
	"testing"

	mq "mergequeue"
)

func change(id, version string) mq.ChangeVersion {
	return mq.ChangeVersion{ID: id, Version: mq.SHA(version)}
}

func standardQueue() []mq.ChangeVersion {
	return []mq.ChangeVersion{
		change("A", "a1"),
		change("B", "b1"),
		change("C", "c1"),
	}
}

func standardCandidates() []mq.Candidate {
	return []mq.Candidate{
		{
			ID:     "prefix-1",
			Base:   "t0",
			Prefix: []mq.ChangeVersion{change("A", "a1")},
			Build:  "m-a",
		},
		{
			ID:     "prefix-2",
			Base:   "t0",
			Prefix: []mq.ChangeVersion{change("A", "a1"), change("B", "b1")},
			Build:  "m-ab",
			Steps: []mq.PrefixCommit{
				{Length: 1, Commit: "m-a"},
			},
		},
		{
			ID:     "prefix-3",
			Base:   "t0",
			Prefix: standardQueue(),
			Build:  "m-abc",
			Steps: []mq.PrefixCommit{
				{Length: 1, Commit: "m-a"},
				{Length: 2, Commit: "m-ab"},
			},
		},
	}
}

func receiptsFor(commits ...mq.SHA) []mq.Receipt {
	out := make([]mq.Receipt, 0, len(commits))
	for _, commit := range commits {
		out = append(out, mq.PassedReceipt(commit))
	}
	return out
}

func mustAdjudicate(t *testing.T, in mq.Input) *mq.Report {
	t.Helper()
	report, err := mq.Adjudicate(in)
	if err != nil {
		t.Fatalf("Adjudicate returned unexpected error: %v", err)
	}
	return report
}

func TestHappyPathReleasesExactHead(t *testing.T) {
	in := mq.Input{
		Trunk:      "t0",
		Queue:      standardQueue(),
		Candidates: standardCandidates(),
		Receipts:   receiptsFor("m-a", "m-ab", "m-abc"),
	}

	report := mustAdjudicate(t, in)

	if got := report.ReuseIDs(); len(got) != 3 || got[0] != "prefix-1" || got[1] != "prefix-2" || got[2] != "prefix-3" {
		t.Fatalf("ReuseIDs = %v, want all three candidates", got)
	}
	if len(report.Rebuilds) != 0 {
		t.Fatalf("Rebuilds = %+v, want none", report.Rebuilds)
	}
	if !report.CanReleaseHead() {
		t.Fatalf("head not releasable: %+v", report.Release)
	}
	if report.Release.Commit != "m-a" {
		t.Fatalf("release commit = %q, want m-a", report.Release.Commit)
	}
	if report.Head == nil || report.Head.ID != "A" {
		t.Fatalf("Head = %+v, want A", report.Head)
	}
	wantSources := []string{"prefix-1", "prefix-2", "prefix-3"}
	if len(report.Release.Sources) != len(wantSources) {
		t.Fatalf("release sources = %v, want %v", report.Release.Sources, wantSources)
	}
	for i, want := range wantSources {
		if report.Release.Sources[i] != want {
			t.Fatalf("release sources = %v, want %v", report.Release.Sources, wantSources)
		}
	}
}

func TestReceiptGateUsesOnlyExactCurrentCommit(t *testing.T) {
	base := mq.Input{
		Trunk: "t0",
		Queue: []mq.ChangeVersion{change("A", "a1")},
		Candidates: []mq.Candidate{{
			ID:     "prefix-1",
			Base:   "t0",
			Prefix: []mq.ChangeVersion{change("A", "a1")},
			Build:  "m-a",
		}},
	}

	tests := []struct {
		name        string
		headCommit  mq.SHA
		receipts    []mq.Receipt
		wantAllowed bool
		wantReason  mq.Reason
		wantAction  mq.Action
		wantState   mq.TestState
	}{
		{
			name:       "missing receipt",
			wantReason: mq.ReasonMissingReceipt,
			wantAction: mq.ActionWait,
			wantState:  mq.StateMissing,
		},
		{
			name:       "pending receipt",
			receipts:   []mq.Receipt{{Commit: "m-a", State: mq.StatePending}},
			wantReason: mq.ReasonTestPending,
			wantAction: mq.ActionWait,
			wantState:  mq.StatePending,
		},
		{
			name:       "running receipt",
			receipts:   []mq.Receipt{{Commit: "m-a", State: mq.StateRunning}},
			wantReason: mq.ReasonTestRunning,
			wantAction: mq.ActionWait,
			wantState:  mq.StateRunning,
		},
		{
			name:        "failed receipt blocks",
			receipts:    []mq.Receipt{mq.FailedReceipt("m-a")},
			wantAllowed: false,
			wantReason:  mq.ReasonTestFailed,
			wantAction:  mq.ActionFailed,
			wantState:   mq.StateFailed,
		},
		{
			name:       "passing receipt on another commit does not count",
			receipts:   receiptsFor("m-a-superseded"),
			wantReason: mq.ReasonMissingReceipt,
			wantAction: mq.ActionWait,
			wantState:  mq.StateMissing,
		},
		{
			name:        "exact passing receipt releases",
			receipts:    receiptsFor("m-a"),
			wantAllowed: true,
			wantReason:  mq.ReasonTestPassed,
			wantAction:  mq.ActionReuse,
			wantState:   mq.StatePassed,
		},
		{
			name:       "one failure among receipts wins",
			receipts:   []mq.Receipt{mq.PassedReceipt("m-a"), mq.FailedReceipt("m-a")},
			wantReason: mq.ReasonTestFailed,
			wantAction: mq.ActionFailed,
			wantState:  mq.StateFailed,
		},
		{
			name:        "authoritative current commit must match receipt exactly",
			headCommit:  "m-a-current",
			receipts:    receiptsFor("m-a", "m-a-current"),
			wantAllowed: true,
			wantReason:  mq.ReasonTestPassed,
			wantAction:  mq.ActionReuse,
			wantState:   mq.StatePassed,
		},
		{
			name:       "old passing receipt cannot satisfy authoritative new commit",
			headCommit: "m-a-current",
			receipts:   receiptsFor("m-a"),
			wantReason: mq.ReasonMissingReceipt,
			wantAction: mq.ActionReuse, // old candidate build itself still passed
			wantState:  mq.StatePassed,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			in := base
			in.HeadCommit = tt.headCommit
			in.Receipts = tt.receipts
			report := mustAdjudicate(t, in)

			if report.CanReleaseHead() != tt.wantAllowed {
				t.Fatalf("Allowed = %v, want %v; release=%+v", report.CanReleaseHead(), tt.wantAllowed, report.Release)
			}
			if report.Release.Reason != tt.wantReason {
				t.Fatalf("release reason = %q, want %q", report.Release.Reason, tt.wantReason)
			}
			d, ok := report.Candidate("prefix-1")
			if !ok {
				t.Fatal("prefix-1 decision missing")
			}
			if d.Action != tt.wantAction {
				t.Fatalf("candidate action = %q, want %q", d.Action, tt.wantAction)
			}
			if d.Test.State != tt.wantState {
				t.Fatalf("candidate test state = %q, want %q", d.Test.State, tt.wantState)
			}
		})
	}
}

func TestMiddleSwapPreservesEarlierResultsAndRebuildsAffected(t *testing.T) {
	queue := []mq.ChangeVersion{change("A", "a1"), change("B", "b2"), change("C", "c1")}
	in := mq.Input{
		Trunk:      "t0",
		Queue:      queue,
		Candidates: standardCandidates(), // prefix-2/3 still register B@b1
		Receipts:   receiptsFor("m-a", "m-ab", "m-abc"),
	}
	report := mustAdjudicate(t, in)

	p1, ok := report.Candidate("prefix-1")
	if !ok || p1.Action != mq.ActionReuse {
		t.Fatalf("prefix-1 = %+v, ok=%v; want reuse", p1, ok)
	}
	for _, id := range []string{"prefix-2", "prefix-3"} {
		d, ok := report.Candidate(id)
		if !ok {
			t.Fatalf("%s decision missing", id)
		}
		if d.Action != mq.ActionRebuild || d.Reason != mq.ReasonPrefixChanged {
			t.Fatalf("%s = %+v, want prefix_changed rebuild", id, d)
		}
		if d.MismatchIndex != 1 || d.MatchedLength != 1 {
			t.Fatalf("%s mismatch/match = %d/%d, want mismatch 1 and one match", id, d.MismatchIndex, d.MatchedLength)
		}
		if len(d.MatchedPrefixes) != 1 || d.MatchedPrefixes[0].Commit != "m-a" || d.MatchedPrefixes[0].Test.State != mq.StatePassed {
			t.Fatalf("%s matched prefixes = %+v, want retained passed m-a", id, d.MatchedPrefixes)
		}
	}

	gotIDs := report.RebuildIDs()
	if len(gotIDs) != 2 || gotIDs[0] != "prefix-2" || gotIDs[1] != "prefix-3" {
		t.Fatalf("RebuildIDs = %v, want prefix-2,prefix-3", gotIDs)
	}
	wantPrefixes := [][]mq.ChangeVersion{
		{change("A", "a1"), change("B", "b2")},
		{change("A", "a1"), change("B", "b2"), change("C", "c1")},
	}
	for i, req := range report.Rebuilds {
		if req.Base != "t0" {
			t.Fatalf("rebuild %d base = %q, want t0", i, req.Base)
		}
		if len(req.Prefix) != len(wantPrefixes[i]) {
			t.Fatalf("rebuild %d prefix = %v, want %v", i, req.Prefix, wantPrefixes[i])
		}
		for j := range wantPrefixes[i] {
			if req.Prefix[j] != wantPrefixes[i][j] {
				t.Fatalf("rebuild %d prefix = %v, want %v", i, req.Prefix, wantPrefixes[i])
			}
		}
	}

	// The unaffected first prefix still has its exact passing receipt.
	if !report.CanReleaseHead() || report.Release.Commit != "m-a" {
		t.Fatalf("head release = %+v, want m-a release", report.Release)
	}
}

func TestRebuiltMiddleSwapAndLandingInterleaving(t *testing.T) {
	swappedQueue := []mq.ChangeVersion{change("A", "a1"), change("B", "b2"), change("C", "c1")}
	rebuiltCandidates := []mq.Candidate{
		{
			ID:     "prefix-1",
			Base:   "t0",
			Prefix: []mq.ChangeVersion{change("A", "a1")},
			Build:  "m-a",
		},
		{
			ID:     "prefix-2",
			Base:   "t0",
			Prefix: []mq.ChangeVersion{change("A", "a1"), change("B", "b2")},
			Build:  "m-ab2",
			Steps:  []mq.PrefixCommit{{Length: 1, Commit: "m-a"}},
		},
		{
			ID:     "prefix-3",
			Base:   "t0",
			Prefix: swappedQueue,
			Build:  "m-ab2c",
			Steps: []mq.PrefixCommit{
				{Length: 1, Commit: "m-a"},
				{Length: 2, Commit: "m-ab2"},
			},
		},
	}

	// New builds are in flight; only the retained A result and B2 result pass.
	in := mq.Input{
		Trunk:      "t0",
		Queue:      swappedQueue,
		Candidates: rebuiltCandidates,
		Receipts: []mq.Receipt{
			mq.PassedReceipt("m-a"),
			mq.PassedReceipt("m-ab2"),
			{Commit: "m-ab2c", State: mq.StateRunning},
		},
	}
	report := mustAdjudicate(t, in)
	if !report.CanReleaseHead() || report.Release.Commit != "m-a" {
		t.Fatalf("A should still release: %+v", report.Release)
	}
	if d, _ := report.Candidate("prefix-3"); d.Action != mq.ActionWait {
		t.Fatalf("prefix-3 action = %q, want wait", d.Action)
	}

	// A lands exactly as the recorded step m-a. The queue shifts; prefix-2's
	// final build is now the head-only B2 merge result. prefix-3 is still
	// running overall, but its recorded step m-ab2 has a passing receipt.
	landed := in
	landed.Trunk = "m-a"
	landed.Queue = []mq.ChangeVersion{change("B", "b2"), change("C", "c1")}
	landed.Candidates = append([]mq.Candidate(nil), rebuiltCandidates...)
	landed.Candidates = append(landed.Candidates, mq.Candidate{
		ID:     "old-prefix-1-landed",
		Base:   "t0",
		Prefix: []mq.ChangeVersion{change("A", "a1")},
		Build:  "m-a",
	})
	landedReport := mustAdjudicate(t, landed)

	if d, _ := landedReport.Candidate("old-prefix-1-landed"); d.Action != mq.ActionRetired {
		t.Fatalf("landed c1 = %+v, want retired", d)
	}
	for _, id := range []string{"prefix-2", "prefix-3"} {
		d, ok := landedReport.Candidate(id)
		if !ok || d.AnchorLength != 1 {
			t.Fatalf("%s = %+v ok=%v, want anchor 1", id, d, ok)
		}
	}
	if !landedReport.CanReleaseHead() {
		t.Fatalf("B2 should release from exact m-ab2 receipt: %+v", landedReport.Release)
	}
	if landedReport.Release.Commit != "m-ab2" {
		t.Fatalf("release commit = %q, want m-ab2", landedReport.Release.Commit)
	}

	// A direct commit D is inserted before B lands. Old speculative commits no
	// longer contain D, so all shifted candidates must rebuild and the old
	// m-ab2 receipt must not authorize B.
	direct := landed
	direct.Trunk = "direct-d"
	directReport := mustAdjudicate(t, direct)
	for _, id := range []string{"prefix-2", "prefix-3"} {
		d, _ := directReport.Candidate(id)
		if d.Action != mq.ActionRebuild || d.Reason != mq.ReasonTrunkMoved {
			t.Fatalf("%s after direct push = %+v, want trunk_moved rebuild", id, d)
		}
	}
	if directReport.CanReleaseHead() {
		t.Fatalf("stale receipt unexpectedly released head: %+v", directReport.Release)
	}
	if directReport.Release.Reason != mq.ReasonNoCurrentCommit {
		t.Fatalf("reason = %q, want no_current_commit", directReport.Release.Reason)
	}

	// Once rebuilt on D, an exact receipt for the new current commit releases.
	direct.HeadCommit = "d-b2"
	direct.Receipts = append(direct.Receipts, mq.PassedReceipt("d-b2"))
	rebuiltOnDirect := mustAdjudicate(t, direct)
	if !rebuiltOnDirect.CanReleaseHead() || rebuiltOnDirect.Release.Commit != "d-b2" {
		t.Fatalf("exact rebuilt receipt should release: %+v", rebuiltOnDirect.Release)
	}
}

func TestMatchingIntermediateReusesLaterCandidatesAfterLanding(t *testing.T) {
	in := mq.Input{
		Trunk:      "m-a",
		Queue:      []mq.ChangeVersion{change("B", "b1"), change("C", "c1")},
		Candidates: standardCandidates(),
		Receipts:   receiptsFor("m-a", "m-ab", "m-abc"),
	}
	report := mustAdjudicate(t, in)

	c1, _ := report.Candidate("prefix-1")
	if c1.Action != mq.ActionRetired || c1.Reason != mq.ReasonLanded {
		t.Fatalf("prefix-1 = %+v, want retired/landed", c1)
	}
	c2, _ := report.Candidate("prefix-2")
	if c2.Action != mq.ActionReuse || c2.AnchorLength != 1 {
		t.Fatalf("prefix-2 = %+v, want anchored reuse", c2)
	}
	if len(c2.MatchedPrefixes) != 1 || c2.MatchedPrefixes[0].Commit != "m-ab" {
		t.Fatalf("prefix-2 matched = %+v, want current head commit m-ab", c2.MatchedPrefixes)
	}
	c3, _ := report.Candidate("prefix-3")
	if c3.Action != mq.ActionReuse || c3.AnchorLength != 1 || len(c3.MatchedPrefixes) != 2 {
		t.Fatalf("prefix-3 = %+v, want anchored reuse with two matched prefixes", c3)
	}
	if c3.MatchedPrefixes[0].Commit != "m-ab" || c3.MatchedPrefixes[1].Commit != "m-abc" {
		t.Fatalf("prefix-3 matched commits = %+v", c3.MatchedPrefixes)
	}
	if !report.CanReleaseHead() || report.Release.Commit != "m-ab" {
		t.Fatalf("release = %+v, want exact m-ab", report.Release)
	}
}

func TestDifferentLandedCommitRebuildsShiftedPrefixes(t *testing.T) {
	in := mq.Input{
		Trunk: "a-landed-with-different-sha",
		Queue: []mq.ChangeVersion{change("B", "b1"), change("C", "c1")},
		Candidates: []mq.Candidate{
			standardCandidates()[1],
			standardCandidates()[2],
		},
		Receipts: receiptsFor("m-ab", "m-abc"),
	}
	report := mustAdjudicate(t, in)

	c2, _ := report.Candidate("prefix-2")
	if c2.Action != mq.ActionRebuild || c2.Reason != mq.ReasonTrunkMoved {
		t.Fatalf("prefix-2 = %+v, want rebuild", c2)
	}
	if len(report.Rebuilds) != 2 {
		t.Fatalf("Rebuilds = %+v, want two shifted requests", report.Rebuilds)
	}
	if len(report.Rebuilds[0].Prefix) != 1 || report.Rebuilds[0].Prefix[0].ID != "B" {
		t.Fatalf("first rebuild = %v, want B-only prefix", report.Rebuilds[0].Prefix)
	}
	if len(report.Rebuilds[1].Prefix) != 2 || report.Rebuilds[1].Prefix[1].ID != "C" {
		t.Fatalf("second rebuild = %v, want B,C prefix", report.Rebuilds[1].Prefix)
	}
	if report.CanReleaseHead() {
		t.Fatalf("old m-ab receipt released after different landing: %+v", report.Release)
	}
}

func TestDirectTrunkInsertionForcesRebuildOfAllPrefixLengths(t *testing.T) {
	in := mq.Input{
		Trunk:      "direct-commit",
		Queue:      standardQueue(),
		Candidates: standardCandidates(),
		Receipts:   receiptsFor("m-a", "m-ab", "m-abc"),
	}
	report := mustAdjudicate(t, in)

	if ids := report.RebuildIDs(); len(ids) != 3 {
		t.Fatalf("RebuildIDs = %v, want all candidates", ids)
	}
	for _, d := range report.Decisions {
		if d.Action != mq.ActionRebuild || d.Reason != mq.ReasonTrunkMoved {
			t.Fatalf("%s = %+v, want trunk_moved rebuild", d.CandidateID, d)
		}
		if len(d.MatchedPrefixes) != 0 {
			t.Fatalf("%s retained prefixes = %+v; trunk changed directly, want none", d.CandidateID, d.MatchedPrefixes)
		}
	}
	if report.CanReleaseHead() {
		t.Fatalf("release allowed after direct trunk insertion: %+v", report.Release)
	}
	if report.Release.Reason != mq.ReasonNoCurrentCommit {
		t.Fatalf("reason = %q, want no_current_commit", report.Release.Reason)
	}
}

func TestAmbiguousDerivedHeadCommitIsNotReleased(t *testing.T) {
	in := mq.Input{
		Trunk: "t0",
		Queue: []mq.ChangeVersion{change("A", "a1"), change("B", "b1")},
		Candidates: []mq.Candidate{
			{
				ID:     "c-a",
				Base:   "t0",
				Prefix: []mq.ChangeVersion{change("A", "a1")},
				Build:  "m-a-one",
			},
			{
				ID:     "c-ab",
				Base:   "t0",
				Prefix: []mq.ChangeVersion{change("A", "a1"), change("B", "b1")},
				Build:  "m-ab-two",
				Steps:  []mq.PrefixCommit{{Length: 1, Commit: "m-a-two"}},
			},
		},
		Receipts: receiptsFor("m-a-one", "m-a-two", "m-ab-two"),
	}

	report := mustAdjudicate(t, in)
	if report.CanReleaseHead() || report.Release.Reason != mq.ReasonAmbiguousCommit {
		t.Fatalf("release = %+v, want ambiguity block", report.Release)
	}

	in.HeadCommit = "m-a-two"
	report = mustAdjudicate(t, in)
	if !report.CanReleaseHead() || report.Release.Commit != "m-a-two" {
		t.Fatalf("authoritative exact commit should release: %+v", report.Release)
	}
}

func TestQueueShrinkAndEmptyBoundaries(t *testing.T) {
	t.Run("candidate longer than queue is canceled but matching prefix retained", func(t *testing.T) {
		in := mq.Input{
			Trunk: "t0",
			Queue: []mq.ChangeVersion{change("A", "a1")},
			Candidates: []mq.Candidate{{
				ID:     "prefix-3",
				Base:   "t0",
				Prefix: standardQueue(),
				Build:  "m-abc",
				Steps: []mq.PrefixCommit{
					{Length: 1, Commit: "m-a"},
					{Length: 2, Commit: "m-ab"},
				},
			}},
			Receipts: receiptsFor("m-a"),
		}
		report := mustAdjudicate(t, in)
		d, _ := report.Candidate("prefix-3")
		if d.Action != mq.ActionCancel || d.Reason != mq.ReasonQueueShorter {
			t.Fatalf("decision = %+v, want cancel/queue_shorter", d)
		}
		if len(d.MatchedPrefixes) != 1 || d.MatchedPrefixes[0].Commit != "m-a" {
			t.Fatalf("matched = %+v, want retained m-a", d.MatchedPrefixes)
		}
		if !report.CanReleaseHead() {
			t.Fatalf("retained exact m-a receipt should release: %+v", report.Release)
		}
	})

	t.Run("empty queue never releases", func(t *testing.T) {
		in := mq.Input{
			Trunk: "t0",
			Candidates: []mq.Candidate{{
				ID:     "orphan",
				Base:   "t0",
				Prefix: []mq.ChangeVersion{change("A", "a1")},
				Build:  "m-a",
			}},
			Receipts: receiptsFor("m-a"),
		}
		report := mustAdjudicate(t, in)
		if report.Head != nil || report.CanReleaseHead() {
			t.Fatalf("empty queue report head=%+v release=%+v", report.Head, report.Release)
		}
		if report.Release.Reason != mq.ReasonNoHead {
			t.Fatalf("reason = %q, want no_head", report.Release.Reason)
		}
		d, _ := report.Candidate("orphan")
		if d.Action != mq.ActionCancel {
			t.Fatalf("orphan action = %q, want cancel", d.Action)
		}
	})
}

func TestAdjudicateRejectsMalformedInput(t *testing.T) {
	validCandidate := mq.Candidate{
		ID:     "c",
		Base:   "t0",
		Prefix: []mq.ChangeVersion{change("A", "a1")},
		Build:  "m-a",
	}
	tests := []struct {
		name  string
		input mq.Input
	}{
		{"missing trunk", mq.Input{Queue: standardQueue()}},
		{"missing queue version", mq.Input{Trunk: "t0", Queue: []mq.ChangeVersion{{ID: "A"}}}},
		{"duplicate candidate id", mq.Input{
			Trunk:      "t0",
			Candidates: []mq.Candidate{validCandidate, validCandidate},
		}},
		{"empty prefix", mq.Input{
			Trunk:      "t0",
			Candidates: []mq.Candidate{{ID: "c", Base: "t0", Build: "x"}},
		}},
		{"invalid receipt state", mq.Input{
			Trunk:    "t0",
			Receipts: []mq.Receipt{{Commit: "x", State: "bogus"}},
		}},
		{"step length out of range", mq.Input{
			Trunk: "t0",
			Candidates: []mq.Candidate{{
				ID: "c", Base: "t0", Prefix: validCandidate.Prefix, Build: "m-a",
				Steps: []mq.PrefixCommit{{Length: 2, Commit: "x"}},
			}},
		}},
		{"final step differs from build", mq.Input{
			Trunk: "t0",
			Candidates: []mq.Candidate{{
				ID: "c", Base: "t0", Prefix: validCandidate.Prefix, Build: "m-a",
				Steps: []mq.PrefixCommit{{Length: 1, Commit: "different"}},
			}},
		}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := mq.Adjudicate(tt.input)
			if !errors.Is(err, mq.ErrInvalidInput) {
				t.Fatalf("error = %v, want ErrInvalidInput", err)
			}
		})
	}
}
