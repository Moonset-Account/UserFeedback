package expertparallel

import (
	"errors"
	"math"
	"sync"
	"testing"
)

func nearlyEqual(a, b, tol float64) bool {
	return math.Abs(a-b) <= tol
}

func assertVector(t *testing.T, got, want []float64, label string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s: vector length = %d, want %d", label, len(got), len(want))
	}
	for i := range got {
		if !nearlyEqual(got[i], want[i], 1e-12) {
			t.Fatalf("%s: component %d = %v, want %v (full got=%v want=%v)",
				label, i, got[i], want[i], got, want)
		}
	}
}

func findResult(t *testing.T, results []RankResult, rankID, tokenID string) *TokenResult {
	t.Helper()
	for _, rr := range results {
		if rr.RankID != rankID {
			continue
		}
		for _, tr := range rr.Tokens {
			if tr != nil && tr.TokenID == tokenID {
				return tr
			}
		}
	}
	t.Fatalf("token %s of rank %s not found in results", tokenID, rankID)
	return nil
}

// TestSelectionAndMerge covers the normal path: cross-rank capacity selection
// ordered by (weight desc, token id asc), re-normalized weighted merge, and
// restoration to original batch positions.
func TestSelectionAndMerge(t *testing.T) {
	round, err := NewRound(
		[]string{"r0", "r1"},
		[]ExpertSpec{{ExpertID: "E1", Capacity: 2}, {ExpertID: "E2", Capacity: 2}},
		2,
	)
	if err != nil {
		t.Fatalf("NewRound: %v", err)
	}

	batch0 := []Token{
		{TokenID: "T0", BatchPosition: 1, Candidates: []Candidate{
			{ExpertID: "E1", Weight: 5}, {ExpertID: "E2", Weight: 1},
		}},
		{TokenID: "T1", BatchPosition: 0, Candidates: []Candidate{
			{ExpertID: "E1", Weight: 3},
		}},
	}
	batch1 := []Token{
		{TokenID: "T2", BatchPosition: 0, Candidates: []Candidate{
			{ExpertID: "E1", Weight: 3}, {ExpertID: "E2", Weight: 4},
		}},
		{TokenID: "T3", BatchPosition: 1, Candidates: []Candidate{
			{ExpertID: "E2", Weight: 4},
		}},
	}

	if round.AllBatchesCollected() {
		t.Fatal("round must not be complete after zero batches")
	}
	if err := round.SubmitBatch("r0", batch0); err != nil {
		t.Fatalf("SubmitBatch r0: %v", err)
	}
	if round.AllBatchesCollected() {
		t.Fatal("round must not be complete after one of two batches")
	}
	if _, err := round.Dispatch(); !errors.Is(err, ErrBatchesIncomplete) {
		t.Fatalf("Dispatch before all batches: got %v, want ErrBatchesIncomplete", err)
	}
	if err := round.SubmitBatch("r1", batch1); err != nil {
		t.Fatalf("SubmitBatch r1: %v", err)
	}
	if !round.AllBatchesCollected() {
		t.Fatal("AllBatchesCollected should be true after both submissions")
	}

	packages, err := round.Dispatch()
	if err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if len(packages) != 2 {
		t.Fatalf("packages = %d, want 2 (one per expert)", len(packages))
	}

	wantSelection := map[string][]string{
		"E1": {"T0", "T1"}, // weights 5,3 — T1 beats T2 on token-id tie
		"E2": {"T2", "T3"}, // weights 4,4 — T2 < T3 on token id
	}
	for _, pkg := range packages {
		gotIDs := make([]string, 0, len(pkg.WorkItems))
		for _, item := range pkg.WorkItems {
			gotIDs = append(gotIDs, item.TokenID)
		}
		want, ok := wantSelection[pkg.ExpertID]
		if !ok {
			t.Fatalf("unexpected expert package %q", pkg.ExpertID)
		}
		if len(gotIDs) != len(want) {
			t.Fatalf("expert %s selected %v, want %v", pkg.ExpertID, gotIDs, want)
		}
		for i := range want {
			if gotIDs[i] != want[i] {
				t.Fatalf("expert %s selection order = %v, want %v", pkg.ExpertID, gotIDs, want)
			}
		}
	}
	if round.TotalWorkItems() != 4 {
		t.Fatalf("TotalWorkItems = %d, want 4", round.TotalWorkItems())
	}

	// Return vectors (length 2), deliberately out of work-item order.
	returns := []ReturnPacket{
		{ExpertID: "E2", TokenID: "T3", Vector: []float64{2, 2}},
		{ExpertID: "E1", TokenID: "T1", Vector: []float64{4, 0}},
		{ExpertID: "E2", TokenID: "T2", Vector: []float64{0, 2}},
		{ExpertID: "E1", TokenID: "T0", Vector: []float64{10, 10}},
	}
	if _, err := round.Results(); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("Results before all returns: got %v, want ErrIncomplete", err)
	}
	for i, pkt := range returns {
		if err := round.SubmitReturn(pkt); err != nil {
			t.Fatalf("SubmitReturn #%d: %v", i, err)
		}
	}
	if !round.Complete() {
		t.Fatal("Complete should be true once every distinct work item returned")
	}

	results, err := round.Results()
	if err != nil {
		t.Fatalf("Results: %v", err)
	}
	if len(results) != 2 {
		t.Fatalf("rank results = %d, want 2", len(results))
	}

	// T0: E1 accepted (5), E2 rejected -> only accepted weight used.
	t0 := findResult(t, results, "r0", "T0")
	if t0.Overflow {
		t.Fatal("T0 must not be overflow: E1 accepted it")
	}
	if len(t0.AcceptedExperts) != 1 || t0.AcceptedExperts[0] != "E1" {
		t.Fatalf("T0 accepted experts = %v, want [E1]", t0.AcceptedExperts)
	}
	assertVector(t, t0.Vector, []float64{10, 10}, "T0")
	if t0.BatchPosition != 1 {
		t.Fatalf("T0 restored position = %d, want 1", t0.BatchPosition)
	}

	// T1: single E1 acceptance.
	t1 := findResult(t, results, "r0", "T1")
	assertVector(t, t1.Vector, []float64{4, 0}, "T1")
	if t1.BatchPosition != 0 {
		t.Fatalf("T1 restored position = %d, want 0", t1.BatchPosition)
	}

	// T2: E2 accepted (4), E1 rejected.
	t2 := findResult(t, results, "r1", "T2")
	assertVector(t, t2.Vector, []float64{0, 2}, "T2")

	// T3: single E2 acceptance.
	t3 := findResult(t, results, "r1", "T3")
	assertVector(t, t3.Vector, []float64{2, 2}, "T3")

	// Original batch layout must be restored exactly, including ordering.
	r0 := results[0]
	if r0.RankID != "r0" || len(r0.Tokens) != 2 {
		t.Fatalf("r0 result layout wrong: %+v", r0)
	}
	if r0.Tokens[0].TokenID != "T1" || r0.Tokens[1].TokenID != "T0" {
		t.Fatalf("r0 position restoration = [%s %s], want [T1 T0]",
			r0.Tokens[0].TokenID, r0.Tokens[1].TokenID)
	}
	r1 := results[1]
	if r1.Tokens[0].TokenID != "T2" || r1.Tokens[1].TokenID != "T3" {
		t.Fatalf("r1 position restoration = [%s %s], want [T2 T3]",
			r1.Tokens[0].TokenID, r1.Tokens[1].TokenID)
	}
}

// TestRenormalizationOverAcceptedWeights checks the key rule that merge weights
// are re-normalized over the experts that actually accepted the token, while a
// fully rejected token gets a zero vector and the overflow flag.
func TestRenormalizationAndOverflow(t *testing.T) {
	round, err := NewRound(
		[]string{"r0"},
		[]ExpertSpec{
			{ExpertID: "E1", Capacity: 1},
			{ExpertID: "E2", Capacity: 0}, // zero capacity: everything rejected
		},
		1,
	)
	if err != nil {
		t.Fatalf("NewRound: %v", err)
	}

	batch := []Token{
		// T0: both experts nominate; E1 accepts (3), E2 rejects (1 rejected).
		{TokenID: "T0", BatchPosition: 0, Candidates: []Candidate{
			{ExpertID: "E1", Weight: 3}, {ExpertID: "E2", Weight: 1},
		}},
		// T1: tie on weight with T0 at E1; loses on token id, fully rejected.
		{TokenID: "T1", BatchPosition: 1, Candidates: []Candidate{
			{ExpertID: "E1", Weight: 3},
		}},
		// T2: no candidates at all -> overflow with zero vector.
		{TokenID: "T2", BatchPosition: 2},
		// T3: only nominates the zero-capacity expert -> overflow.
		{TokenID: "T3", BatchPosition: 3, Candidates: []Candidate{
			{ExpertID: "E2", Weight: 9},
		}},
	}
	if err := round.SubmitBatch("r0", batch); err != nil {
		t.Fatalf("SubmitBatch: %v", err)
	}
	packages, err := round.Dispatch()
	if err != nil {
		t.Fatalf("Dispatch: %v", err)
	}

	// E1 keeps exactly one item (capacity 1); E2 keeps none despite 2 candidates.
	pkgByExpert := map[string]*WorkPackage{}
	for i := range packages {
		pkgByExpert[packages[i].ExpertID] = &packages[i]
	}
	if len(pkgByExpert["E1"].WorkItems) != 1 || pkgByExpert["E1"].WorkItems[0].TokenID != "T0" {
		t.Fatalf("E1 work items = %+v, want only T0", pkgByExpert["E1"].WorkItems)
	}
	if len(pkgByExpert["E2"].WorkItems) != 0 {
		t.Fatalf("E2 work items = %+v, want empty (capacity 0)", pkgByExpert["E2"].WorkItems)
	}

	if err := round.SubmitReturn(ReturnPacket{
		ExpertID: "E1", TokenID: "T0", Vector: []float64{9},
	}); err != nil {
		t.Fatalf("SubmitReturn: %v", err)
	}
	results, err := round.Results()
	if err != nil {
		t.Fatalf("Results: %v", err)
	}
	tokens := results[0].Tokens

	// T0: only E1 accepted, so its accepted weight 3 re-normalizes to 1.
	assertVector(t, tokens[0].Vector, []float64{9}, "T0")
	if tokens[0].Overflow {
		t.Fatal("T0 must not be overflow")
	}
	// T1: lost the tie, fully rejected -> zero vector + overflow.
	assertVector(t, tokens[1].Vector, []float64{0}, "T1")
	if !tokens[1].Overflow {
		t.Fatal("T1 must be overflow")
	}
	if tokens[1].AcceptedExperts != nil && len(tokens[1].AcceptedExperts) != 0 {
		t.Fatalf("T1 accepted experts = %v, want empty", tokens[1].AcceptedExperts)
	}
	// T2: no candidates -> zero vector + overflow.
	assertVector(t, tokens[2].Vector, []float64{0}, "T2")
	if !tokens[2].Overflow {
		t.Fatal("T2 (no candidates) must be overflow")
	}
	// T3: zero-capacity expert only -> zero vector + overflow.
	assertVector(t, tokens[3].Vector, []float64{0}, "T3")
	if !tokens[3].Overflow {
		t.Fatal("T3 must be overflow")
	}
}

// TestTwoAcceptedExpertsMerge exercises the weighted merge across two accepted
// experts, including exact normalization arithmetic.
func TestTwoAcceptedExpertsMerge(t *testing.T) {
	round, err := NewRound(
		[]string{"r0"},
		[]ExpertSpec{{ExpertID: "E1", Capacity: 5}, {ExpertID: "E2", Capacity: 5}},
		2,
	)
	if err != nil {
		t.Fatalf("NewRound: %v", err)
	}
	if err := round.SubmitBatch("r0", []Token{
		{TokenID: "T0", BatchPosition: 0, Candidates: []Candidate{
			{ExpertID: "E1", Weight: 1}, {ExpertID: "E2", Weight: 3},
		}},
	}); err != nil {
		t.Fatalf("SubmitBatch: %v", err)
	}
	if _, err := round.Dispatch(); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if err := round.SubmitReturn(ReturnPacket{
		ExpertID: "E1", TokenID: "T0", Vector: []float64{100, 8},
	}); err != nil {
		t.Fatalf("SubmitReturn E1: %v", err)
	}
	if err := round.SubmitReturn(ReturnPacket{
		ExpertID: "E2", TokenID: "T0", Vector: []float64{20, 0},
	}); err != nil {
		t.Fatalf("SubmitReturn E2: %v", err)
	}
	results, err := round.Results()
	if err != nil {
		t.Fatalf("Results: %v", err)
	}
	tr := results[0].Tokens[0]
	// weights 1 and 3 re-normalize to 0.25 and 0.75.
	want := []float64{0.25*100 + 0.75*20, 0.25*8 + 0.75*0} // {40, 2}
	assertVector(t, tr.Vector, want, "T0")
	if len(tr.AcceptedExperts) != 2 || tr.AcceptedExperts[0] != "E1" || tr.AcceptedExperts[1] != "E2" {
		t.Fatalf("AcceptedExperts = %v, want [E1 E2]", tr.AcceptedExperts)
	}
}

// TestDuplicateReturnIsDeduplicated verifies that re-sending one packet does not
// inflate the received count, cannot complete the round early, and the first
// received vector is what finalization uses.
func TestDuplicateReturnIsDeduplicated(t *testing.T) {
	round, err := NewRound(
		[]string{"r0"},
		[]ExpertSpec{{ExpertID: "E1", Capacity: 2}},
		1,
	)
	if err != nil {
		t.Fatalf("NewRound: %v", err)
	}
	if err := round.SubmitBatch("r0", []Token{
		{TokenID: "T0", BatchPosition: 0, Candidates: []Candidate{{ExpertID: "E1", Weight: 1}}},
		{TokenID: "T1", BatchPosition: 1, Candidates: []Candidate{{ExpertID: "E1", Weight: 1}}},
	}); err != nil {
		t.Fatalf("SubmitBatch: %v", err)
	}
	if _, err := round.Dispatch(); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}

	first := ReturnPacket{ExpertID: "E1", TokenID: "T0", Vector: []float64{7}}
	resent := ReturnPacket{ExpertID: "E1", TokenID: "T0", Vector: []float64{999}}
	if err := round.SubmitReturn(first); err != nil {
		t.Fatalf("first SubmitReturn: %v", err)
	}
	for i := 0; i < 3; i++ {
		if err := round.SubmitReturn(resent); err != nil {
			t.Fatalf("re-sent SubmitReturn #%d: %v", i, err)
		}
	}
	if round.ReturnedCount() != 1 {
		t.Fatalf("ReturnedCount = %d, want 1 after re-sends", round.ReturnedCount())
	}
	if round.Complete() {
		t.Fatal("round must not be complete: T1 work item still missing")
	}
	if _, err := round.Results(); !errors.Is(err, ErrIncomplete) {
		t.Fatalf("Results with missing item: got %v, want ErrIncomplete", err)
	}
	remaining := round.RemainingWorkItems()
	if len(remaining) != 1 || remaining[0].TokenID != "T1" {
		t.Fatalf("RemainingWorkItems = %+v, want only T1", remaining)
	}

	if err := round.SubmitReturn(ReturnPacket{
		ExpertID: "E1", TokenID: "T1", Vector: []float64{1},
	}); err != nil {
		t.Fatalf("SubmitReturn T1: %v", err)
	}
	if !round.Complete() {
		t.Fatal("round should complete once each distinct work item is in")
	}
	results, err := round.Results()
	if err != nil {
		t.Fatalf("Results: %v", err)
	}
	// First vector wins; the re-sent 999 must never affect output.
	assertVector(t, results[0].Tokens[0].Vector, []float64{7}, "T0")
}

// TestConcurrentBatchesAndShuffledReturns drives collection and reclaim from
// multiple goroutines and asserts the final merged output is still correct.
func TestConcurrentBatchesAndShuffledReturns(t *testing.T) {
	const ranks = 4
	rankIDs := make([]string, ranks)
	specs := []ExpertSpec{
		{ExpertID: "E1", Capacity: 6},
		{ExpertID: "E2", Capacity: 6},
	}
	round, err := NewRound(rankIDsFor(ranks, rankIDs), specs, 2)
	if err != nil {
		t.Fatalf("NewRound: %v", err)
	}

	// Each rank submits 3 tokens; every token nominates both experts.
	var wg sync.WaitGroup
	for ri := 0; ri < ranks; ri++ {
		ri := ri
		wg.Add(1)
		go func() {
			defer wg.Done()
			batch := make([]Token, 3)
			for p := 0; p < 3; p++ {
				batch[p] = Token{
					TokenID:       tokenName(ri, p),
					BatchPosition: p,
					Candidates: []Candidate{
						{ExpertID: "E1", Weight: ri*3 + p + 1},
						{ExpertID: "E2", Weight: 100 - (ri*3 + p)},
					},
				}
			}
			if err := round.SubmitBatch(rankIDs[ri], batch); err != nil {
				t.Errorf("SubmitBatch %s: %v", rankIDs[ri], err)
			}
		}()
	}
	wg.Wait()
	if !round.AllBatchesCollected() {
		t.Fatal("all rank batches should be collected")
	}

	packages, err := round.Dispatch()
	if err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	// Total capacity is 12; there are 24 candidates (12 per expert).
	if round.TotalWorkItems() != 12 {
		t.Fatalf("TotalWorkItems = %d, want 12", round.TotalWorkItems())
	}

	// Fire every return from its own goroutine, then re-send half of them.
	var packets []ReturnPacket
	for _, pkg := range packages {
		for _, item := range pkg.WorkItems {
			packets = append(packets, ReturnPacket{
				ExpertID: pkg.ExpertID,
				TokenID:  item.TokenID,
				Vector:   []float64{float64(item.Weight), float64(item.Weight) * 2},
			})
		}
	}
	wg = sync.WaitGroup{}
	for i, pkt := range packets {
		pkt := pkt
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := round.SubmitReturn(pkt); err != nil {
				t.Errorf("SubmitReturn %s/%s: %v", pkt.ExpertID, pkt.TokenID, err)
			}
		}()
		if i%2 == 0 {
			dup := pkt
			wg.Add(1)
			go func() {
				defer wg.Done()
				if err := round.SubmitReturn(dup); err != nil {
					t.Errorf("duplicate SubmitReturn %s/%s: %v", dup.ExpertID, dup.TokenID, err)
				}
			}()
		}
	}
	wg.Wait()

	if !round.Complete() {
		t.Fatalf("round incomplete: %d/%d returned", round.ReturnedCount(), round.TotalWorkItems())
	}
	if round.ReturnedCount() != 12 {
		t.Fatalf("ReturnedCount = %d, want 12 distinct items", round.ReturnedCount())
	}

	results, err := round.Results()
	if err != nil {
		t.Fatalf("Results: %v", err)
	}
	if len(results) != ranks {
		t.Fatalf("rank count = %d, want %d", len(results), ranks)
	}
	for ri := 0; ri < ranks; ri++ {
		rr := results[ri]
		if rr.RankID != rankIDs[ri] {
			t.Fatalf("result order[%d] = %s, want %s", ri, rr.RankID, rankIDs[ri])
		}
		if len(rr.Tokens) != 3 {
			t.Fatalf("rank %s tokens = %d, want 3", rr.RankID, len(rr.Tokens))
		}
		for p, tr := range rr.Tokens {
			if tr == nil || tr.TokenID != tokenName(ri, p) || tr.BatchPosition != p {
				t.Fatalf("rank %s position %d restored wrong: %+v", rr.RankID, p, tr)
			}
			// Accepted tokens merge real vectors; rejected ones are zero/overflow.
			if tr.Overflow {
				assertVector(t, tr.Vector, []float64{0, 0}, tr.TokenID)
				continue
			}
			if len(tr.Vector) != 2 {
				t.Fatalf("%s vector len = %d, want 2", tr.TokenID, len(tr.Vector))
			}
			// Independently recompute the expected merge.
			rec := findAccepted(t, round, tr.TokenID)
			want := recomputeMerge(t, round, rec)
			assertVector(t, tr.Vector, want, tr.TokenID)
		}
	}
}

// TestValidationAndLifecycleErrors covers boundary/error paths.
func TestValidationAndLifecycleErrors(t *testing.T) {
	if _, err := NewRound(nil, []ExpertSpec{{ExpertID: "E1", Capacity: 1}}, 1); !errors.Is(err, ErrNoRanks) {
		t.Fatalf("empty ranks: got %v, want ErrNoRanks", err)
	}
	if _, err := NewRound([]string{"r0"}, nil, 1); !errors.Is(err, ErrNoExperts) {
		t.Fatalf("empty experts: got %v, want ErrNoExperts", err)
	}
	if _, err := NewRound([]string{"r0"}, []ExpertSpec{{ExpertID: "E1", Capacity: 1}}, 0); !errors.Is(err, ErrInvalidVectorLength) {
		t.Fatalf("zero vector length: got %v, want ErrInvalidVectorLength", err)
	}
	if _, err := NewRound([]string{"r0", "r0"}, []ExpertSpec{{ExpertID: "E1", Capacity: 1}}, 1); !errors.Is(err, ErrInvalidRank) {
		t.Fatalf("duplicate ranks: got %v, want ErrInvalidRank", err)
	}
	if _, err := NewRound([]string{"r0"}, []ExpertSpec{
		{ExpertID: "E1", Capacity: 1}, {ExpertID: "E1", Capacity: 2},
	}, 1); !errors.Is(err, ErrInvalidExpert) {
		t.Fatalf("duplicate experts: got %v, want ErrInvalidExpert", err)
	}
	if _, err := NewRound([]string{"r0"}, []ExpertSpec{{ExpertID: "E1", Capacity: -1}}, 1); !errors.Is(err, ErrInvalidCapacity) {
		t.Fatalf("negative capacity: got %v, want ErrInvalidCapacity", err)
	}

	round, err := NewRound(
		[]string{"r0", "r1"},
		[]ExpertSpec{{ExpertID: "E1", Capacity: 2}},
		2,
	)
	if err != nil {
		t.Fatalf("NewRound: %v", err)
	}

	// Returns before dispatch are rejected.
	if err := round.SubmitReturn(ReturnPacket{ExpertID: "E1", TokenID: "T0", Vector: []float64{0, 0}}); !errors.Is(err, ErrNotDispatched) {
		t.Fatalf("early return: got %v, want ErrNotDispatched", err)
	}

	good := []Token{{TokenID: "T0", BatchPosition: 0, Candidates: []Candidate{{ExpertID: "E1", Weight: 1}}}}
	if err := round.SubmitBatch("unknown", good); !errors.Is(err, ErrInvalidRank) {
		t.Fatalf("unknown rank: got %v, want ErrInvalidRank", err)
	}
	if err := round.SubmitBatch("r0", good); err != nil {
		t.Fatalf("good batch: %v", err)
	}
	if err := round.SubmitBatch("r0", good); !errors.Is(err, ErrDuplicateRank) {
		t.Fatalf("double submit: got %v, want ErrDuplicateRank", err)
	}

	// Invalid batches for r1 must leave the round state untouched.
	badCases := []struct {
		name  string
		batch []Token
		want  error
	}{
		{"duplicate token id", []Token{
			{TokenID: "T0", BatchPosition: 0, Candidates: []Candidate{{ExpertID: "E1", Weight: 1}}},
		}, ErrUnknownToken},
		{"position out of range", []Token{
			{TokenID: "T9", BatchPosition: 5, Candidates: []Candidate{{ExpertID: "E1", Weight: 1}}},
		}, ErrInvalidPosition},
		{"duplicated position", []Token{
			{TokenID: "T9", BatchPosition: 0},
			{TokenID: "T8", BatchPosition: 0},
		}, ErrInvalidPosition},
		{"too many candidates", []Token{
			{TokenID: "T9", BatchPosition: 0, Candidates: []Candidate{
				{ExpertID: "E1", Weight: 1}, {ExpertID: "E1", Weight: 2}, {ExpertID: "E1", Weight: 3},
			}},
		}, ErrInvalidCandidate},
		{"non-positive weight", []Token{
			{TokenID: "T9", BatchPosition: 0, Candidates: []Candidate{{ExpertID: "E1", Weight: 0}}},
		}, ErrInvalidCandidate},
		{"unknown expert", []Token{
			{TokenID: "T9", BatchPosition: 0, Candidates: []Candidate{{ExpertID: "GHOST", Weight: 1}}},
		}, ErrInvalidCandidate},
		{"duplicate expert on token", []Token{
			{TokenID: "T9", BatchPosition: 0, Candidates: []Candidate{
				{ExpertID: "E1", Weight: 1}, {ExpertID: "E1", Weight: 2},
			}},
		}, ErrDuplicateCandidate},
	}
	for _, tc := range badCases {
		if err := round.SubmitBatch("r1", tc.batch); !errors.Is(err, tc.want) {
			t.Fatalf("%s: got %v, want %v", tc.name, err, tc.want)
		}
	}
	// r1 still needs a valid batch; failed validations must not count as submission.
	if round.AllBatchesCollected() {
		t.Fatal("rejected batches must not mark the rank as submitted")
	}
	if err := round.SubmitBatch("r1", []Token{
		{TokenID: "T1", BatchPosition: 0, Candidates: []Candidate{{ExpertID: "E1", Weight: 2}}},
	}); err != nil {
		t.Fatalf("valid r1 batch: %v", err)
	}

	packages, err := round.Dispatch()
	if err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if _, err := round.Dispatch(); !errors.Is(err, ErrNotCollecting) {
		t.Fatalf("second dispatch: got %v, want ErrNotCollecting", err)
	}
	if err := round.SubmitBatch("r0", good); !errors.Is(err, ErrNotCollecting) {
		t.Fatalf("batch after dispatch: got %v, want ErrNotCollecting", err)
	}

	if err := round.SubmitReturn(ReturnPacket{ExpertID: "GHOST", TokenID: "T0", Vector: []float64{1, 1}}); !errors.Is(err, ErrUnknownWorkItem) {
		t.Fatalf("unknown expert return: got %v, want ErrUnknownWorkItem", err)
	}
	if err := round.SubmitReturn(ReturnPacket{ExpertID: "E1", TokenID: "GHOST", Vector: []float64{1, 1}}); !errors.Is(err, ErrUnknownWorkItem) {
		t.Fatalf("unknown token return: got %v, want ErrUnknownWorkItem", err)
	}
	if err := round.SubmitReturn(ReturnPacket{ExpertID: "E1", TokenID: "T0", Vector: []float64{1}}); !errors.Is(err, ErrVectorLengthMismatch) {
		t.Fatalf("bad vector length: got %v, want ErrVectorLengthMismatch", err)
	}

	// Dispatch package contents must be independent of internal state.
	packages[0].WorkItems[0].Weight = -123
	if round.packages["E1"].WorkItems[0].Weight == -123 {
		t.Fatal("returned work packages must be independent copies")
	}
}

// TestResultsAreIndependentCopies ensures callers cannot mutate round state via
// returned slices, and repeated Results calls stay consistent.
func TestResultsAreIndependentCopies(t *testing.T) {
	round, err := NewRound(
		[]string{"r0"},
		[]ExpertSpec{{ExpertID: "E1", Capacity: 1}},
		1,
	)
	if err != nil {
		t.Fatalf("NewRound: %v", err)
	}
	if err := round.SubmitBatch("r0", []Token{
		{TokenID: "T0", BatchPosition: 0, Candidates: []Candidate{{ExpertID: "E1", Weight: 1}}},
	}); err != nil {
		t.Fatalf("SubmitBatch: %v", err)
	}
	if _, err := round.Dispatch(); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if err := round.SubmitReturn(ReturnPacket{ExpertID: "E1", TokenID: "T0", Vector: []float64{5}}); err != nil {
		t.Fatalf("SubmitReturn: %v", err)
	}
	first, err := round.Results()
	if err != nil {
		t.Fatalf("Results: %v", err)
	}
	first[0].Tokens[0].Vector[0] = -999
	second, err := round.Results()
	if err != nil {
		t.Fatalf("second Results: %v", err)
	}
	assertVector(t, second[0].Tokens[0].Vector, []float64{5}, "second Results")
}

// ---- helpers for the concurrency test ----

func rankIDsFor(n int, out []string) []string {
	for i := 0; i < n; i++ {
		out[i] = "r" + string(rune('0'+i))
	}
	return out
}

func tokenName(rank, pos int) string {
	return "T-r" + string(rune('0'+rank)) + "-p" + string(rune('0'+pos))
}

// findAccepted looks up the internal acceptance record for a token (test only).
func findAccepted(t *testing.T, r *Round, tokenID string) *tokenRecord {
	t.Helper()
	r.mu.Lock()
	defer r.mu.Unlock()
	rec, ok := r.tokens[tokenID]
	if !ok {
		t.Fatalf("internal token %s missing", tokenID)
	}
	return rec
}

// recomputeMerge independently rebuilds the expected merged vector using the
// same accepted-weight normalization rule.
func recomputeMerge(t *testing.T, r *Round, rec *tokenRecord) []float64 {
	t.Helper()
	r.mu.Lock()
	defer r.mu.Unlock()

	out := make([]float64, r.vecLen)
	sum := 0
	for _, w := range rec.accepted {
		sum += w
	}
	if sum == 0 {
		return out
	}
	for expertID, w := range rec.accepted {
		vec, ok := r.returns[expertID][rec.tokenID]
		if !ok {
			t.Fatalf("missing stored return for %s/%s", expertID, rec.tokenID)
		}
		scale := float64(w) / float64(sum)
		for i := range out {
			out[i] += scale * vec[i]
		}
	}
	return out
}
