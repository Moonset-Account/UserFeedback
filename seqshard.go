// Package seqshard plans the per-rank execution description for sequence
// parallel training.
//
// Training samples are concatenated into one global token stream (the token of
// one sample directly followed by the first token of the next sample). The
// stream is then handed out to ranks by contiguous global half-open ranges
// [Start, End), so a shard boundary may fall in the middle of a sample.
//
// Every token keeps the position it had inside its own sample. Its training
// label is the next token of the same sample; the final token of a sample has
// no label and its loss is ignored.
//
// The attention context of a token only contains the most recent W tokens of
// the same sample up to and including the token itself. Context that lives on
// another rank is described once as global fetch ranges merged per source
// rank (overlapping or adjacent ranges are coalesced and every token is taken
// at most once). Labels are reported separately and never appear inside fetch
// ranges.
package seqshard

// Range is a half-open global token interval [Start, End).
type Range struct {
	Start int
	End   int
}

// Sample is one training sample given as its ordered token values.
type Sample struct {
	ID     string
	Tokens []int
}

// Label describes the next-token label for one token. A sample's final token
// has Valid == false and contributes no loss.
type Label struct {
	GlobalPos int  // global position of the token the label belongs to
	Value     int  // next token value inside the same sample
	Valid     bool // false for the final token of a sample
}

// TokenPlan is the local execution description for one token.
type TokenPlan struct {
	GlobalPos int // position inside the concatenated global stream
	SampleID  string
	SamplePos int // zero-based position inside the original sample
	Value     int // token value
	// Attention is the complete local-context window for this token, expressed
	// as a global range and clipped to the token's own sample.
	Attention Range
}

// FetchSegment is a global interval that a rank must pull from SourceRank for
// attention computation. Intervals of one source rank are merged while
// overlapping or adjacent, so no global token appears twice.
type FetchSegment struct {
	SourceRank int
	Range      Range
}

// RankPlan is the execution description generated for one rank.
type RankPlan struct {
	Rank   int
	Owns   Range // global token range owned by this rank
	Tokens []TokenPlan
	// Fetch holds only ranges owned by other ranks, grouped and merged per
	// source rank. Label-only dependencies are deliberately excluded.
	Fetch  []FetchSegment
	Labels []Label
}

// Plan is the full distributed description for every rank plus the global
// layout of the concatenated samples.
type Plan struct {
	Ranks       []RankPlan
	TotalTokens int
	// SampleRanges maps each input sample to its global [Start, End) range.
	SampleRanges map[string]Range
}

type sampleLayout struct {
	id     string
	tokens []int
	start  int
	end    int
}

// Build concatenates samples into one global token stream and cuts it into
// contiguous rank ranges.
//
// cuts contains the range boundaries: rank r owns [cuts[r], cuts[r+1]); the
// first entry must be 0, the last one must equal the total token count and
// entries must be strictly increasing. window is the per-sample attention
// window W (at least 1).
func Build(samples []Sample, cuts []int, window int) (*Plan, error) {
	if window < 1 {
		return nil, &ValidationError{Reason: "window must be at least 1"}
	}
	if len(cuts) < 2 {
		return nil, &ValidationError{Reason: "cuts must contain at least a start and an end boundary"}
	}
	if cuts[0] != 0 {
		return nil, &ValidationError{Reason: "first cut must be 0"}
	}
	if len(samples) == 0 && cuts[len(cuts)-1] != 0 {
		return nil, &ValidationError{Reason: "no samples but cuts describe a non-empty stream"}
	}

	layouts := make([]sampleLayout, 0, len(samples))
	total := 0
	seen := make(map[string]bool, len(samples))
	for _, s := range samples {
		if len(s.Tokens) == 0 {
			return nil, &ValidationError{Reason: "samples must contain at least one token"}
		}
		if seen[s.ID] {
			return nil, &ValidationError{Reason: "duplicate sample id: " + s.ID}
		}
		seen[s.ID] = true
		layouts = append(layouts, sampleLayout{
			id:     s.ID,
			tokens: s.Tokens,
			start:  total,
			end:    total + len(s.Tokens),
		})
		total += len(s.Tokens)
	}

	if cuts[len(cuts)-1] != total {
		return nil, &ValidationError{Reason: "last cut must equal the total token count"}
	}
	if total == 0 {
		// The only valid layout for an empty stream is one zero-width rank.
		if len(cuts) != 2 || cuts[0] != 0 || cuts[1] != 0 {
			return nil, &ValidationError{Reason: "empty stream only accepts cuts [0,0]"}
		}
	} else {
		for i := 1; i < len(cuts); i++ {
			if cuts[i] <= cuts[i-1] {
				return nil, &ValidationError{Reason: "cuts must be strictly increasing"}
			}
		}
	}

	sampleAt := func(globalPos int) sampleLayout {
		// Layouts are ordered and non-overlapping; binary search keeps this
		// independent of a linear scan for long streams.
		lo, hi := 0, len(layouts)
		for lo < hi {
			mid := (lo + hi) / 2
			if layouts[mid].end <= globalPos {
				lo = mid + 1
			} else {
				hi = mid
			}
		}
		return layouts[lo]
	}

	rankCount := len(cuts) - 1
	plan := &Plan{
		Ranks:        make([]RankPlan, rankCount),
		TotalTokens:  total,
		SampleRanges: make(map[string]Range, len(layouts)),
	}
	for _, l := range layouts {
		plan.SampleRanges[l.id] = Range{Start: l.start, End: l.end}
	}

	for r := 0; r < rankCount; r++ {
		own := Range{Start: cuts[r], End: cuts[r+1]}
		rp := RankPlan{Rank: r, Owns: own}

		// Windows for every owned token, merged first into the smallest set of
		// disjoint global ranges. Adjacent ranges are coalesced because
		// fetching [a,b) and [b,c) is one continuous transfer.
		var windows []Range
		for pos := own.Start; pos < own.End; pos++ {
			l := sampleAt(pos)
			samplePos := pos - l.start
			winStart := l.start + maxInt(0, samplePos-window+1)
			rp.Tokens = append(rp.Tokens, TokenPlan{
				GlobalPos: pos,
				SampleID:  l.id,
				SamplePos: samplePos,
				Value:     l.tokens[samplePos],
				Attention: Range{Start: winStart, End: pos + 1},
			})
			windows = mergeRanges(append(windows, Range{Start: winStart, End: pos + 1}))

			if samplePos < len(l.tokens)-1 {
				rp.Labels = append(rp.Labels, Label{
					GlobalPos: pos,
					Value:     l.tokens[samplePos+1],
					Valid:     true,
				})
			} else {
				rp.Labels = append(rp.Labels, Label{
					GlobalPos: pos,
					Valid:     false,
				})
			}
		}

		// Keep local pieces for attention without any fetch; every remaining
		// piece must come from another rank.
		remote := subtractRanges(windows, own)
		for _, piece := range remote {
			for src := 0; src < rankCount; src++ {
				if src == r {
					continue
				}
				srcRange := Range{Start: cuts[src], End: cuts[src+1]}
				if got, ok := intersect(piece, srcRange); ok {
					rp.Fetch = append(rp.Fetch, FetchSegment{SourceRank: src, Range: got})
				}
			}
		}
		rp.Fetch = mergeFetchSegments(rp.Fetch)
		plan.Ranks[r] = rp
	}
	return plan, nil
}

// GlobalTokens rebuilds the concatenated token stream from the per-rank
// descriptions in rank order.
func (p *Plan) GlobalTokens() []TokenPlan {
	out := make([]TokenPlan, 0, p.TotalTokens)
	for r := range p.Ranks {
		out = append(out, p.Ranks[r].Tokens...)
	}
	return out
}

// ValidationError describes an invalid Build argument.
type ValidationError struct {
	Reason string
}

func (e *ValidationError) Error() string {
	return "seqshard: " + e.Reason
}

// mergeRanges sorts and coalesces ranges, joining ranges that overlap or touch
// (the end of one equals the start of the next).
func mergeRanges(in []Range) []Range {
	if len(in) == 0 {
		return nil
	}
	sorted := make([]Range, len(in))
	copy(sorted, in)
	for i := 1; i < len(sorted); i++ {
		for j := i; j > 0 && sorted[j-1].Start > sorted[j].Start; j-- {
			sorted[j-1], sorted[j] = sorted[j], sorted[j-1]
		}
	}
	merged := []Range{sorted[0]}
	for _, rg := range sorted[1:] {
		last := &merged[len(merged)-1]
		if rg.Start <= last.End {
			if rg.End > last.End {
				last.End = rg.End
			}
		} else {
			merged = append(merged, rg)
		}
	}
	return merged
}

// subtractRanges returns the parts of ranges that do not overlap cut, keeping
// the merged, disjoint representation of ranges.
func subtractRanges(ranges []Range, cut Range) []Range {
	var out []Range
	for _, rg := range ranges {
		left := Range{Start: rg.Start, End: minInt(rg.End, cut.Start)}
		if left.End > left.Start {
			out = append(out, left)
		}
		right := Range{Start: maxInt(rg.Start, cut.End), End: rg.End}
		if right.End > right.Start {
			out = append(out, right)
		}
	}
	return mergeRanges(out)
}

// intersect returns the overlap of a and b when it is non-empty.
func intersect(a, b Range) (Range, bool) {
	start := maxInt(a.Start, b.Start)
	end := minInt(a.End, b.End)
	if start >= end {
		return Range{}, false
	}
	return Range{Start: start, End: end}, true
}

// mergeFetchSegments coalesces overlapping or adjacent ranges that have the
// same source rank and keeps segments ordered by source rank and position.
func mergeFetchSegments(in []FetchSegment) []FetchSegment {
	if len(in) == 0 {
		return nil
	}
	sorted := make([]FetchSegment, len(in))
	copy(sorted, in)
	for i := 1; i < len(sorted); i++ {
		for j := i; j > 0; j-- {
			if sorted[j-1].SourceRank < sorted[j].SourceRank ||
				(sorted[j-1].SourceRank == sorted[j].SourceRank &&
					sorted[j-1].Range.Start <= sorted[j].Range.Start) {
				break
			}
			sorted[j-1], sorted[j] = sorted[j], sorted[j-1]
		}
	}
	merged := []FetchSegment{sorted[0]}
	for _, seg := range sorted[1:] {
		last := &merged[len(merged)-1]
		if seg.SourceRank == last.SourceRank && seg.Range.Start <= last.Range.End {
			if seg.Range.End > last.Range.End {
				last.Range.End = seg.Range.End
			}
		} else {
			merged = append(merged, seg)
		}
	}
	return merged
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
