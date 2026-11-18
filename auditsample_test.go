package auditsample_test

import (
	"errors"
	"reflect"
	"strings"
	"testing"

	"auditsample"
)

// fullGrid returns one candidate per (control, bizline) pair of
// {auth,crypto,logging} x {pay,risk}.
func fullGrid() []auditsample.Candidate {
	controls := []string{"auth", "crypto", "logging"}
	lines := []string{"pay", "risk"}
	var out []auditsample.Candidate
	id := 0
	for _, c := range controls {
		for _, l := range lines {
			id++
			out = append(out, auditsample.Candidate{
				ChangeID: "CHG-" + string(rune('0'+id)),
				Control:  c,
				BizLine:  l,
			})
		}
	}
	return out
}

func gridRules(quota int) auditsample.Rules {
	return auditsample.Rules{
		Quota:          quota,
		ControlMinimum: map[string]int{"auth": 1, "crypto": 1, "logging": 1},
		BizLineMinimum: map[string]int{"pay": 1, "risk": 1},
	}
}

// assertCoverage verifies the sample against the cross constraints: quota
// cap, per-control and per-bizline minimums, and change-ID uniqueness.
func assertCoverage(t *testing.T, batch *auditsample.Batch, s *auditsample.Sample, rules auditsample.Rules) {
	t.Helper()
	if len(s.ChangeIDs) > rules.Quota {
		t.Fatalf("sample size %d exceeds quota %d", len(s.ChangeIDs), rules.Quota)
	}
	byID := map[string]auditsample.Candidate{}
	for _, c := range batch.Candidates {
		if _, ok := byID[c.ChangeID]; !ok {
			byID[c.ChangeID] = c
		}
	}
	seen := map[string]bool{}
	controlCount := map[string]int{}
	lineCount := map[string]int{}
	for _, id := range s.ChangeIDs {
		if seen[id] {
			t.Fatalf("change %s sampled twice", id)
		}
		seen[id] = true
		c, ok := byID[id]
		if !ok {
			t.Fatalf("sampled change %s not in sealed batch", id)
		}
		controlCount[c.Control]++
		lineCount[c.BizLine]++
	}
	for ctrl, min := range rules.ControlMinimum {
		if controlCount[ctrl] < min {
			t.Fatalf("control %q covered %d times, minimum %d", ctrl, controlCount[ctrl], min)
		}
	}
	for line, min := range rules.BizLineMinimum {
		if lineCount[line] < min {
			t.Fatalf("bizline %q covered %d times, minimum %d", line, lineCount[line], min)
		}
	}
}

// Acceptable result: quota is sufficient, all coverage minimums are met,
// the sample is deterministic for the same (batch, seed, rules).
func TestDrawAcceptableSample(t *testing.T) {
	reg := auditsample.NewRegistry()
	batch, err := reg.Seal(fullGrid())
	if err != nil {
		t.Fatalf("seal: %v", err)
	}
	rules := gridRules(4)

	s, err := auditsample.Draw(batch, 20261119, rules)
	if err != nil {
		t.Fatalf("draw: %v", err)
	}
	if len(s.ChangeIDs) != 4 {
		t.Fatalf("expected exactly 4 picks (quota), got %d: %v", len(s.ChangeIDs), s.ChangeIDs)
	}
	assertCoverage(t, batch, s, rules)

	again, err := auditsample.Draw(batch, 20261119, rules)
	if err != nil {
		t.Fatalf("redraw: %v", err)
	}
	if !reflect.DeepEqual(s.ChangeIDs, again.ChangeIDs) {
		t.Fatalf("same seed must replay same sample: %v vs %v", s.ChangeIDs, again.ChangeIDs)
	}

	// Other seeds must still satisfy every cross constraint.
	for seed := uint64(1); seed <= 20; seed++ {
		ss, err := auditsample.Draw(batch, seed, rules)
		if err != nil {
			t.Fatalf("seed %d: %v", seed, err)
		}
		assertCoverage(t, batch, ss, rules)
	}
}

// Rejected result: quota 2 cannot cover 3 control minimums + 2 bizline
// minimums (at least 3 picks needed); Draw must report the uncoverable
// conditions instead of silently returning an under-covered sample.
func TestDrawQuotaUnsolvableReportsConditions(t *testing.T) {
	reg := auditsample.NewRegistry()
	batch, err := reg.Seal(fullGrid())
	if err != nil {
		t.Fatalf("seal: %v", err)
	}
	rules := gridRules(2)

	s, err := auditsample.Draw(batch, 7, rules)
	if err == nil {
		t.Fatalf("expected uncoverable error, got sample %v", s.ChangeIDs)
	}
	if s != nil {
		t.Fatalf("sample must be nil when uncoverable, got %+v", s)
	}
	var uerr *auditsample.UncoverableError
	if !errors.As(err, &uerr) {
		t.Fatalf("expected *UncoverableError, got %T: %v", err, err)
	}
	// Two greedy picks cover 2 controls and both lines; exactly one control
	// minimum remains unmet, by one pick, because of the quota.
	if len(uerr.Conditions) != 1 {
		t.Fatalf("expected exactly 1 uncoverable condition, got %+v", uerr.Conditions)
	}
	c := uerr.Conditions[0]
	if c.Kind != auditsample.KindControl || c.Required != 1 || c.Missing != 1 {
		t.Fatalf("unexpected condition: %+v", c)
	}
	if c.Reason != auditsample.ReasonQuotaExceeded {
		t.Fatalf("expected reason %q, got %q", auditsample.ReasonQuotaExceeded, c.Reason)
	}
	if !strings.Contains(err.Error(), c.Key) {
		t.Fatalf("error message should name the uncoverable condition: %v", err)
	}
}

// Rejected result: a control minimum exceeds the distinct candidates
// available for it, regardless of quota.
func TestDrawInsufficientCandidates(t *testing.T) {
	reg := auditsample.NewRegistry()
	batch, err := reg.Seal([]auditsample.Candidate{
		{ChangeID: "CHG-1", Control: "auth", BizLine: "pay"},
		{ChangeID: "CHG-2", Control: "crypto", BizLine: "risk"},
		{ChangeID: "CHG-3", Control: "crypto", BizLine: "pay"},
	})
	if err != nil {
		t.Fatalf("seal: %v", err)
	}
	rules := auditsample.Rules{
		Quota:          10,
		ControlMinimum: map[string]int{"auth": 2},
		BizLineMinimum: map[string]int{"pay": 1},
	}
	s, err := auditsample.Draw(batch, 1, rules)
	if err == nil || s != nil {
		t.Fatalf("expected uncoverable error, got sample %+v err %v", s, err)
	}
	var uerr *auditsample.UncoverableError
	if !errors.As(err, &uerr) {
		t.Fatalf("expected *UncoverableError, got %T", err)
	}
	if len(uerr.Conditions) != 1 {
		t.Fatalf("expected 1 condition, got %+v", uerr.Conditions)
	}
	c := uerr.Conditions[0]
	if c.Kind != auditsample.KindControl || c.Key != "auth" ||
		c.Required != 2 || c.Missing != 1 ||
		c.Reason != auditsample.ReasonInsufficientCandidates {
		t.Fatalf("unexpected condition: %+v", c)
	}
}

// Boundary + interleaved: one change satisfies a control minimum and a
// bizline minimum at the same time, so 4 minimums are covered by exactly 2
// picks; quota equal to that lower bound must succeed with exactly 2 picks.
func TestDrawBoundaryQuotaExactlyFitsDoubleCoverage(t *testing.T) {
	reg := auditsample.NewRegistry()
	batch, err := reg.Seal([]auditsample.Candidate{
		{ChangeID: "CHG-1", Control: "auth", BizLine: "pay"},
		{ChangeID: "CHG-2", Control: "crypto", BizLine: "risk"},
	})
	if err != nil {
		t.Fatalf("seal: %v", err)
	}
	rules := auditsample.Rules{
		Quota:          2,
		ControlMinimum: map[string]int{"auth": 1, "crypto": 1},
		BizLineMinimum: map[string]int{"pay": 1, "risk": 1},
	}
	s, err := auditsample.Draw(batch, 99, rules)
	if err != nil {
		t.Fatalf("quota equal to the double-coverage lower bound must succeed: %v", err)
	}
	if len(s.ChangeIDs) != 2 {
		t.Fatalf("expected 2 picks covering 4 minimums, got %v", s.ChangeIDs)
	}
	assertCoverage(t, batch, s, rules)

	// One less quota slot makes the same rules unsolvable.
	rules.Quota = 1
	if _, err := auditsample.Draw(batch, 99, rules); err == nil {
		t.Fatal("quota 1 with 4 interleaved minimums must be rejected")
	} else {
		var uerr *auditsample.UncoverableError
		if !errors.As(err, &uerr) || len(uerr.Conditions) == 0 {
			t.Fatalf("expected uncoverable conditions, got %v", err)
		}
		for _, c := range uerr.Conditions {
			if c.Reason != auditsample.ReasonQuotaExceeded {
				t.Fatalf("expected quota_exceeded, got %+v", c)
			}
		}
	}
}

// Duplicate change IDs count as one sample unit; the first sealed
// occurrence wins, and a quota larger than the distinct set simply yields
// every distinct change.
func TestDrawDuplicateChangeIDCountsOnce(t *testing.T) {
	reg := auditsample.NewRegistry()
	batch, err := reg.Seal([]auditsample.Candidate{
		{ChangeID: "CHG-1", Control: "auth", BizLine: "pay"},
		{ChangeID: "CHG-1", Control: "crypto", BizLine: "risk"}, // duplicate ID
		{ChangeID: "CHG-2", Control: "crypto", BizLine: "risk"},
		{ChangeID: "CHG-3", Control: "logging", BizLine: "pay"},
	})
	if err != nil {
		t.Fatalf("seal: %v", err)
	}
	rules := auditsample.Rules{
		Quota:          10,
		ControlMinimum: map[string]int{"crypto": 1, "logging": 1},
	}
	s, err := auditsample.Draw(batch, 5, rules)
	if err != nil {
		t.Fatalf("draw: %v", err)
	}
	if len(s.ChangeIDs) != 3 {
		t.Fatalf("expected 3 distinct changes, got %v", s.ChangeIDs)
	}
	assertCoverage(t, batch, s, rules)
	// CHG-1's first occurrence is auth/pay, so the crypto minimum forces
	// CHG-2 into the sample.
	want := map[string]bool{"CHG-1": true, "CHG-2": true, "CHG-3": true}
	for _, id := range s.ChangeIDs {
		if !want[id] {
			t.Fatalf("unexpected change %s in %v", id, s.ChangeIDs)
		}
		delete(want, id)
	}
	if len(want) != 0 {
		t.Fatalf("missing changes %v in %v", want, s.ChangeIDs)
	}
}

// Tamper evidence: replacing a candidate after sealing to the draw — even while
// keeping the original version number and digest fields — must be detected;
// the registry's sealed copy remains the replayable source of truth.
func TestVerifyDetectsSilentReplacement(t *testing.T) {
	reg := auditsample.NewRegistry()
	cands := fullGrid()
	batch, err := reg.Seal(cands)
	if err != nil {
		t.Fatalf("seal: %v", err)
	}
	rules := gridRules(4)
	s, err := auditsample.Draw(batch, 42, rules)
	if err != nil {
		t.Fatalf("draw: %v", err)
	}
	if err := auditsample.Verify(s, batch); err != nil {
		t.Fatalf("verify original: %v", err)
	}

	// Caller-side mutation of the input slice after sealing must not leak
	// into the sealed batch.
	cands[0] = auditsample.Candidate{ChangeID: "CHG-999", Control: "auth", BizLine: "pay"}
	if err := auditsample.Verify(s, batch); err != nil {
		t.Fatalf("sealed batch must be immune to caller-side mutation: %v", err)
	}

	// Forged batch: same version and digest fields, one candidate replaced.
	forged := &auditsample.Batch{
		Version:    batch.Version,
		Digest:     batch.Digest,
		Candidates: append([]auditsample.Candidate{}, batch.Candidates...),
	}
	forged.Candidates[1] = auditsample.Candidate{ChangeID: "CHG-999", Control: "crypto", BizLine: "risk"}
	if err := auditsample.Verify(s, forged); err == nil {
		t.Fatal("silent candidate replacement must be detected")
	}

	// Mutating the caller-held batch copy is detected as well, while the
	// registry's sealed copy still replays the original sample.
	batch.Candidates[0] = auditsample.Candidate{ChangeID: "CHG-999", Control: "auth", BizLine: "pay"}
	if err := auditsample.Verify(s, batch); err == nil {
		t.Fatal("mutated batch copy must fail verification")
	}
	sealed, err := reg.Batch(1)
	if err != nil {
		t.Fatalf("registry batch: %v", err)
	}
	if err := auditsample.Verify(s, sealed); err != nil {
		t.Fatalf("registry-sealed batch must still verify: %v", err)
	}
}

// Candidate correction opens a new batch version; the original batch, seed
// and rules still replay the original sample, and the original sample does
// not verify against the corrected batch.
func TestCorrectionOpensNewBatchOriginalReplays(t *testing.T) {
	reg := auditsample.NewRegistry()
	b1, err := reg.Seal(fullGrid())
	if err != nil {
		t.Fatalf("seal v1: %v", err)
	}
	rules := gridRules(4)
	s1, err := auditsample.Draw(b1, 777, rules)
	if err != nil {
		t.Fatalf("draw: %v", err)
	}

	corrected := fullGrid()
	corrected[0] = auditsample.Candidate{ChangeID: "CHG-1", Control: "auth", BizLine: "risk"} // correction
	b2, err := reg.Correct(b1, corrected)
	if err != nil {
		t.Fatalf("correct: %v", err)
	}
	if b2.Version != 2 {
		t.Fatalf("correction must open batch version 2, got %d", b2.Version)
	}
	if b2.Digest == b1.Digest {
		t.Fatal("corrected batch must have a different digest")
	}

	// Replay from the original sealed batch.
	orig, err := reg.Batch(1)
	if err != nil {
		t.Fatalf("batch v1: %v", err)
	}
	if err := auditsample.Verify(s1, orig); err != nil {
		t.Fatalf("original sample must replay from original batch: %v", err)
	}
	replay, err := auditsample.Draw(orig, s1.Seed, s1.Rules)
	if err != nil {
		t.Fatalf("replay draw: %v", err)
	}
	if !reflect.DeepEqual(replay.ChangeIDs, s1.ChangeIDs) {
		t.Fatalf("replay mismatch: %v vs %v", replay.ChangeIDs, s1.ChangeIDs)
	}

	// The original sample must not verify against the corrected batch.
	if err := auditsample.Verify(s1, b2); err == nil {
		t.Fatal("sample from batch v1 must not verify against corrected batch v2")
	}

	// The corrected batch is itself drawable and verifiable.
	s2, err := auditsample.Draw(b2, 777, rules)
	if err != nil {
		t.Fatalf("draw v2: %v", err)
	}
	if err := auditsample.Verify(s2, b2); err != nil {
		t.Fatalf("verify v2 sample: %v", err)
	}
	assertCoverage(t, b2, s2, rules)
}

// Empty minimums with a positive quota yield a plain random sample bounded
// by the quota; zero quota yields an empty sample.
func TestDrawNoMinimums(t *testing.T) {
	reg := auditsample.NewRegistry()
	batch, err := reg.Seal(fullGrid())
	if err != nil {
		t.Fatalf("seal: %v", err)
	}
	s, err := auditsample.Draw(batch, 3, auditsample.Rules{Quota: 4})
	if err != nil {
		t.Fatalf("draw: %v", err)
	}
	if len(s.ChangeIDs) != 4 {
		t.Fatalf("expected 4 picks, got %v", s.ChangeIDs)
	}
	assertCoverage(t, batch, s, auditsample.Rules{Quota: 4})

	s, err = auditsample.Draw(batch, 3, auditsample.Rules{Quota: 0})
	if err != nil {
		t.Fatalf("draw quota 0: %v", err)
	}
	if len(s.ChangeIDs) != 0 {
		t.Fatalf("quota 0 must yield empty sample, got %v", s.ChangeIDs)
	}
}
