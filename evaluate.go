package capacitycontroller

import "sort"

type clippedObs struct {
	key obsKey
	o   Observation
	// portion of the observation intersecting this window
	start, end int64
}

// evaluateWindow verifies window [start,end) against the currently known
// lifecycle and observations. It never mutates controller state.
func (c *Controller) evaluateWindow(idx int, start, end int64) Verdict {
	v := Verdict{Window: idx, Start: start, End: end, Status: StatusUnverifiable}

	gks := make([]genKey, 0, len(c.online))
	for gk := range c.online {
		gks = append(gks, gk)
	}
	sort.Slice(gks, func(i, j int) bool {
		if gks[i].instance != gks[j].instance {
			return gks[i].instance < gks[j].instance
		}
		return gks[i].gen < gks[j].gen
	})

	var totalAvailable, totalBusy, totalRequests int64
	anyOnline := false
	problems := []Problem{}

	for _, gk := range gks {
		// 1. Actual online slices of this generation inside the window.
		pieces := make([]span, 0)
		for _, s := range c.online[gk] {
			if s.end <= start || s.start >= end {
				continue
			}
			pieces = append(pieces, span{max64(s.start, start), min64(s.end, end)})
		}
		if len(pieces) == 0 {
			// Observations for an offline generation are contradictions:
			// an old generation must never fill a new generation's gap.
			for _, k := range c.byGen[gk] {
				o := c.observations[k]
				if o.End <= start || o.Start >= end {
					continue
				}
				problems = append(problems, Problem{
					Kind: ProblemContradiction, Instance: gk.instance, Gen: gk.gen,
					Start: max64(o.Start, start), End: min64(o.End, end),
					Detail: "observation claims time while this generation is offline",
				})
			}
			continue
		}
		anyOnline = true

		// 2. Collect observations touching the window and validate each
		//    against the window boundary and the online union.
		valid := make([]clippedObs, 0)
		union := append([]span(nil), pieces...)
		for _, k := range c.byGen[gk] {
			o := c.observations[k]
			if o.End <= start || o.Start >= end {
				continue
			}
			cs, ce := max64(o.Start, start), min64(o.End, end)

			if o.Start < start || o.End > end {
				problems = append(problems, Problem{
					Kind: ProblemOverlap, Instance: gk.instance, Gen: gk.gen,
					Start: cs, End: ce,
					Detail: "observation crosses the window boundary; intervals may not straddle windows",
				})
				continue
			}
			if !insideUnion(cs, ce, union) {
				problems = append(problems, Problem{
					Kind: ProblemContradiction, Instance: gk.instance, Gen: gk.gen,
					Start: cs, End: ce,
					Detail: "observation covers time outside the generation's online period",
				})
				continue
			}
			if o.Busy > o.End-o.Start {
				problems = append(problems, Problem{
					Kind: ProblemContradiction, Instance: gk.instance, Gen: gk.gen,
					Start: cs, End: ce,
					Detail: "busy increment exceeds the interval length",
				})
				continue
			}
			valid = append(valid, clippedObs{key: k, o: o, start: cs, end: ce})
			totalBusy += o.Busy
			totalRequests += o.Requests
		}

		// 3. Every online slice needs non-overlapping full coverage.
		sort.Slice(valid, func(i, j int) bool {
			if valid[i].start != valid[j].start {
				return valid[i].start < valid[j].start
			}
			return valid[i].end < valid[j].end
		})
		for _, p := range pieces {
			totalAvailable += p.end - p.start
			cursor := p.start
			for _, iv := range valid {
				if iv.end <= p.start || iv.start >= p.end {
					continue
				}
				if iv.start > cursor {
					problems = append(problems, Problem{
						Kind: ProblemGap, Instance: gk.instance, Gen: gk.gen,
						Start: cursor, End: iv.start,
						Detail: "online slice has no covering observation",
					})
					cursor = iv.end
					continue
				}
				if iv.start < cursor {
					oe := min64(iv.end, cursor)
					problems = append(problems, Problem{
						Kind: ProblemOverlap, Instance: gk.instance, Gen: gk.gen,
						Start: iv.start, End: oe,
						Detail: "observations overlap; increments cannot be attributed",
					})
				}
				if iv.end > cursor {
					cursor = iv.end
				}
			}
			if cursor < p.end {
				problems = append(problems, Problem{
					Kind: ProblemGap, Instance: gk.instance, Gen: gk.gen,
					Start: cursor, End: p.end,
					Detail: "online slice has no covering observation",
				})
			}
		}
	}

	if !anyOnline {
		problems = append(problems, Problem{
			Kind: ProblemNoOnline, Start: start, End: end,
			Detail: "no instance generation is online during this window",
		})
	}

	if len(problems) > 0 {
		sortProblems(problems)
		v.Problems = problems
		v.Busy = totalBusy
		v.Available = totalAvailable
		v.Requests = totalRequests
		return v
	}

	// 4. Verified window: total busy / total available vs watermarks.
	utilPermille := int((totalBusy*1000 + totalAvailable/2) / totalAvailable)
	v.Busy = totalBusy
	v.Available = totalAvailable
	v.Requests = totalRequests
	v.UtilizationPermille = utilPermille
	switch {
	case utilPermille >= c.cfg.HighWatermarkPermille:
		v.Status = StatusHigh
	case utilPermille <= c.cfg.LowWatermarkPermille:
		v.Status = StatusLow
	default:
		v.Status = StatusNormal
	}
	return v
}

func insideUnion(start, end int64, union []span) bool {
	for _, s := range union {
		if start >= s.start && end <= s.end {
			return true
		}
	}
	return false
}

func sortProblems(p []Problem) {
	sort.SliceStable(p, func(i, j int) bool {
		a, b := p[i], p[j]
		if a.Kind != b.Kind {
			return a.Kind < b.Kind
		}
		if a.Instance != b.Instance {
			return a.Instance < b.Instance
		}
		if a.Gen != b.Gen {
			return a.Gen < b.Gen
		}
		if a.Start != b.Start {
			return a.Start < b.Start
		}
		return a.End < b.End
	})
}

func min64(a, b int64) int64 {
	if a < b {
		return a
	}
	return b
}

func max64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
