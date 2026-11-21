package seqshard

import (
	"errors"
	"testing"
)

// referenceToken is the independently computed, per-sample expectation for one
// global token position.
type referenceToken struct {
	sampleID  string
	samplePos int
	value     int
	attention Range
	label     Label
}

// buildReference computes the same training semantics sample by sample without
// any rank notion: local window W inside the sample and next-token labels.
func buildReference(samples []Sample, window int) map[int]referenceToken {
	ref := make(map[int]referenceToken)
	offset := 0
	for _, s := range samples {
		for i, v := range s.Tokens {
			pos := offset + i
			winStart := offset + maxInt(0, i-window+1)
			lab := Label{GlobalPos: pos, Valid: false}
			if i < len(s.Tokens)-1 {
				lab.Value = s.Tokens[i+1]
				lab.Valid = true
			}
			ref[pos] = referenceToken{
				sampleID:  s.ID,
				samplePos: i,
				value:     v,
				attention: Range{Start: winStart, End: pos + 1},
				label:     lab,
			}
		}
		offset += len(s.Tokens)
	}
	return ref
}

// expectedFetchSet recomputes every remote attention token with plain set
// bookkeeping, independently of the range-merging implementation.
func expectedFetchSet(tokens []TokenPlan, own Range, cuts []int) map[int]map[int]struct{} {
	want := make(map[int]map[int]struct{})
	for _, tk := range tokens {
		for p := tk.Attention.Start; p < tk.Attention.End; p++ {
			if p >= own.Start && p < own.End {
				continue
			}
			src := -1
			for r := 0; r < len(cuts)-1; r++ {
				if p >= cuts[r] && p < cuts[r+1] {
					src = r
					break
				}
			}
			if want[src] == nil {
				want[src] = make(map[int]struct{})
			}
			want[src][p] = struct{}{}
		}
	}
	return want
}

func assertFetchMatchesSet(t *testing.T, rp RankPlan, cuts []int) {
	t.Helper()
	want := expectedFetchSet(rp.Tokens, rp.Owns, cuts)
	got := make(map[int]map[int]struct{})
	for _, seg := range rp.Fetch {
		// Every fetched range must really be owned by its source rank.
		if seg.Range.Start < cuts[seg.SourceRank] || seg.Range.End > cuts[seg.SourceRank+1] {
			t.Fatalf("rank %d fetches %v from rank %d which does not own it",
				rp.Rank, seg.Range, seg.SourceRank)
		}
		// Fetch ranges never overlap the consumer's own shard.
		if seg.Range.Start < rp.Owns.End && seg.Range.End > rp.Owns.Start {
			t.Fatalf("rank %d fetch %v overlaps own range %v", rp.Rank, seg.Range, rp.Owns)
		}
		if got[seg.SourceRank] == nil {
			got[seg.SourceRank] = make(map[int]struct{})
		}
		for p := seg.Range.Start; p < seg.Range.End; p++ {
			if _, dup := got[seg.SourceRank][p]; dup {
				t.Fatalf("rank %d fetches global token %d more than once", rp.Rank, p)
			}
			got[seg.SourceRank][p] = struct{}{}
		}
	}
	if len(got) != len(want) {
		t.Fatalf("rank %d: source rank set = %v, want %v", rp.Rank, got, want)
	}
	for src, wantPos := range want {
		gotPos, ok := got[src]
		if !ok || len(gotPos) != len(wantPos) {
			t.Fatalf("rank %d source %d: got %v, want %v", rp.Rank, src, gotPos, wantPos)
		}
		for p := range wantPos {
			if _, ok := gotPos[p]; !ok {
				t.Fatalf("rank %d source %d missing token %d", rp.Rank, src, p)
			}
		}
	}
}

func assertStitchEqualsReference(t *testing.T, p *Plan, samples []Sample, window int, cuts []int) {
	t.Helper()
	ref := buildReference(samples, window)
	stitched := p.GlobalTokens()
	if len(stitched) != p.TotalTokens {
		t.Fatalf("stitched %d tokens, total %d", len(stitched), p.TotalTokens)
	}
	seenPos := make(map[int]bool)
	labelByPos := make(map[int]Label)
	for _, rp := range p.Ranks {
		for _, lab := range rp.Labels {
			labelByPos[lab.GlobalPos] = lab
		}
	}
	if len(labelByPos) != p.TotalTokens {
		t.Fatalf("got %d labels for %d tokens", len(labelByPos), p.TotalTokens)
	}
	for i, tk := range stitched {
		if tk.GlobalPos != i {
			t.Fatalf("stitched position %d carries global pos %d", i, tk.GlobalPos)
		}
		if seenPos[i] {
			t.Fatalf("global token %d appears twice", i)
		}
		seenPos[i] = true
		want, ok := ref[i]
		if !ok {
			t.Fatalf("unexpected global token %d", i)
		}
		if tk.SampleID != want.sampleID || tk.SamplePos != want.samplePos || tk.Value != want.value {
			t.Fatalf("token %d identity = (%s,%d,%d), want (%s,%d,%d)",
				i, tk.SampleID, tk.SamplePos, tk.Value,
				want.sampleID, want.samplePos, want.value)
		}
		if tk.Attention != want.attention {
			t.Fatalf("token %d attention = %v, want %v", i, tk.Attention, want.attention)
		}
		lab := labelByPos[i]
		if lab != want.label {
			t.Fatalf("token %d label = %+v, want %+v", i, lab, want.label)
		}
	}
	for r := range p.Ranks {
		assertFetchMatchesSet(t, p.Ranks[r], cuts)
	}
}

// TestBuildLongSampleThreeShardsThenShort is the headline scenario: one long
// sample crossing all three shards with a short sample immediately after it.
// All expected ranges below are hand-computed on the global token stream.
func TestBuildLongSampleThreeShardsThenShort(t *testing.T) {
	long := Sample{ID: "long", Tokens: []int{0, 1, 2, 3, 4, 5, 6, 7, 8, 9}}
	short := Sample{ID: "short", Tokens: []int{100, 101, 102, 103}}
	samples := []Sample{long, short}
	cuts := []int{0, 4, 9, 14}
	const window = 3

	p, err := Build(samples, cuts, window)
	if err != nil {
		t.Fatalf("Build returned error: %v", err)
	}
	if p.TotalTokens != 14 {
		t.Fatalf("TotalTokens = %d, want 14", p.TotalTokens)
	}
	if p.SampleRanges["long"] != (Range{Start: 0, End: 10}) ||
		p.SampleRanges["short"] != (Range{Start: 10, End: 14}) {
		t.Fatalf("unexpected sample ranges: %+v", p.SampleRanges)
	}
	if len(p.Ranks) != 3 {
		t.Fatalf("got %d ranks, want 3", len(p.Ranks))
	}

	// Rank 0 owns global [0,4); its W=3 windows all resolve locally.
	r0 := p.Ranks[0]
	if r0.Owns != (Range{Start: 0, End: 4}) || len(r0.Tokens) != 4 || len(r0.Fetch) != 0 {
		t.Fatalf("rank 0 plan wrong: %+v", r0)
	}
	if r0.Tokens[3].SamplePos != 3 || r0.Tokens[3].Attention != (Range{Start: 1, End: 4}) {
		t.Fatalf("rank 0 last token wrong: %+v", r0.Tokens[3])
	}

	// Rank 1 owns [4,9); every window needs the two preceding long-sample
	// tokens, merged into one fetch from rank 0.
	r1 := p.Ranks[1]
	wantFetch1 := []FetchSegment{{SourceRank: 0, Range: Range{Start: 2, End: 4}}}
	if len(r1.Tokens) != 5 || !segmentsEqual(r1.Fetch, wantFetch1) {
		t.Fatalf("rank 1 fetch = %+v, want %+v", r1.Fetch, wantFetch1)
	}
	if r1.Tokens[0].GlobalPos != 4 || r1.Tokens[0].Attention != (Range{Start: 2, End: 5}) {
		t.Fatalf("rank 1 first token wrong: %+v", r1.Tokens[0])
	}

	// Rank 2 owns [9,14): the long sample's last token plus the whole short
	// sample. Windows [7,10) and [10,14) are adjacent and coalesced; after
	// removing the local [9,14), only [7,9) is fetched from rank 1.
	r2 := p.Ranks[2]
	wantFetch2 := []FetchSegment{{SourceRank: 1, Range: Range{Start: 7, End: 9}}}
	if len(r2.Tokens) != 5 || !segmentsEqual(r2.Fetch, wantFetch2) {
		t.Fatalf("rank 2 fetch = %+v, want %+v", r2.Fetch, wantFetch2)
	}

	// Labels: the long sample's final token (global 9) and the short sample's
	// final token (global 13) carry no loss; every other token points at the
	// next token of its own sample.
	wantLabels := map[int]Label{
		0:  {GlobalPos: 0, Value: 1, Valid: true},
		1:  {GlobalPos: 1, Value: 2, Valid: true},
		2:  {GlobalPos: 2, Value: 3, Valid: true},
		3:  {GlobalPos: 3, Value: 4, Valid: true},
		4:  {GlobalPos: 4, Value: 5, Valid: true},
		5:  {GlobalPos: 5, Value: 6, Valid: true},
		6:  {GlobalPos: 6, Value: 7, Valid: true},
		7:  {GlobalPos: 7, Value: 8, Valid: true},
		8:  {GlobalPos: 8, Value: 9, Valid: true},
		9:  {GlobalPos: 9, Valid: false},
		10: {GlobalPos: 10, Value: 101, Valid: true},
		11: {GlobalPos: 11, Value: 102, Valid: true},
		12: {GlobalPos: 12, Value: 103, Valid: true},
		13: {GlobalPos: 13, Valid: false},
	}
	gotLabels := make(map[int]Label)
	for _, rp := range p.Ranks {
		for _, lab := range rp.Labels {
			gotLabels[lab.GlobalPos] = lab
		}
	}
	if len(gotLabels) != len(wantLabels) {
		t.Fatalf("got %d labels, want %d", len(gotLabels), len(wantLabels))
	}
	for pos, want := range wantLabels {
		if gotLabels[pos] != want {
			t.Fatalf("label at %d = %+v, want %+v", pos, gotLabels[pos], want)
		}
	}

	// Distributed local descriptions, stitched back, must match the
	// sample-by-sample computation item by item.
	assertStitchEqualsReference(t, p, samples, window, cuts)
}

// TestInterleavedSplitsAcrossRanks has several samples each split by shard
// boundaries, exercising boundary handling on both sides of every sample.
func TestInterleavedSplitsAcrossRanks(t *testing.T) {
	samples := []Sample{
		{ID: "a", Tokens: []int{0, 1, 2, 3, 4}},
		{ID: "b", Tokens: []int{10, 11, 12, 13, 14}},
		{ID: "c", Tokens: []int{20, 21, 22, 23, 24}},
	}
	cuts := []int{0, 3, 8, 15}
	const window = 2

	p, err := Build(samples, cuts, window)
	if err != nil {
		t.Fatalf("Build returned error: %v", err)
	}
	wantFetch := [][]FetchSegment{
		nil,
		{{SourceRank: 0, Range: Range{Start: 2, End: 3}}},
		{{SourceRank: 1, Range: Range{Start: 7, End: 8}}},
	}
	for r := range p.Ranks {
		if !segmentsEqual(p.Ranks[r].Fetch, wantFetch[r]) {
			t.Fatalf("rank %d fetch = %+v, want %+v", r, p.Ranks[r].Fetch, wantFetch[r])
		}
	}
	assertStitchEqualsReference(t, p, samples, window, cuts)
}

// TestWindowOne is the boundary case: every context window contains only the
// token itself, so no attention data crosses ranks even when labels do.
func TestWindowOne(t *testing.T) {
	samples := []Sample{
		{ID: "a", Tokens: []int{10, 11, 12}},
		{ID: "b", Tokens: []int{20, 21}},
	}
	cuts := []int{0, 2, 5}
	p, err := Build(samples, cuts, 1)
	if err != nil {
		t.Fatalf("Build returned error: %v", err)
	}
	for _, rp := range p.Ranks {
		if len(rp.Fetch) != 0 {
			t.Fatalf("rank %d should fetch nothing with W=1, got %+v", rp.Rank, rp.Fetch)
		}
		for _, tk := range rp.Tokens {
			if tk.Attention != (Range{Start: tk.GlobalPos, End: tk.GlobalPos + 1}) {
				t.Fatalf("token %d attention %v not a singleton", tk.GlobalPos, tk.Attention)
			}
		}
	}
	// The label of global token 1 lives on rank 1 (global token 2), but labels
	// are reported separately and must not create a fetch segment.
	r0Labels := p.Ranks[0].Labels
	if !r0Labels[1].Valid || r0Labels[1].Value != 12 {
		t.Fatalf("cross-rank label wrong: %+v", r0Labels[1])
	}
	// Global token 2 is the last token of sample a and lives on rank 1: its
	// label is ignored, even though its predecessor's label pointed at it.
	if p.Ranks[1].Labels[0].GlobalPos != 2 || p.Ranks[1].Labels[0].Valid {
		t.Fatalf("global 2 label must be invalid, got %+v", p.Ranks[1].Labels[0])
	}
	assertStitchEqualsReference(t, p, samples, 1, cuts)
}

// TestLargeWindowSpansMultipleRanks makes one rank pull from two different
// source ranks and verifies deduplication of overlapping token windows.
func TestLargeWindowSpansMultipleRanks(t *testing.T) {
	samples := []Sample{
		{ID: "a", Tokens: []int{0, 1, 2, 3, 4, 5, 6, 7}},
		{ID: "b", Tokens: []int{8, 9, 10, 11}},
	}
	cuts := []int{0, 2, 6, 12}
	const window = 20 // larger than any sample: full per-sample prefixes

	p, err := Build(samples, cuts, window)
	if err != nil {
		t.Fatalf("Build returned error: %v", err)
	}
	if len(p.Ranks[0].Fetch) != 0 {
		t.Fatalf("rank 0 fetch = %+v, want none", p.Ranks[0].Fetch)
	}
	wantFetch1 := []FetchSegment{{SourceRank: 0, Range: Range{Start: 0, End: 2}}}
	if !segmentsEqual(p.Ranks[1].Fetch, wantFetch1) {
		t.Fatalf("rank 1 fetch = %+v, want %+v", p.Ranks[1].Fetch, wantFetch1)
	}
	wantFetch2 := []FetchSegment{
		{SourceRank: 0, Range: Range{Start: 0, End: 2}},
		{SourceRank: 1, Range: Range{Start: 2, End: 6}},
	}
	if !segmentsEqual(p.Ranks[2].Fetch, wantFetch2) {
		t.Fatalf("rank 2 fetch = %+v, want %+v", p.Ranks[2].Fetch, wantFetch2)
	}
	// Attention must never cross a sample boundary regardless of window size.
	tok8 := p.Ranks[2].Tokens[2] // global 8 = first token of sample b
	if tok8.SampleID != "b" || tok8.Attention != (Range{Start: 8, End: 9}) {
		t.Fatalf("sample b first token leaked across boundary: %+v", tok8)
	}
	assertStitchEqualsReference(t, p, samples, window, cuts)
}

// TestSingleTokenSamples covers minimum-length samples: every token is a final
// token and therefore has an ignored label.
func TestSingleTokenSamples(t *testing.T) {
	samples := []Sample{
		{ID: "x", Tokens: []int{7}},
		{ID: "y", Tokens: []int{8}},
		{ID: "z", Tokens: []int{9}},
	}
	cuts := []int{0, 1, 3}
	p, err := Build(samples, cuts, 4)
	if err != nil {
		t.Fatalf("Build returned error: %v", err)
	}
	for _, rp := range p.Ranks {
		if len(rp.Fetch) != 0 {
			t.Fatalf("single-token samples need no remote context, got %+v", rp.Fetch)
		}
		for _, lab := range rp.Labels {
			if lab.Valid {
				t.Fatalf("token %d should have an ignored label", lab.GlobalPos)
			}
		}
		for _, tk := range rp.Tokens {
			if tk.SamplePos != 0 || tk.Attention != (Range{Start: tk.GlobalPos, End: tk.GlobalPos + 1}) {
				t.Fatalf("single-token plan wrong: %+v", tk)
			}
		}
	}
	assertStitchEqualsReference(t, p, samples, 4, cuts)
}

// TestCutsAlignedWithSampleBoundaries verifies that no fetch is needed when
// shards line up exactly with sample ends.
func TestCutsAlignedWithSampleBoundaries(t *testing.T) {
	samples := []Sample{
		{ID: "a", Tokens: []int{0, 1, 2, 3}},
		{ID: "b", Tokens: []int{4, 5, 6, 7}},
	}
	cuts := []int{0, 4, 8}
	p, err := Build(samples, cuts, 3)
	if err != nil {
		t.Fatalf("Build returned error: %v", err)
	}
	for _, rp := range p.Ranks {
		if len(rp.Fetch) != 0 {
			t.Fatalf("aligned cuts should need no fetch, rank %d got %+v", rp.Rank, rp.Fetch)
		}
	}
	firstB := p.Ranks[1].Tokens[0]
	if firstB.GlobalPos != 4 || firstB.Attention != (Range{Start: 4, End: 5}) {
		t.Fatalf("first token of sample b wrong: %+v", firstB)
	}
	assertStitchEqualsReference(t, p, samples, 3, cuts)
}

// TestRandomizedLayouts drives many deterministic random layouts through both
// the planner and the independent sample-by-sample/set-based reference.
func TestRandomizedLayouts(t *testing.T) {
	seed := int64(987654321)
	next := func() int {
		// Deterministic LCG, no external dependencies.
		seed = seed*6364136223846793005 + 1442695040888963407
		return int((seed >> 33) & 0x7fffffff)
	}
	for iter := 0; iter < 200; iter++ {
		sampleCount := 1 + next()%4
		samples := make([]Sample, sampleCount)
		total := 0
		for i := range samples {
			n := 1 + next()%6
			toks := make([]int, n)
			for j := range toks {
				toks[j] = next() % 1000
			}
			samples[i] = Sample{ID: string(rune('a' + i)), Tokens: toks}
			total += n
		}
		rankCount := 1 + next()%4
		if rankCount > total {
			rankCount = total
		}
		boundaries := map[int]bool{0: true, total: true}
		for len(boundaries) < rankCount+1 {
			boundaries[1+next()%(total-1)] = true
		}
		cuts := make([]int, 0, len(boundaries))
		for b := range boundaries {
			cuts = append(cuts, b)
		}
		for i := 1; i < len(cuts); i++ {
			for j := i; j > 0 && cuts[j-1] > cuts[j]; j-- {
				cuts[j-1], cuts[j] = cuts[j], cuts[j-1]
			}
		}
		window := 1 + next()%6
		p, err := Build(samples, cuts, window)
		if err != nil {
			t.Fatalf("iteration %d Build error: %v", iter, err)
		}
		assertStitchEqualsReference(t, p, samples, window, cuts)
	}
}

func TestRangeHelpers(t *testing.T) {
	got := mergeRanges([]Range{{Start: 1, End: 4}, {Start: 3, End: 5}})
	if len(got) != 1 || got[0] != (Range{Start: 1, End: 5}) {
		t.Fatalf("overlap merge = %v", got)
	}
	got = mergeRanges([]Range{{Start: 1, End: 3}, {Start: 3, End: 4}, {Start: 6, End: 7}})
	if len(got) != 2 || got[0] != (Range{Start: 1, End: 4}) || got[1] != (Range{Start: 6, End: 7}) {
		t.Fatalf("adjacent/disjoint merge = %v", got)
	}
	sub := subtractRanges([]Range{{Start: 0, End: 10}}, Range{Start: 3, End: 7})
	if len(sub) != 2 || sub[0] != (Range{Start: 0, End: 3}) || sub[1] != (Range{Start: 7, End: 10}) {
		t.Fatalf("subtract middle = %v", sub)
	}
	sub = subtractRanges([]Range{{Start: 0, End: 10}}, Range{Start: 0, End: 4})
	if len(sub) != 1 || sub[0] != (Range{Start: 4, End: 10}) {
		t.Fatalf("subtract prefix = %v", sub)
	}
	if ov, ok := intersect(Range{Start: 1, End: 5}, Range{Start: 3, End: 7}); !ok || ov != (Range{Start: 3, End: 5}) {
		t.Fatalf("intersect overlap = %v ok=%v", ov, ok)
	}
	if _, ok := intersect(Range{Start: 1, End: 3}, Range{Start: 3, End: 5}); ok {
		t.Fatal("touching ranges must not intersect")
	}
	segs := mergeFetchSegments([]FetchSegment{
		{SourceRank: 1, Range: Range{Start: 5, End: 7}},
		{SourceRank: 0, Range: Range{Start: 0, End: 1}},
		{SourceRank: 1, Range: Range{Start: 7, End: 9}},
	})
	want := []FetchSegment{
		{SourceRank: 0, Range: Range{Start: 0, End: 1}},
		{SourceRank: 1, Range: Range{Start: 5, End: 9}},
	}
	if !segmentsEqual(segs, want) {
		t.Fatalf("fetch merge = %+v, want %+v", segs, want)
	}
}

func TestValidationErrors(t *testing.T) {
	samples := []Sample{{ID: "a", Tokens: []int{1, 2}}}
	cases := []struct {
		name    string
		samples []Sample
		cuts    []int
		window  int
	}{
		{"zero window", samples, []int{0, 2}, 0},
		{"cuts too short", samples, []int{0}, 2},
		{"cuts not starting at zero", samples, []int{1, 2}, 2},
		{"cuts ending wrong", samples, []int{0, 3}, 2},
		{"cuts not increasing", samples, []int{0, 0, 2}, 2},
		{"empty sample", []Sample{{ID: "x", Tokens: nil}}, []int{0, 0}, 2},
		{"duplicate sample id", []Sample{
			{ID: "a", Tokens: []int{1}},
			{ID: "a", Tokens: []int{2}},
		}, []int{0, 2}, 2},
		{"no samples with non-empty cuts", nil, []int{0, 1}, 2},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Build(tc.samples, tc.cuts, tc.window)
			var ve *ValidationError
			if !errors.As(err, &ve) {
				t.Fatalf("got %v, want *ValidationError", err)
			}
		})
	}
}

func TestSingleRankKeepsEverythingLocal(t *testing.T) {
	samples := []Sample{
		{ID: "a", Tokens: []int{1, 2, 3}},
		{ID: "b", Tokens: []int{4, 5}},
	}
	p, err := Build(samples, []int{0, 5}, 2)
	if err != nil {
		t.Fatalf("Build returned error: %v", err)
	}
	if len(p.Ranks) != 1 || len(p.Ranks[0].Fetch) != 0 {
		t.Fatalf("single rank plan wrong: %+v", p.Ranks)
	}
	assertStitchEqualsReference(t, p, samples, 2, []int{0, 5})
}

func TestEmptyStream(t *testing.T) {
	p, err := Build(nil, []int{0, 0}, 2)
	if err != nil {
		t.Fatalf("empty stream Build returned error: %v", err)
	}
	if p.TotalTokens != 0 || len(p.Ranks) != 1 {
		t.Fatalf("empty stream plan wrong: %+v", p)
	}
	if p.Ranks[0].Owns != (Range{}) || len(p.Ranks[0].Tokens) != 0 ||
		len(p.Ranks[0].Fetch) != 0 || len(p.Ranks[0].Labels) != 0 {
		t.Fatalf("empty rank must hold no tokens, fetch or labels: %+v", p.Ranks[0])
	}
	if len(p.GlobalTokens()) != 0 || len(p.SampleRanges) != 0 {
		t.Fatalf("empty plan must expose no global tokens or samples")
	}
	if _, err := Build(nil, []int{0, 0, 0}, 2); err == nil {
		t.Fatal("multiple zero-width ranks should be rejected")
	}
}

func segmentsEqual(a, b []FetchSegment) bool {
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
